# Phase 1.2 remaining investigations and proposals

Updated: 2026-11-12. Phase 1.2 remains unapproved. No production or demo systems were contacted.

## 1. Production MongoDB topology

### Evidence found

- The workspace has no production Compose/Kubernetes/Helm/Terraform/deployment manifest or production connection profile.
- `README.md` describes the development pod running a local `mongod` and setting `MONGO_URL` to `localhost`. That is developer-environment documentation and is not evidence of production topology.
- The isolated Phase 1.2 test environment is a single-node replica set and passed transaction-backed integration tests. This proves only that the test deployment supports the exercised transaction semantics.
- No connection to production was made.

### Requirements to verify before deployment

The deployment owner must provide, without sharing credentials in source control:

1. MongoDB server/version and hosting provider.
2. Standalone, replica-set, or sharded topology; member count, regions/zones, voting and write-concern policy.
3. Whether application sessions support multi-document transactions and the transaction lifetime/size limits.
4. Journaling, read/write concern, retryable-write and driver retry settings.
5. Failover tests demonstrating commit/unknown-commit behavior while primary changes, plus backup/restore and point-in-time recovery evidence.
6. The intended application role and per-database privileges.

Critical refresh, idempotency, and audit-coupled mutations must fail closed if the deployed topology cannot provide the transaction behavior they require. Do not switch them to non-atomic writes. If the service cannot support transactions, choose and test an atomic outbox/data-model design before enabling those writes.

**Status:** blocked by deployment evidence. The local README is insufficient to make a topology inference.

## 2. API clients and integration inventory

### Repository inventory

- Current browser API calls cover authentication/session flows and the dashboard overview. No frontend source calls inventory adjustment, purchase create/update, order payment, process transfer, ready, or dispatch.
- The required `Idempotency-Key` header is already present in the backend contract for these seven covered mutation shapes:
  - `POST /api/inventory/items/{item_id}/adjust`
  - `POST /api/purchases`
  - `PATCH /api/purchases/{purchase_id}` (including receipt/status updates)
  - `POST /api/orders/{order_id}/payments`
  - `POST /api/orders/{order_id}/stages/{k}/transfer`
  - `POST /api/orders/{order_id}/ready`
  - `POST /api/orders/{order_id}/dispatch`
- Repository integration tests call the covered routes with idempotency keys.
- `openpyxl` appears in dependency manifests, but no application import/export implementation or Excel client was found.
- No webhook client, CLI, scheduled job, or separate API SDK calling the covered routes was found.

### Limits and release gate

Repository search cannot discover deployed Excel macros, scripts, customer integrations, mobile clients, or callers in another repository. Before release enforcement, the product/integration owner must inventory API gateway logs and integration ownership records, contact each caller owner, issue keys stable across retries, and verify compatibility in staging. Keep a migration/communication window for clients that need updates; do not silently exempt a client from idempotency or silently deploy an incompatible contract.

**Status:** local inventory complete; external inventory blocked by owner/deployment evidence.

## 3. Failed-login and middleware audit assessment

- `write_audit_event()` propagates insert failures when given a Mongo session, but catches and logs failures for calls without a transaction session.
- Failed credential checks call the non-transactional audit path. Rate-limited attempts return before that audit call. Some other authentication denials and unknown-account reset requests intentionally do not create per-account records to avoid revealing account existence.
- `RequestAuditMiddleware` writes a best-effort generic event after the response for API POST/PUT/PATCH/DELETE outside `/api/auth/` when the handler has not set `transactional_audit_committed`. This cannot roll back the already completed route mutation. It also records only requests with an active company context.
- Migrated critical state changes use required transactional audit. Generic middleware events remain supplementary telemetry, not a reliable mutation ledger.

### Proposed classification

1. Keep business and successful security state changes on the existing required transaction-plus-audit path.
2. Treat failed-login, rate-limit, origin-denial, and other security-denial signals as security telemetry rather than business mutation audit. Record a pseudonymous subject/IP hash, event category, timestamp, request ID and outcome; never persist passwords, reset/invitation tokens, cookies or raw bearer tokens.
3. Define whether security-denial telemetry must be durable during a database outage. If yes, ship structured events to an independently durable monitored log/security sink, and alert when delivery is unavailable. A best-effort local log is not a guarantee.
4. Do not use the generic response middleware as the only audit for a newly added critical route. A route must explicitly use the audited mutation contract or be documented as non-critical.

**Owner decision needed:** security-event retention, access controls, alert thresholds, and whether failed authentication must fail closed when the audit sink is unavailable. No change to this outage policy is made here.

## 4. Durable email outbox proposal

The current mail adapter runs as a background task after the relevant request path; delivery failure is logged but there is no durable retry record. Use a transactional outbox for reset and invitation delivery:

### Proposed `email_outbox` record

```json
{
  "event_id": "uuid",
  "tenant_id": "company-id-or-system",
  "kind": "membership.invitation | auth.password_reset",
  "template_version": 1,
  "recipient_hash": "sha256(normalized-recipient)",
  "payload_ciphertext": "base64",
  "payload_nonce": "base64",
  "payload_key_version": "kms-key-version",
  "state": "pending | leased | sent | retry | dead_letter",
  "attempts": 0,
  "next_attempt_at": "UTC timestamp",
  "lease_owner": null,
  "lease_until": null,
  "created_at": "UTC timestamp",
  "sent_at": null,
  "last_error_code": null
}
```

- In one transaction, write the token hash or invitation state, required audit event, and one outbox row. Email workers never create or change the underlying credential/membership state.
- Encrypt sensitive delivery payloads with a dedicated managed key. Do not put raw reset/invitation tokens in plaintext outbox fields or logs. Define key rotation and decrypt access before rollout.
- A worker atomically leases due events, sends using a stable event-derived message ID, then marks sent. Retry transient errors with bounded exponential backoff and jitter. Move exhausted/permanent failures to a visible dead-letter state and alert an operator.
- SMTP has no general exactly-once delivery guarantee. A crash after the mail server accepts a message but before the database marks it sent may cause a duplicate. Use provider idempotency if available, a stable `Message-ID`, and idempotent single-use tokens; state the delivery guarantee as at-least-once.
- Reconciliation reports old pending/leased records, stale leases, repeated failures and state rows without a corresponding outbox row. Monitor queue age, attempts and dead-letter count.
- Do not TTL-delete outbox records until a retention policy is approved. Sent records can later be archived under an approved policy; pending/dead-letter rows must remain visible.

**Decisions needed before implementation:** mail provider and idempotency support, encryption key management, retry/dead-letter thresholds, operational alert owner, and retention. No outbox has been implemented.

## 5. Proposed department-level field visibility policy (for approval only)

This is a conservative proposal based on current departments and API field groups. It is not implemented. The policy should be enforced on response models/DTOs, not by hiding fields in the UI. Company-wide permissions continue to receive the full approved company payload.

| Active membership scope | Proposed order/customer fields | Proposed production/Job Card fields | Proposed commercial/other fields |
|---|---|---|---|
| Sales | Customer identity/contact and sales order details for orders the membership may access | Stage/status, due date, approved quantity and QC/dispatch state summaries | Price, payment terms, advance and balance visible; no internal operator notes unless explicitly shared |
| Production department (only assigned department) | Order number, product/specification reference and required completion date; customer contact details masked | Technical Job Card fields required by that process, assigned route/stage, input/OK/reject/WIP quantities and that department's own notes | No unit price, order total, payment history, supplier cost, unrelated department notes or other departments' detailed production records |
| Quality | Order and product identity needed to inspect; customer contact details masked | Approved technical revision, process history needed for inspection, inspection data, defect/rejection reason, rework disposition and QA sign-off | No price/payment, supplier cost, or unrelated HR/attendance information |
| Dispatch | Order/shipping identity, destination/contact required for delivery, packed quantity, delivery date, carrier/reference and dispatch status | Final accepted quantity and dispatch authorization only | No price/payment details, supplier details, or unrelated process/employee notes |
| Inventory/Stores | Material/finished-goods demand reference and authorized order identifier | Material/specification, batch/lot, location, reservation/issue/return quantity and required technical handling fields | No customer contact, sales prices/payment, unrelated production notes, supplier commercial terms unless separately authorized |
| Purchasing | Purchase request/order, supplier, material/spec, quantity, receipt and invoice-match fields | Material demand and approved specification/revision; no unrelated production/operator details | Supplier pricing and purchasing totals visible; no customer contact or sales margin/payment data |
| Department manager | Same field set as their department, across that department's records | Department's own work plus aggregate metrics for other departments only if separately authorized | No company-wide commercial or personal data by default |
| Owner/admin or explicit company-wide permission | Full company record subject to purpose and audit rules | Full approved technical and production record | Full commercial view |

### Open policy questions

- Should production operators see customer names, masked identifiers, or only order numbers?
- Are customer delivery addresses/contact fields needed by Production or only Dispatch/Sales?
- Which technical fields are required by each of the 24 standard processes, QA, and Stores?
- Can department managers see cross-department aggregate quantities, and at what granularity?
- Should reject/rework reasons or operator names be restricted to QA/management?
- Are invoice values, unit costs, order prices and payments restricted to distinct roles?

**Approval gate:** product and department owners must approve the matrix and answer the questions before field-level filtering is implemented. No redaction rule has been applied.

## 6. Browser E2E for simultaneous refresh

- Added `tests/e2e/auth-refresh-tabs.spec.ts` and a Playwright `test:e2e` command. The test opens two real Chrome tabs, routes all API calls to in-test stubs, and asserts both restore the same session with one refresh request and the same UUID request ID.
- The first run reproduced a browser race: two refresh HTTP requests were issued despite Web Locks because the second tab acquired the lock before processing the BroadcastChannel result. The browser client now requests a live access token from peer tabs over BroadcastChannel while holding the lock; the token is held in memory and is not persisted to localStorage.
- Final desktop Chrome result: **1 passed**. This validates browser coordination only. The APIs were stubbed; MongoDB refresh behavior is separately covered by the 15-case probe and full integration suite. No backend or database was contacted by this E2E run.
- Evidence: `tests/evidence/auth-refresh-final/results.json`; initial race screenshot/context under `tests/evidence/auth-refresh-before-fix/`.

## 7. Idempotency growth and retention

- Read-only measurement in the cleaned isolated test database: `idempotency_records` contained **0 records, 0 BSON bytes**. This is an empty-test baseline, not production growth evidence.
- Each current record stores tenant/key hashes, actor, operation, payload digest, completion time, audit reference and the serialized response. Record size therefore scales primarily with the stored response; order responses may be much larger than compact inventory responses. No meaningful p50/p95 size can be inferred from an empty database.
- Illustrative capacity only: at an assumed average of 1 KiB per record, 10,000 completed operations/day would add about 10 MiB/day or 3.6 GiB/year of raw BSON before indexes, replicas, journal, backups and storage overhead. This is an arithmetic scenario, not a measured FactoryOS rate/size.

### Recommendation

1. Keep no TTL deletion until product/operations approve policy; current implementation intentionally has no idempotency TTL.
2. Preserve a compact permanent idempotency tombstone (tenant/key hash, actor, operation, payload hash, resource reference and result digest) for as long as replaying the key could repeat a financial/stock effect.
3. If large response bodies need expiry, consider separately archiving/compacting the response after an approved retry window while retaining the tombstone and a deterministic way to return the final resource state. This changes replay semantics and needs client contract tests and approval.
4. Instrument daily record counts, BSON p50/p95/p99 by operation, index size, write rate and duplicate-replay age in staging. Estimate real capacity from representative traffic before choosing retention.
5. Define legal/audit backup retention separately from application idempotency retention. Never remove a key record solely to save storage without assessing delayed client retries and duplicate-effect risk.

**Approval needed:** whether permanent tombstones are acceptable, how long full response bodies must be replayable, and whether compaction/archival may replace exact original-response replay. No TTL was introduced.

## Approval decisions requested

1. Approve or revise the proposed department field-visibility matrix before any redaction implementation.
2. Name an owner to provide production Mongo topology, transaction and failover evidence; no production connection will be made by engineering for this check.
3. Provide the deployed API client/integration inventory or identify its owner before release enforcement.
4. Decide the required durability/retention and outage behavior for failed-login/security-denial events.
5. Select email provider, payload encryption/key management, retry/dead-letter operations and retention for outbox implementation.
6. Decide idempotency tombstone and full-result retention/compaction policy; TTL remains disabled.
