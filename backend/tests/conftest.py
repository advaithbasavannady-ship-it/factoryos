"""Pytest configuration and fixtures for the isolated FactoryOS test backend."""

import os
from urllib.parse import urlsplit


_DEMO_ENV_NAMES = (
    "ENABLE_DEMO_SEED", "DEMO_MONGO_URL", "DEMO_DB_NAME", "DEMO_COMPANY_ID",
    "DEMO_DEPLOYMENT_ID", "DEMO_ALLOWED_HOSTS", "DEMO_SOCKET_TIMEOUT_MS",
)
_DB_NAME_ENV_NAMES = {"DB_NAME", "TEST_DB_NAME", "DEMO_DB_NAME", "DATABASE_NAME"}


def _sanitize_mock_environment() -> None:
    """Prevent dotenv or demo settings from restoring database access in mock mode."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name in tuple(os.environ):
        if "MONGO" in name.upper() or "MONGODB" in name.upper():
            os.environ.pop(name, None)
    for name in _DEMO_ENV_NAMES:
        os.environ.pop(name, None)


def _sanitize_integration_pytest_environment() -> None:
    """Keep database and signing credentials in the runner/backend, not pytest."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name in tuple(os.environ):
        upper = name.upper()
        if (
            "MONGO" in upper
            or "MONGODB" in upper
            or upper in _DB_NAME_ENV_NAMES
            or upper.endswith("_DB_NAME")
            or upper.startswith("DB_")
            or upper.startswith("DATABASE")
            or upper == "TEST_JWT_SECRET"
        ):
            os.environ.pop(name, None)


# conftest is loaded before test collection; sanitize before test modules import
# application code that may call load_dotenv or create a Motor client.
if os.environ.get("FACTORYOS_TEST_LANE") == "mock":
    _sanitize_mock_environment()
elif os.environ.get("FACTORYOS_TEST_LANE") == "integration":
    _sanitize_integration_pytest_environment()

import httpx
import pytest
import pytest_asyncio

BACKEND_URL = os.environ.get("BACKEND_URL")
API_URL = f"{BACKEND_URL}/api" if BACKEND_URL else None
TEST_SERVER_MARKER = "factoryos-isolated-test-server-v1"


def _require_lane() -> str:
    lane = os.environ.get("FACTORYOS_TEST_LANE")
    if lane not in {"mock", "integration"}:
        raise RuntimeError("FACTORYOS_TEST_LANE must be set to either 'mock' or 'integration'")
    return lane


def _validate_jwt_secret(value: str, *, name: str) -> str:
    secret = (value or "").strip()
    if not secret:
        raise RuntimeError(f"{name} is required for the selected test lane")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError(f"{name} must be at least 32 bytes")
    placeholder_values = {
        "dev-change-me",
        "change-me",
        "example-secret",
        "your-jwt-secret",
        "your-secret-key",
        "test-secret",
    }
    if secret.lower() in placeholder_values:
        raise RuntimeError(f"{name} must not be a known placeholder")
    return secret


def _require_mock_configuration() -> None:
    """Mock lane: explicit mock selection, valid JWT_SECRET, and no Mongo-backed env leakage."""
    if os.environ.get("FACTORYOS_TEST_LANE") != "mock":
        raise RuntimeError("FACTORYOS_TEST_LANE must be set to 'mock' for mock-mode startup")
    if os.environ.get("DB_NAME") != "factoryos_security_test":
        raise RuntimeError("The mock lane requires DB_NAME=factoryos_security_test")
    secret = os.environ.get("JWT_SECRET")
    if secret is None:
        raise RuntimeError("JWT_SECRET is required for FACTORYOS_TEST_LANE=mock and must be supplied explicitly")
    _validate_jwt_secret(secret, name="JWT_SECRET")
    _sanitize_mock_environment()


def _require_test_configuration() -> None:
    """Require HTTP test configuration while rejecting DB credentials in pytest."""
    lane = _require_lane()
    if lane != "integration":
        return
    # The runner validates database credentials for itself and the backend child.
    # Pytest validates only its HTTP target and test token; it receives no DB URI.
    from run_isolated import validate_pytest_environment

    try:
        validate_pytest_environment()
    except ValueError as exc:
        raise RuntimeError(f"Invalid pytest HTTP configuration: {exc}") from None
    _validate_jwt_secret(os.environ.get("JWT_SECRET", ""), name="JWT_SECRET")
    token = os.environ.get("TEST_ACCESS_TOKEN", "")
    if not token.strip():
        raise RuntimeError("TEST_ACCESS_TOKEN is required for the isolated integration lane")


def _require_child_target_configuration() -> tuple[str, str, int]:
    """Require the runner-owned loopback endpoint and expected child identity."""
    if BACKEND_URL is None:
        raise RuntimeError("BACKEND_URL must be set by the isolated test runner")
    parsed = urlsplit(BACKEND_URL)
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None:
        raise RuntimeError("integration BACKEND_URL must target the runner's 127.0.0.1 child socket")
    if parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise RuntimeError("integration BACKEND_URL has an invalid form")
    run_id = os.environ.get("FACTORYOS_TEST_RUN_ID", "").strip()
    raw_pid = os.environ.get("FACTORYOS_TEST_CHILD_PID", "").strip()
    try:
        child_pid = int(raw_pid)
    except ValueError:
        raise RuntimeError("runner-provided test child identity is missing or invalid") from None
    if not run_id or child_pid <= 0:
        raise RuntimeError("runner-provided test child identity is missing or invalid")
    return BACKEND_URL.rstrip("/"), run_id, child_pid


def _verify_backend_identity() -> None:
    """Independently verify the child before any HTTP fixture is yielded."""
    base_url, run_id, child_pid = _require_child_target_configuration()
    try:
        response = httpx.get(f"{base_url}/__test__/identity", timeout=5.0)
        response.raise_for_status()
        identity = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("isolated test backend identity request failed") from None
    expected = {
        "marker": TEST_SERVER_MARKER,
        "run_id": run_id,
        "database_name": "factoryos_security_test",
        "pid": child_pid,
    }
    if identity != expected:
        raise RuntimeError("HTTP target identity does not match this pytest child configuration")


# Run at pytest startup, before collection and fixture execution can trigger any DB-backed work.
def pytest_configure(config):
    _require_lane()
    lane = os.environ.get("FACTORYOS_TEST_LANE")
    if lane == "mock":
        _require_mock_configuration()
    else:
        _require_test_configuration()
        _require_child_target_configuration()


def api_url(path: str = "") -> str:
    """Absolute URL for an /api route when an explicit integration backend URL is configured."""
    if API_URL is None:
        raise RuntimeError("BACKEND_URL is required for the integration lane and is not defaulted")
    return f"{API_URL}{path}"


@pytest.fixture(scope="session")
def backend_url() -> str:
    _verify_backend_identity()
    return BACKEND_URL.rstrip("/")


@pytest.fixture
def client():
    """Sync httpx client rooted at /api — the default for endpoint tests.

    Example:
        def test_status(client):
            assert client.get("/status").status_code == 200
    """
    if os.environ.get("FACTORYOS_TEST_LANE") != "integration":
        raise RuntimeError(
            "Live-backend HTTP fixtures require FACTORYOS_TEST_LANE=integration with valid isolated settings. "
            "Mock/unit tests must not use the live-backend client fixtures."
        )
    if BACKEND_URL is None:
        raise RuntimeError("BACKEND_URL is required for the integration lane and is not defaulted")
    _require_test_configuration()
    _verify_backend_identity()
    with httpx.Client(base_url=f"{BACKEND_URL.rstrip('/')}/api", timeout=30.0) as c:
        yield c


@pytest_asyncio.fixture
async def aclient():
    """Async HTTP client variant for integration tests that use async requests."""
    if os.environ.get("FACTORYOS_TEST_LANE") != "integration":
        raise RuntimeError(
            "Live-backend HTTP fixtures require FACTORYOS_TEST_LANE=integration with valid isolated settings. "
            "Mock/unit tests must not use the live-backend client fixtures."
        )
    if BACKEND_URL is None:
        raise RuntimeError("BACKEND_URL is required for the integration lane and is not defaulted")
    _require_test_configuration()
    _verify_backend_identity()
    async with httpx.AsyncClient(base_url=f"{BACKEND_URL.rstrip('/')}/api", timeout=30.0) as c:
        yield c


# --- app-specific fixtures below this line ---
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_sys.path.insert(0, str(_Path(__file__).parent))
from helpers import REPEAT_CUSTOMER_ID, auth_headers  # noqa: E402


@pytest.fixture
def fresh_customer(client):
    """A brand-new customer for security tests. Requires explicit bearer auth and factoryos_security_test."""
    import uuid
    token = os.environ["TEST_ACCESS_TOKEN"]
    name = f"tscheck-cust-{uuid.uuid4().hex[:8]}"
    headers = auth_headers(token)
    r = client.post(
        "/customers",
        json={
            "name": name,
            "contact_person": "Test Contact",
            "email": "t@test.demo",
            "phone": "+1 555-000-0000",
            "city": "Testville",
            "industry": "Testing",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()
