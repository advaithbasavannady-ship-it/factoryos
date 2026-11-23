# FactoryOS test lanes

FactoryOS uses two explicit pytest lanes:

- `FACTORYOS_TEST_LANE=mock`: mock/unit tests only. These tests require `DB_NAME=factoryos_security_test` and a dedicated test-only `JWT_SECRET` of at least 32 bytes. They do not require `TEST_ACCESS_TOKEN` or `TEST_MONGO_URL`, and they must not use the live-backend HTTP fixtures.
- `FACTORYOS_TEST_LANE=integration`: database-backed integration tests through the isolated backend HTTP API. The runner/backend child require `TEST_JWT_SECRET`, `TEST_MONGO_URL`, and `TEST_ACCESS_TOKEN`; pytest receives the access token, a mapped test-only `JWT_SECRET` needed by demo-safety import tests, and HTTP child identity settings, but no MongoDB URI or database name.

The lane must be selected explicitly. Missing or unknown values are rejected before pytest collection.

## Mock lane

Use the mock lane for the existing demo-safety test suite and other standalone, non-HTTP tests. The user must supply their own test-only `JWT_SECRET` value; do not reuse a fixed secret from this repository.

```bash
cd backend
DB_NAME=factoryos_security_test \
FACTORYOS_TEST_LANE=mock \
PYTHON_DOTENV_DISABLED=1 \
JWT_SECRET='replace-with-your-own-test-only-secret-at-least-32-bytes' \
python -m pytest tests/test_demo_seed_safety.py -q
```

`JWT_SECRET` must be a dedicated test-only value, at least 32 bytes, and must not be a production or placeholder secret.
The isolated runner also sets `PYTHON_DOTENV_DISABLED=1`, removes `MONGO_URL`, `TEST_MONGO_URL`, and demo-seeding settings before pytest starts, and conftest repeats that sanitation before test collection. Mock tests must use database mocks; imports that require a configured MongoDB URL should fail rather than load one from `backend/.env`.

## Integration lane

The integration runner starts and owns a guarded test backend child. Do not set `BACKEND_URL` yourself; the runner removes any inherited value, binds an already-open socket to `127.0.0.1`, passes it to the child, and supplies the child URL to pytest only after identity and session preflight succeed.

```bash
cd backend
FACTORYOS_TEST_LANE=integration \
TEST_JWT_SECRET='replace-with-your-own-test-only-secret-at-least-32-bytes' \
TEST_MONGO_URL='mongodb://test-user:secret@mongo.example.invalid:27017' \
TEST_ACCESS_TOKEN='replace-with-a-token-issued-for-the-test-user' \
python tests/run_isolated.py -q
```

The runner receives and validates `TEST_JWT_SECRET` and `TEST_MONGO_URL`, and checks that the caller's generic credentials are not the same values. It creates a unique run ID and launches `tests/server.py` as a child process on the inherited loopback socket. The backend child receives the explicit test values and their mapped `MONGO_URL`/`JWT_SECRET` settings, plus the fixed `DB_NAME=factoryos_security_test`. The pytest subprocess receives the validated test secret as `JWT_SECRET` because `test_demo_seed_safety.py` imports `authz` through `server.py`; it does not receive `TEST_JWT_SECRET`. Generic application credentials are never fallbacks. The runner compares the child's identity response (marker, run ID, database name, and PID) with the process it launched, then verifies `TEST_ACCESS_TOKEN` against the child's read-only `/api/session` endpoint before starting pytest. Identity and session preflight reject redirects and require responses from the original loopback origin.

The pytest subprocess receives only operating-system runtime variables plus `FACTORYOS_TEST_LANE`, `TEST_ACCESS_TOKEN`, test-only `JWT_SECRET`, `BACKEND_URL`, run ID, child PID, and `PYTHON_DOTENV_DISABLED=1`. It does not receive `MONGO_URL`, `TEST_MONGO_URL`, any other Mongo-named environment variable, database-name variables, or the raw `TEST_JWT_SECRET`. Conftest repeats database-variable sanitation before test collection and validates the HTTP-only environment. The tests interact with the backend through HTTP; the backend child performs the database operations.

The test server disables python-dotenv using its supported `PYTHON_DOTENV_DISABLED=1` setting, clears demo-seeding environment variables before application imports, validates that the inherited listener is an already-listening IPv4 TCP socket bound to `127.0.0.1`, performs a read-only MongoDB ping before readiness, and does not schedule index creation. Its company router omits `POST /api/demo/reset`; other company routes keep their existing dependencies. The pytest `client`, `aclient`, and `backend_url` fixtures independently verify the child identity before use. The runner forces integration tests to run serially (`-n 0`) and terminates and waits only for its own child process, including on preflight or pytest failure.

The ping and identity checks prove only that the test backend process received the intended run configuration and can reach its configured database server. They do not prove MongoDB role isolation or identify the physical deployment. A MongoDB administrator must separately provision the test identity with access limited to `factoryos_security_test` and no access to `factoryos` or `factoryos_demo`; this must be verified outside this runner. Never place production credentials in `TEST_MONGO_URL`.

The test users, active sessions, company memberships, and role-specific bearer tokens must be provisioned separately in the isolated test database. The current `TEST_ACCESS_TOKEN` setup does not replace the missing role-specific identities required by the existing role workflows. The `TEST_*` values are explicit test inputs, not fallbacks for generic application variables. The runner maps only the supplied test secret and URI into the child application's `JWT_SECRET` and `MONGO_URL` settings. It does not create MongoDB users or modify permissions, collections, indexes, or migrations. MongoDB server-side role isolation still requires independent administrator verification.
