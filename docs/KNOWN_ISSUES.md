# FactoryOS known issues

Last updated: 2026-11-12

| ID | Severity | Status | Issue / evidence | Next action |
|---|---|---|---|---|
| AUTH-001 | High | IMPLEMENTED, TESTED | Bounded refresh retry uses UUID request ID, encrypted rotation result, 30-second eligibility and server revalidation. Dedicated Mongo probe 15/15; full integration 64/64. | Production secret stability and transaction topology remain deployment gates; review bounded bearer replay window during final security signoff. |
| AUTH-003 | High | RESOLVED, TESTED | New distinct-request-ID concurrent old-token race test passed 1/1 against the isolated Mongo replica set. It asserts one winner, one rejected replay, and revocation of the winning successor and access session. It ran separately from the prior focused 4/4 suite. | Keep replay behavior unchanged; run combined focused suite only after relevant auth changes. |
| AUD-001 | High | IMPLEMENTED ON MIGRATED ROUTES, TESTED | Required audit shares transactions with selected operational and successful security state mutations; injected failure aborted inventory stock change and returned 503. Final full suite 64/64; auth probe 15/15. | Failed-login/generic request audit remains best-effort; production transaction topology unverified. |
| INV-001 | High | RESOLVED, TESTED | Conditional atomic adjustment and transaction preserve stock/consumption under concurrency; full suite includes regressions. | Keep existing behavior; no inventory business rules changed. |
| PUR-001 | High | RESOLVED, TESTED | Purchase receipt transition and stock credit are transactional; duplicate/concurrent receipt tested. | Production transaction support remains unverified. |
| ORD-001A | High | RESOLVED, TESTED | Concurrent payments now preserve all payment entries and balance; idempotency binds tenant/actor/operation/payload. | Migrate other selected routes only under reviewed contract. |
| PROD-001 | High | RESOLVED, TESTED | Concurrent transfer quantity lineage regression passed on isolated replica set. | Factory-specific quantity and rework rules remain unresolved. |
| FG-001 | High | RESOLVED, TESTED | Concurrent ready/dispatch transitions move stock once; idempotent original-result replay passed. | Production release depends on topology and recovery gates. |
| MUT-001 | High | IMPLEMENTED ON SELECTED ROUTES, TESTED | Inventory adjustment, purchase create/update/receipt, payment, transfer, ready and dispatch require `Idempotency-Key`; 64-test integration suite passes. No TTL is installed. | External integrations/Excel clients cannot be inventoried from this repo; inventory and update them before release enforcement. Define retention before cleanup. |
| TEN-001 | High | RESOLVED, TESTED | Two-company synthetic tests verify cross-tenant read/write denial and active membership switching. | Continue tenant-scoped query review for routes outside the tested set. |
| DEPT-001 | High | AWAITING PRODUCT APPROVAL | Department-filtered record lists may still return full order/production fields. A proposed matrix is documented in `PHASE_1_2_REMAINING_GATES.md`; no redaction is implemented. | Product/data owners approve or revise field-level visibility by department; then implement and add response tests. |
| SEC-002 | Medium | PARTIAL | Login/rate limiting, reset and invitation flows have coverage; latest auth DB probe passed 15/15. Not all reset/invitation expiry/revocation and abuse cases have independent stress coverage. | Extend only where risk remains after threat review; SMTP delivery is untested. |
| DEPLOY-001 | High | BLOCKED BY DEPLOYMENT EVIDENCE | Test CORS/session/transaction setup does not establish actual production settings. | Obtain exact CORS origins, trusted proxy, edge rate limits, SMTP, Mongo topology, backup/restore and revocation requirements. |
| DEPLOY-MONGO-001 | High | BLOCKED | No production topology manifest or approved evidence is present. README describes local development Mongo only. | Deployment owner provides Mongo version/topology, transaction limits, write concern, failover and restore evidence; no production connection from engineering. |
| CLIENT-001 | High | BLOCKED BY EXTERNAL INVENTORY | No frontend clients call covered mutations, and no Excel workflow is implemented here; external callers are unknown. | Product/integration owner inventories gateway logs, scripts, macros, mobile/third-party clients and coordinates key support before release. |
| AUD-002 | Medium | NEEDS OWNER DECISION | Failed-login and generic middleware audit calls are best-effort; rate-limited login attempts can return before an audit row. | Decide durable security-event sink, retention, monitoring and failure behavior. Critical successful mutations remain transaction-coupled. |
| MAIL-001 | Medium | DESIGN PREPARED, NOT IMPLEMENTED | Email delivery runs after commit and may fail without a durable retry row; outbox design is documented. | Approve provider, encrypted payload key, retries/dead-letter monitoring and retention. |
| E2E-001 | Medium | IMPLEMENTED, TESTED | Desktop Chrome two-tab refresh E2E passed 1/1 after reproducing and fixing an extra refresh request. API calls are stubbed. | Run broader browser/OS coverage in CI; server behavior is separately covered by Mongo tests. |
| RET-001 | Medium | AWAITING POLICY | Isolated clean DB baseline is empty (0 records/0 BSON bytes), so it cannot estimate production growth. | Approve idempotency tombstone/full-result retention and compaction policy; keep TTL disabled meanwhile. |
| MOCK-001 | Low | KNOWN TEST-RUNNER SCOPE | Full mock lane collected integration-only fixtures: 31 passed and 33 errors. Intended pure mock files passed 31/31. | Keep mock and integration test selections separate or add explicit collection markers. |
| REPO-001 | Low | BLOCKED BY APPROVAL GATE | No `.git` directory exists; full historical diff cannot be verified. | Product owner may approve source-control setup separately; do not initialize Git as part of this task. |

## Verification limits

- Latest full integration result: **64 passed, 0 failed, 0 skipped** against synthetic data in `factoryos_security_test`.
- Latest pure mock result: **31 passed, 0 failed, 0 skipped**. Broad mock invocation errors are documented and are not represented as passed or skipped.
- No attachment routes were found; unauthorized attachment access was not applicable to current code.
- No production or demo database was accessed. The isolated Mongo container, internal network, credentials and persistent volume remain intact.
- Phase 1.2 remains unapproved; no production readiness claim is made.
- Source-control diff review remains unavailable because this workspace has no `.git` directory; current test/doc content was inspected directly. Docker isolation was verified using the bundled Docker Desktop CLI. The isolated test fixtures and persistent volume were preserved.
