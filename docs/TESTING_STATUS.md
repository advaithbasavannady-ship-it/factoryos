# FactoryOS testing status

Last updated: 2026-11-12

## Latest verification

The focused refresh suite ran 4/4 before the fifth case existed. The new regression `test_concurrent_old_token_with_different_request_ids_revokes_family` was subsequently run separately against the isolated Mongo replica set and passed 1/1; all five cases therefore have pass evidence across two invocations, but were not run together.

| Lane/check | Result | Environment and notes |
|---|---|---|
| Full backend integration suite | **64 passed, 0 failed, 0 skipped** | Prior complete actual HTTP/backend run against isolated MongoDB and synthetic data; it predates the new regression. Command: `docker compose -p factoryos-phase12 -f /private/tmp/factoryos-phase12/compose.yaml --env-file /private/tmp/factoryos-phase12/compose.env --env-file /private/tmp/factoryos-phase12/test-run.env --profile tests run --rm test-runner` |
| Focused server-backed refresh suite | **4 passed, 0 failed, 0 skipped** | Rerun against isolated Mongo after browser E2E. Includes two independent clients sending the same old cookie/request ID concurrently, replay, cross-session ID reuse, and logout invalidation |
| Distinct-ID concurrent refresh replay | **1 passed, 0 failed, 0 skipped** | Command: `/Applications/Docker.app/Contents/Resources/bin/docker compose -p factoryos-phase12 -f /private/tmp/factoryos-phase12/compose.yaml --env-file /private/tmp/factoryos-phase12/compose.env --env-file /private/tmp/factoryos-phase12/test-run.env --profile tests run --rm test-runner tests/test_auth_refresh_concurrency.py::test_concurrent_old_token_with_different_request_ids_revokes_family`. One rotation succeeded, the distinct-ID request was rejected, and family/session revocation assertions passed. Pytest emitted a non-fatal cache warning because the backend mount is read-only. |
| Refresh database probe | **15 passed, 0 failed** | Current rerun verifies login, rotation, persisted encrypted same-ID concurrent duplicate result, unchanged active family, sequential replay revocation, expiration, stale duplicate, logout, password reset and audit persistence |
| Pure mock/unit files | **31 passed, 0 failed, 0 skipped** | `test_demo_seed_safety.py`, `test_permissions.py`, `test_rate_limit.py`; mock lane strips Mongo settings |
| Frontend TypeScript | Passed | `npm run typecheck` in `frontend/` |
| Frontend lint | Passed | `npm run lint` in `frontend/` |
| Browser refresh E2E | **1 passed** | Playwright desktop Chrome, two real tabs, all API calls stubbed; validates cross-tab coordination only |
| Idempotency growth baseline | **0 records, 0 BSON bytes** | Read-only count in cleaned isolated test DB; not production growth evidence |
| Required-audit failure injection | Passed within full integration suite | Sentinel duplicate `_id` forces audit insert failure; API returns 503, business stock remains unchanged; retry commits once and audit row exists |
| Mutation concurrency and tenant checks | Passed within full integration suite | Inventory, purchase receipt, payment, transfer, ready/dispatch, department, tenant and idempotency coverage |
| Python syntax compilation | Passed | `python3 -m py_compile` over modified auth, audit, idempotency, DB, route and test modules |
| Container boundary check | Passed | `factoryos-phase12-mongo` running; `27017/tcp` has no host binding; only internal `factoryos-phase12-isolated`; named test volume mounted |

## Attempts that did not pass

- Broad mock-lane collection over all tests: **31 passed, 33 errors**, 0 skipped. Those errors were integration-only tests using the live-backend fixture while `FACTORYOS_TEST_LANE=mock`; they were rerun as the intended three pure mock files and passed 31/31. They are not counted as passes or skips.
- One full-suite invocation stopped at runner preflight because the synthetic runner access token in `/private/tmp/factoryos-phase12/test-run.env` was stale after fixture recreation. The test-only token was refreshed from the isolated runner token file without printing it; the subsequent full suite completed 64/64.
- First auth-probe invocation after the integration suite failed before completing because the test account was rate limited. A cleanup/retry then hit a unique email from an earlier probe attempt. Pinned synthetic account cleanup was added to the helper mount; the clean dedicated probe then passed 15/15.
- An earlier refresh probe version passed 14 checks before an additional expired-window/current-successor case was added. The latest run is 15/15.
- First two-tab browser run reproduced two simultaneous refresh HTTP requests despite Web Locks. The E2E regression remained; client now requests a fresh in-memory access token from the peer tab after acquiring the lock. Final desktop run passed 1/1. A follow-up assertion initially expected exactly two session calls, but React StrictMode issues repeated session reads; the assertion now checks all calls use the shared bearer token.
- During full integration, the audit failure test intentionally triggered a duplicate audit `_id`; application logged the injected persistence failure, returned 503 and test assertions passed. This expected log is not a failed test.
- Pytest emitted an asyncio fixture-loop deprecation warning and cache-write warning because the backend is mounted read-only in the runner. Tests still passed.

## Coverage and limits

- No test was skipped in the latest full integration suite.
- Real MongoDB transaction behavior was exercised on the isolated single-node replica set. Production topology, transaction/failover behavior and indexes are not verified for deployment.
- Concurrent refresh same-request ID is tested at HTTP level. Server-side replay/revocation, time-window expiry, session expiry and current-successor cases are in the dedicated Mongo probe/tests. Desktop browser E2E was run; broader browser/OS coverage remains untested.
- A desktop Chrome Playwright E2E now exercises the two-tab client path with API interception. It does not call the backend; server security remains established by separate real-Mongo tests. Mobile project was not run.
- Required audit failure is tested for an idempotent inventory mutation. Transactional audit coverage also wraps selected order lifecycle/customer, production, attendance, machine/maintenance, schedule and company-update writes; every route does not yet have an injected audit-failure test.
- Successful login/session, password-reset token issuance/completion, logout/company switch, invitation and membership state changes, plus refresh rotation/replay revocation, now couple to audit writes. Failed-login/security-denial and generic middleware events remain best-effort. Post-commit email delivery has no durable outbox.
- Repository search found no current frontend calls to the covered inventory/purchasing/payment/transfer/finished-goods APIs and no Excel import/export flow. Independently deployed clients and external integrations were not discoverable.
- The empty `idempotency_records` test baseline measured 0 bytes after fixture cleanup, so no representative record-size distribution or production growth rate is available.
- No SMTP send, file attachment authorization (no attachment routes found), production config verification, backup/restore drill or production Mongo connection was performed.
- The suite seeder refused to overwrite existing synthetic company fixtures. A guarded helper created the dedicated runner/password-reset test identity; no test data cleanup was run. The isolated container/network/credentials/volume were preserved.

## Evidence commands

- Real integration suite: named Compose command above; **64 passed, 0 failed, 0 skipped**.
- Refresh database probe: named `factoryos-phase12` Compose project with `auth-probe.override.yaml`, `auth-harness.env` and `auth_db_probe.py`; **15 passed, 0 failed**.
- Mock tests: named Compose project with `mock-runner.override.yaml`, `mock.env`, selecting the three pure test files; **31 passed, 0 failed, 0 skipped**.
- Frontend: `npm run typecheck && npm run lint` from `frontend/`.
- Python compile: `python3 -m py_compile` over the modified Python files.
- New refresh regression syntax: `python3 -m py_compile backend/tests/test_auth_refresh_concurrency.py` passed.

## Approval state

Phase 1.2 remains **unapproved**. Remaining critical gates include production topology evidence, security-sensitive audit scope, external client inventory/compatibility and approved department field visibility.
