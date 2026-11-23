# FactoryOS autonomous execution

Last updated: 2026-11-12

## Current checkpoint

- Phase: Phase 1.2 reliability and approval gates; Phase 1.2 is **not approved**.
- Repository root: `/Users/advaithdy/Desktop/factoryos`.
- Source control: no `.git` directory exists. No diff-based clean/dirty status is available; do not initialize Git without approval.
- Local instructions: no workspace `AGENTS.md` found in the checked locations.
- Isolated MongoDB: `factoryos-phase12-mongo` running on internal network `factoryos-phase12-isolated`, database `factoryos_security_test`, replica set `factoryos-phase12-rs`, named persistent volume `factoryos-phase12-mongo-data`; container has no published ports. Preserve all resources and credentials.
- Prior cleanup removed synthetic probe records. In the current session, the suite seeder found pre-existing synthetic company fixtures and refused to overwrite them. No suite data was deleted; isolated infrastructure remains.
- Current verification: real MongoDB integration **64 passed, 0 failed, 0 skipped** (prior complete run); original server-backed refresh concurrency **4 passed**, plus the new distinct-ID replay regression **1 passed in a separate run**; refresh DB probe **15 passed, 0 failed**; pure mock suite **31 passed, 0 failed**; desktop Chrome refresh E2E **1 passed**; frontend typecheck and lint passed.
- Docker access restored through `/Applications/Docker.app/Contents/Resources/bin/docker`; selected context was `desktop-linux` at the local Docker Desktop socket. Container inspection confirmed Mongo is running only on `factoryos-phase12-isolated`, has no published ports (`27017/tcp: null`), and mounts existing volume `factoryos-phase12-mongo-data`.
- New `AUTH-003` regression ran against the isolated replica set and passed **1/1**. The fixture seeder refused to overwrite existing synthetic company fixtures. The runner-session helper created its dedicated synthetic runner/reset records; test records were not removed, and Docker infrastructure/volume were preserved.
- Source control remains unavailable for diff review because this workspace snapshot has no `.git` directory. Current test and listed documentation were reviewed directly; `py_compile` passed for the test module.

## Completed and verified

- `AUTH-001`: implemented browser same-tab and cross-tab single-flight plus request-ID-bound encrypted refresh result. Server enforces a 30-second logical duplicate window, revalidates session/user/membership/auth version, preserves sequential replay family revocation, and rejects stale/revoked/expired restoration. Dedicated Mongo probe passed 15/15; focused tests are included in 64/64 full integration.
- `AUD-001`: required audit writes now share Mongo transactions with idempotent high-risk mutations and selected order, production, attendance, machine, schedule, customer, company, login/session, password-reset, invitation and membership mutations. Injected audit insert failure returned 503 and aborted the inventory change; retry succeeded with one mutation and persisted audit event. This guarantee is limited to migrated routes and transaction-capable deployment topology.
- `MUT-001`: idempotency is required on inventory adjustment, purchase create/update/receipt, payment, order transfer, ready and dispatch. Key hash and result bind tenant, actor, operation and canonical payload. Same retries return original results; mismatches conflict. Records have no TTL. High-risk route regressions passed in full Mongo tests.
- `VER-001`: isolated integration suite 64/64; includes auth refresh concurrency, tenant isolation, audit failure injection, inventory, purchasing, payment, transfer, production and finished goods concurrency.
- `VER-002`: pure mock files (`test_demo_seed_safety.py`, `test_permissions.py`, `test_rate_limit.py`) passed 31/31.
- `VER-003`: frontend `npm run typecheck` and `npm run lint` passed.
- `E2E-001`: Playwright two-tab refresh test first reproduced duplicate refresh HTTP requests despite Web Locks; browser client now performs a short in-memory BroadcastChannel token handoff under the lock. Final desktop Chrome test passed 1/1; all API calls were stubbed.
- `RET-001`: read-only isolated test DB baseline is 0 idempotency records / 0 BSON bytes after cleanup. This is not a production growth estimate; recommendation is in `PHASE_1_2_REMAINING_GATES.md`.
- `AUTH-002`: reran same-request concurrent HTTP refresh tests against isolated Mongo (4/4) and the direct Mongo auth probe (15/15). The test confirms two server requests return the same rotation; the probe verifies one bound refresh result, active session, encrypted secret storage and no family revocation.
- `AUTH-003`: `test_concurrent_old_token_with_different_request_ids_revokes_family` ran against real MongoDB in `factoryos_security_test`: **1 passed**. It observed one successful rotation, one unauthorized distinct-ID replay, and verified the winning access session and successor were revoked. The prior focused 4/4 result plus this separately executed fifth test covers all five cases across two invocations.
- `INV-001`, `PUR-001`, `ORD-001A`, `PROD-001`, `FG-001`, `TEN-001`: previous reproduced concurrency/isolation defects remain covered by successful integration regressions; see `TESTING_STATUS.md`.

## Queue and remaining gates

1. `DEPLOY-001` — BLOCKED BY DEPLOYMENT EVIDENCE, P1: repository contains only local development Mongo notes; production topology, transaction/failover, CORS, trusted proxy, edge rate limits, SMTP and backup/restore evidence are unavailable.
2. `MUT-001 rollout` — BLOCKED BY EXTERNAL CLIENT INVENTORY, P1: repository search found no frontend calls for covered operational APIs and no application Excel import/export. Deployed clients/integrations require an owner inventory before release enforcement.
3. `AUD-002` — NEEDS OWNER DECISION, P1: failed-login and generic middleware audit remain best-effort. Decide security-event durability, retention, monitoring and outage behavior.
4. `MAIL-001` — NEEDS OWNER DECISION, P1: durable outbox design prepared; choose email provider/idempotency, encryption key management, retry/dead-letter operations and retention before implementation.
5. `DEPT-001` — AWAITING PRODUCT APPROVAL, P1: proposed field-level visibility matrix prepared in `PHASE_1_2_REMAINING_GATES.md`; no redaction implemented.
6. `RET-001` — AWAITING PRODUCT/OPERATIONS DECISION, P1: no TTL; test DB is empty after cleanup. Approve full-result retention and tombstone/compaction behavior before any cleanup.
7. `E2E-001` — IMPLEMENTED, TESTED: desktop browser case passes; mobile/other OS coverage is not run.
8. `OWNER-001` — PROPOSALS PREPARED: concise alternatives, risks and recommendations for department visibility, security-event audit, email outbox, idempotency retention, production Mongo evidence and API-client inventory are in `PHASE_1_2_OWNER_DECISIONS.md`.

## Decisions and limits

- Product owner approved the bounded refresh design, transaction-backed audit direction and idempotency rollout/indefinite retention direction on 2026-11-12.
- Idempotency records have no TTL; retention cleanup requires a separately approved policy. Empty test DB size is not representative of deployed growth.
- The refresh response cache contains encrypted response bytes and has TTL cleanup; acceptance checks enforce 30 seconds independent of TTL monitor delay.
- Single-node test replica-set transaction success does not establish production topology support.
- No production or demo database was accessed. No migration, index installation or service deployment was run in production.
- A broad mock-lane run collected integration-only tests: 31 passed and 33 errored due to missing integration backend. The intended pure mock files were rerun and passed 31/31; the errors remain recorded in `TESTING_STATUS.md`.

## Next action

The focused test passed; do not repeat it absent a relevant auth change. Continue with independent safe Phase 1.2 gates. Existing isolated suite fixtures and new runner-session synthetic records remain intact. Do not run broad cleanup because it would remove pre-existing synthetic suite data.

Keep Phase 1.2 unapproved pending production deployment evidence, external client inventory, security-event policy, email outbox decisions, department visibility approval and idempotency retention decisions. There is no currently running test or incomplete process known to this session.
