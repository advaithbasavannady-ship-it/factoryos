# FactoryOS technical decisions and open questions

Last updated: 2026-11-12

## Verified implementation decisions

| Topic | Current decision/state | Evidence and limitation |
|---|---|---|
| Tenant authority | Active company membership supplies tenant role and department; operational context derives company from valid session | Two-company Mongo integration tests deny cross-company reads/writes and verify active membership switching |
| Isolated Mongo | Use only `factoryos_security_test` on the named single-node replica set | Container has no published ports; internal Docker network and transaction behavior verified in test environment |
| Refresh concurrency | Same-tab promise and cross-tab Web Locks coordinate browser refresh; server binds UUID request ID to one session/user/company/family and exact old/successor token hashes | 30-second bounded result reuse; 15/15 dedicated DB checks and full integration pass. Simultaneous same-ID requests return the same rotation. Sequential or out-of-window old-token reuse revokes the family. |
| Refresh result secrecy | Response payload is AES-GCM encrypted under an HKDF-derived key using validated JWT secret and dedicated context; raw refresh token is never stored as plaintext | Ciphertext and retry behavior checked against isolated Mongo. The JWT secret must remain stable/available for decrypting records during the short window. |
| Refresh anti-restoration | Before cached result return, session status, expiry, auth version, user state, active membership and current successor hash are rechecked | Expired and revoked session cases passed. Request ID globally unique while result record exists; TTL cleanup does not extend logical acceptance. |
| Audit durability | `run_audited_mutation` couples selected business writes and required audit event in Mongo transaction; idempotent routes also commit original outcome together. Successful login/session, password-reset token issuance/completion, logout/company switch, invitation and membership mutations use it. Refresh rotation and replay revocation have same-transaction audit writes. | Injected audit insert failure returned 503 with business write aborted; audit event persisted after successful retry. Guarantee applies only to migrated routes on transaction-capable topology. |
| Audit fallback | Failed-login/security-denial events and generic middleware audit remain best-effort; auxiliary email delivery is background work after commit and has no durable outbox | These are outside the state-mutation audit guarantee. A delivery failure is logged, but can require retrying invitation/reset flows. |
| Idempotency | Required on inventory adjust, purchase create/update/receipt, order payment, transfer, finished-goods ready and dispatch | Hash of key and canonical payload. Unique tenant-key record binds actor and operation; identical retries return original result; mismatched key reuse returns 409. Permission checks precede replay. Full Mongo suite 64/64. |
| Idempotency retention | Records are retained indefinitely; no TTL cleanup is installed | Growth and cleanup policy need operations/product approval; no automatic deletion risks reopening duplicate effects. |
| Client compatibility | Frontend source contains no calls to those operational mutation routes; no Excel import/export workflow or client is present in current frontend | External integrations and independently deployed clients cannot be inventoried from this workspace. They must be identified and updated before release enforcement. |
| Browser refresh coordination | Playwright desktop Chrome test opens two pages and validates one refresh request and a shared token | First run reproduced two requests despite Web Locks. An in-memory BroadcastChannel request/response under the lock fixed the client race; final E2E passed 1/1. All API calls are stubbed in this E2E. |
| Production topology evidence | No production deployment config found in workspace | README describes only a local development pod Mongo process. No production connection was made; topology, failover, transaction and durability behavior remain unknown. |
| Idempotency growth | Cleaned isolated test DB contains 0 records / 0 BSON bytes | Empty baseline is not representative. Scenario model and no-TTL recommendation are in `PHASE_1_2_REMAINING_GATES.md`. |
| Department visibility | No field-level response policy approved | A data-owner visibility matrix is required; no speculative redaction was added. |
| Test topology | Isolated single-node replica set supports transactions | Production topology and transaction policy remain unverified. |
| Source control | No `.git` directory exists | No Git initialization performed; exact complete diff cannot be independently reconstructed from Git. |

## Refresh token handling design

- Browser generates a UUIDv4 per logical refresh. Same-tab promise and `navigator.locks` coordinate refreshes where supported. Under the lock, a tab can request a still-valid access token from a peer over `BroadcastChannel`; refresh credentials remain HttpOnly cookies and access tokens are not persisted to localStorage.
- The browser keeps the request ID for at most 30 seconds to retry a timed-out request. A new logical refresh uses a new UUID. Server hashes request IDs and refresh tokens; a unique request-ID-hash index prevents a live ID from binding to a different principal/session.
- Successful rotation and refresh-request record are committed in one Mongo transaction. The record binds request ID, session/family, user, company, old-token hash, successor-token hash, issue time and expiration window. Cached response is AES-256-GCM encrypted with HKDF-derived key material and authenticated associated data for the bindings.
- A duplicate response is returned only within 30 seconds, with the exact ID and matching old or just-issued successor cookie, while the user/membership/session remain active and unexpired and session's current refresh hash equals the recorded successor. A stale previous successor cannot overwrite a newer rotation.
- After 30 seconds, reuse of an old token revokes the refresh family. The 30-second acceptance interval is an explicit bounded bearer replay risk: possession of both old cookie and matching request ID inside the interval can retrieve the same successor. Outside it, replay detection remains active.
- TTL index cleanup is scheduled after the logical window; app-level time checks enforce 30 seconds regardless of Mongo TTL monitor delay. Transaction support is mandatory; the route must fail closed on unsupported topology.

## Audit transaction design and recovery

- Critical migrated handler writes the business state and required audit record in one Mongo transaction. Audit insert failure raises; transaction aborts and route returns 503. Idempotency outcome, mutation, and audit record share the same transaction.
- Transaction callback may be retried by the driver; mutation callbacks must therefore be database-only and retry-safe. Emails, notifications and other external side effects must be deferred until after commit or moved to an outbox.
- No claim is made for deployments without transaction semantics. Current implementation does not silently fall back to a non-atomic mutation. A transaction-unavailable deployment must remain blocked for routes depending on this contract.
- A durable email outbox is designed in `PHASE_1_2_REMAINING_GATES.md` but not implemented. With transaction support, token/membership state, audit and outbox record should commit together. If the deployment lacks transactions, a different atomic persistence design is required before email workflows can claim durable delivery.
- Required audit coverage presently includes idempotent inventory/purchase/payment/transfer/finished-goods routes, order lifecycle/customer, production log, attendance, machine/maintenance, schedule/company update, successful authentication/session, password-reset, invitation and membership state mutations. Failed login and generic request events remain best-effort. Email delivery is post-commit and is not guaranteed by an outbox.

## Idempotency contract and rollout

- Required `Idempotency-Key` is hashed before storage and scoped by tenant. Stored record binds actor, operation and canonical payload digest; a different actor, operation or payload with same key is rejected with 409.
- Permission and current department checks must run before replay result is returned. The original serialized response is stored in the mutation transaction.
- Idempotency records have no TTL. Do not delete until a reviewed retention policy considers storage growth, backup retention, delayed retries, duplicate-effect risk and migration strategy.
- Current covered routes: inventory stock adjustment; purchase create/update/receipt; order payment, process transfer, finished-goods ready and dispatch. Other state-changing routes are not implied to be idempotent.
- Repository clients were searched. There are no frontend calls to covered operations or Excel import/export flows in the current frontend. External clients remain unknown; deployment must inventory them and verify header compatibility before enforcement.

## Unresolved decisions

1. **Department response visibility.** Unknown: which order/Job Card/production/customer/quality fields each department may see. Why: record-level filtering can still expose unrelated department data. Evidence/owner: product and department data owners approve a field matrix. Implementation can proceed only on fields already explicitly classified; response redaction is blocked.
2. **Production transaction topology.** Unknown: actual deployment MongoDB topology, transaction settings and failover behavior. Why: refresh and audited mutations require transactions. Evidence/owner: deployment operations provide topology and tested transaction/failover proof. Production enablement is blocked.
3. **External idempotency clients.** Unknown: deployed scripts, Excel macros, integrations and third-party callers. Why: required headers can break clients and retries without stable keys can duplicate operations. Evidence/owner: product/integration owner inventories clients and updates them. Repository-only changes are complete; external enforcement/release remains blocked.
4. **Security event retention.** Unknown: retention and alerting for failed-login and generic request audit signals. Why: these are still best-effort and may be lost during database outages, though successful credential/session and membership mutations are transaction-coupled. Evidence/owner: security owner defines retention, monitoring and outage behavior. Stronger security-event durability is blocked.
5. **Idempotency record retention.** No TTL approved. Why: indefinite storage growth and cleanup can reopen duplicate effects. Evidence/owner: operations/product define retention and archival/reconciliation; no cleanup implementation until then.
6. **Deployment CORS/proxy/edge limits/SMTP/recovery.** Unknown actual deployment values and runbooks. Why: affects CSRF, client trust, abuse resistance, invitation/reset delivery and recovery. Evidence/owner: deployment operations provide config and recovery drill. Deployment signoff is blocked.
7. **Department response visibility.** Proposed matrix exists in `PHASE_1_2_REMAINING_GATES.md`. Product and department owners must approve it before redaction; no policy has been implemented.
8. **Email outbox.** Transactional outbox design is documented. Provider idempotency, encrypted payload key management, retry/dead-letter thresholds, monitoring owner and retention require approval before implementation.
9. **Security-denial telemetry.** Failed-login and generic middleware audit remain best-effort. Decide retention, durable sink/monitoring and outage behavior.

## Technical recommendation

Keep Phase 1.2 unapproved pending the blocked deployment, external-client and department visibility gates, and explicit audit coverage for security-sensitive mutations. Do not treat test-replica success as production evidence.

Concise owner-facing alternatives, risks and recommendations are summarized in `PHASE_1_2_OWNER_DECISIONS.md`. Those recommendations are not implemented policy.
# Session note — 2026-11-12

- Added regression coverage for concurrent refresh requests using one old token and two different request IDs. It is pending execution; the existing 4/4 server-backed count predates this test.
- Docker Desktop CLI at `/Applications/Docker.app/Contents/Resources/bin/docker` used the local `desktop-linux` context. Inspection verified the Mongo test container is attached only to `factoryos-phase12-isolated`, has no published ports, and uses the existing named data volume.
- The new regression passed 1/1 against `factoryos_security_test`, verifying replay rejection and session-family revocation. No product decision was made and no security policy was weakened. Phase 1.2 remains unapproved.
- The fixture seeder refused to overwrite existing test fixtures. The guarded runner provisioner created its synthetic runner/reset fixture; no test data cleanup was performed.

