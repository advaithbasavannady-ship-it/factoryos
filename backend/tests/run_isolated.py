"""Run pytest with explicit configuration and an owned integration backend child.

The runner verifies the child process identity and active test session before
pytest starts. It cannot prove MongoDB role isolation.
"""

from __future__ import annotations

import os
import json
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


SECURITY_DB_NAME = "factoryos_security_test"
TEST_SERVER_MARKER = "factoryos-isolated-test-server-v1"
_DEMO_ENV_NAMES = (
    "ENABLE_DEMO_SEED", "DEMO_MONGO_URL", "DEMO_DB_NAME", "DEMO_COMPANY_ID",
    "DEMO_DEPLOYMENT_ID", "DEMO_ALLOWED_HOSTS", "DEMO_SOCKET_TIMEOUT_MS",
)
_PLACEHOLDERS = {
    "dev-change-me",
    "change-me",
    "example-secret",
    "your-jwt-secret",
    "your-secret-key",
    "test-secret",
    "test-secret-change-me",
    "changeme",
    "password",
}
_PYTEST_RUNTIME_ENV_NAMES = {
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "LC_CTYPE",
    "SYSTEMROOT", "WINDIR", "__CF_USER_TEXT_ENCODING",
}
_PYTEST_DB_ENV_NAMES = {"DB_NAME", "TEST_DB_NAME", "DEMO_DB_NAME", "DATABASE_NAME"}
_PYTEST_JWT_SECRET_NAMES = {"TEST_JWT_SECRET"}


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirects())


def _require_lane(source: Mapping[str, str] | None = None) -> str:
    env = os.environ if source is None else source
    lane = env.get("FACTORYOS_TEST_LANE")
    if lane not in {"mock", "integration"}:
        raise ValueError("FACTORYOS_TEST_LANE must be set to 'mock' or 'integration'")
    return lane


def _required(source: Mapping[str, str], name: str) -> str:
    value = source.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required for the selected test lane")
    return value.strip()


def _validated_secret(source: Mapping[str, str], *, env_name: str) -> str:
    secret = _required(source, env_name)
    if secret.lower() in _PLACEHOLDERS:
        raise ValueError(f"{env_name} must not be a known placeholder")
    if len(secret.encode("utf-8")) < 32:
        raise ValueError(f"{env_name} must be at least 32 bytes")
    return secret


def _remove_demo_environment(env: dict[str, str]) -> None:
    for name in _DEMO_ENV_NAMES:
        env.pop(name, None)


def _remove_mongo_environment(env: dict[str, str]) -> None:
    """Remove all Mongo-named variables, including unrecognized URI aliases."""
    for name in tuple(env):
        if "MONGO" in name.upper() or "MONGODB" in name.upper():
            env.pop(name, None)


def build_test_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a child environment without falling back to app credentials.

    The mock lane preserves the explicitly supplied JWT_SECRET and strips Mongo
    settings entirely; the integration lane requires explicit TEST_* values.
    """
    original = dict(os.environ if source is None else source)
    lane = _require_lane(original)

    if lane == "mock":
        secret = original.get("JWT_SECRET", "").strip()
        if not secret:
            raise ValueError("JWT_SECRET is required for the mock lane")
        _validated_secret({"JWT_SECRET": secret}, env_name="JWT_SECRET")
        child = original.copy()
        _remove_mongo_environment(child)
        child["DB_NAME"] = SECURITY_DB_NAME
        child["PYTHON_DOTENV_DISABLED"] = "1"
        _remove_demo_environment(child)
        return child

    test_secret = _validated_secret(original, env_name="TEST_JWT_SECRET")
    test_mongo_url = _required(original, "TEST_MONGO_URL")
    test_access_token = _required(original, "TEST_ACCESS_TOKEN")
    if test_mongo_url == "__EPHEMERAL_MONGOD__" or not test_mongo_url.startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError("TEST_MONGO_URL must be a MongoDB URI")
    if test_access_token == "__MINT_ON_STARTUP__":
        raise ValueError("TEST_ACCESS_TOKEN must not use a sentinel placeholder")

    inherited_secret = original.get("JWT_SECRET", "").strip()
    if inherited_secret and inherited_secret == test_secret:
        raise ValueError("TEST_JWT_SECRET must be separately provisioned")
    inherited_mongo_url = original.get("MONGO_URL", "").strip()
    if inherited_mongo_url and inherited_mongo_url == test_mongo_url:
        raise ValueError("TEST_MONGO_URL must be separately provisioned")

    child = original.copy()
    _remove_mongo_environment(child)
    child.pop("JWT_SECRET", None)
    child.pop("TEST_JWT_SECRET", None)
    child["JWT_SECRET"] = test_secret
    child["TEST_JWT_SECRET"] = test_secret
    child["MONGO_URL"] = test_mongo_url
    child["TEST_MONGO_URL"] = test_mongo_url
    child["DB_NAME"] = SECURITY_DB_NAME
    child["PYTHON_DOTENV_DISABLED"] = "1"
    for name in _DEMO_ENV_NAMES:
        child.pop(name, None)
    # An inherited shared-backend URL must never survive integration setup.
    child.pop("BACKEND_URL", None)
    return child


def validate_test_environment(source: Mapping[str, str] | None = None) -> None:
    """Validate the Mongo/JWT configuration used by the runner and backend child."""
    env = os.environ if source is None else source
    lane = _require_lane(env)
    if lane != "integration":
        return
    test_secret = _validated_secret(env, env_name="TEST_JWT_SECRET")
    test_mongo_url = _required(env, "TEST_MONGO_URL")
    if test_mongo_url == "__EPHEMERAL_MONGOD__" or not test_mongo_url.startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError("TEST_MONGO_URL must be a MongoDB URI")
    test_access_token = _required(env, "TEST_ACCESS_TOKEN")
    if test_access_token == "__MINT_ON_STARTUP__":
        raise ValueError("TEST_ACCESS_TOKEN must not use a sentinel placeholder")
    if env.get("JWT_SECRET") != test_secret:
        raise ValueError("JWT_SECRET must match TEST_JWT_SECRET in the isolated test process")
    if env.get("MONGO_URL") != test_mongo_url:
        raise ValueError("MONGO_URL must match TEST_MONGO_URL in the isolated test process")
    if env.get("DB_NAME") != SECURITY_DB_NAME:
        raise ValueError(f"DB_NAME must be exactly {SECURITY_DB_NAME}")


def validate_pytest_environment(source: Mapping[str, str] | None = None) -> None:
    """Validate pytest's minimal HTTP-only environment without DB credentials."""
    env = os.environ if source is None else source
    if _require_lane(env) != "integration":
        raise ValueError("pytest HTTP configuration requires the integration lane")
    _required(env, "TEST_ACCESS_TOKEN")
    _validated_secret(env, env_name="JWT_SECRET")
    if env.get("PYTHON_DOTENV_DISABLED", "").lower() not in {"1", "true", "yes", "y", "t"}:
        raise ValueError("PYTHON_DOTENV_DISABLED=1 is required for the pytest process")
    forbidden = [
        name for name in env
        if "MONGO" in name.upper()
        or "MONGODB" in name.upper()
        or name.upper() in _PYTEST_DB_ENV_NAMES
        or name.upper().endswith("_DB_NAME")
        or name.upper().startswith("DB_")
        or name.upper().startswith("DATABASE")
        or name.upper() in _PYTEST_JWT_SECRET_NAMES
    ]
    if forbidden:
        raise ValueError("pytest environment must not contain database or JWT secret settings")


def _pytest_environment(
    source: Mapping[str, str], *, backend_url: str, run_id: str, child_pid: int
) -> dict[str, str]:
    """Build a small pytest environment with runtime essentials and HTTP identity only."""
    env = {name: value for name, value in source.items() if name in _PYTEST_RUNTIME_ENV_NAMES or name.startswith("LC_")}
    env.update({
        "FACTORYOS_TEST_LANE": "integration",
        # The mock demo-safety tests import authz through server.py; pass only
        # the validated test key, never the caller's generic key.
        "JWT_SECRET": _validated_secret(source, env_name="TEST_JWT_SECRET"),
        "TEST_ACCESS_TOKEN": _required(source, "TEST_ACCESS_TOKEN"),
        "BACKEND_URL": backend_url,
        "FACTORYOS_TEST_RUN_ID": run_id,
        "FACTORYOS_TEST_CHILD_PID": str(child_pid),
        "PYTHON_DOTENV_DISABLED": "1",
    })
    cred_override = source.get("TEST_ROLE_CREDENTIALS_FILE", "").strip()
    if cred_override:
        env["TEST_ROLE_CREDENTIALS_FILE"] = cred_override
    validate_pytest_environment(env)
    return env


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError("preflight URL has an invalid origin")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise RuntimeError("preflight URL has an invalid origin") from None
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _request_json(url: str, *, headers: Mapping[str, str] | None = None, timeout: float = 1.0) -> dict:
    request = Request(url, headers=dict(headers or {}))
    expected_origin = _origin(url)
    with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
        if _origin(response.geturl()) != expected_origin:
            raise RuntimeError("preflight response came from another origin")
        if response.status != 200:
            raise RuntimeError("test backend preflight returned a non-200 response")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("test backend preflight returned an invalid response")
    return payload


def _post_json(url: str, body: dict, *, headers: Mapping[str, str] | None = None, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    request = Request(url, data=data, headers=req_headers, method="POST")
    expected_origin = _origin(url)
    with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
        if _origin(response.geturl()) != expected_origin:
            raise RuntimeError("preflight response came from another origin")
        if response.status != 200:
            raise RuntimeError(f"preflight POST to {url} returned status {response.status}")
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("preflight response returned invalid payload")
    return payload


def _verify_identity(url: str, *, run_id: str, child_pid: int) -> None:
    identity = _request_json(url + "/__test__/identity")
    expected = {
        "marker": TEST_SERVER_MARKER,
        "run_id": run_id,
        "database_name": SECURITY_DB_NAME,
        "pid": child_pid,
    }
    if identity != expected:
        raise RuntimeError("test backend identity does not match the launched child")


def _wait_for_child_identity(url: str, process: subprocess.Popen, *, run_id: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    identity_url = url + "/__test__/identity"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("test backend child exited before readiness")
        try:
            _verify_identity(url, run_id=run_id, child_pid=process.pid)
            return
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise RuntimeError("test backend identity endpoint redirected") from None
            time.sleep(0.2)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            time.sleep(0.2)
    raise RuntimeError(f"test backend did not become ready at {identity_url}")


def _verify_active_test_session(url: str, token: str) -> None:
    request = Request(url + "/api/session", headers={"Authorization": f"Bearer {token}"})
    try:
        with _NO_REDIRECT_OPENER.open(request, timeout=10.0) as response:
            if _origin(response.geturl()) != _origin(url):
                raise RuntimeError("test session preflight response came from another origin")
            if response.status != 200:
                raise RuntimeError("test token was not accepted by the isolated backend")
            # Consume the body but do not log or persist user details.
            response.read()
    except (HTTPError, URLError, TimeoutError, OSError):
        raise RuntimeError("test token preflight failed against the isolated backend") from None


def _stop_child(process: subprocess.Popen) -> None:
    """Terminate and reap only the child process returned by this runner's Popen."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            # Continue to the exact Popen child; never signal a process group or
            # rediscover a process by port/PID.
            pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                if process.poll() is None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        raise RuntimeError("could not terminate the isolated backend child") from None
                    except OSError:
                        if process.poll() is None:
                            raise RuntimeError("could not wait for the isolated backend child") from None
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                raise RuntimeError("isolated backend child did not exit after kill") from None
            except OSError:
                if process.poll() is None:
                    raise RuntimeError("could not wait for the isolated backend child") from None
                try:
                    process.wait()
                except OSError:
                    raise RuntimeError("could not reap the isolated backend child") from None
    else:
        try:
            process.wait()
        except OSError:
            if process.poll() is None:
                raise RuntimeError("could not reap the isolated backend child") from None


def _run_mock_pytest(backend_dir: Path, env: dict[str, str], argv: list[str]) -> int:
    args = [sys.executable, "-m", "pytest", "-c", str(backend_dir / "tests" / "pytest.ini"), *argv]
    return subprocess.run(args, cwd=backend_dir, env=env, check=False).returncode


def _find_mongod() -> str:
    path = shutil.which("mongod")
    if path:
        return path
    for candidate in ("/opt/homebrew/bin/mongod", "/usr/local/bin/mongod"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("mongod executable not found on host")


def _start_ephemeral_mongod() -> tuple[subprocess.Popen, str, int, str]:
    mongod_path = _find_mongod()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    if port == 27017:
        raise RuntimeError("refusing default host MongoDB port 27017")

    mongo_tmpdir = tempfile.mkdtemp(prefix="factoryos_rs0_isolated_")
    log_path = os.path.join(mongo_tmpdir, "mongod.log")
    mongod_proc = subprocess.Popen(
        [
            mongod_path,
            "--replSet", "rs0",
            "--port", str(port),
            "--bind_ip", "127.0.0.1",
            "--dbpath", mongo_tmpdir,
            "--logpath", log_path,
        ],
        close_fds=True,
    )
    direct_url = f"mongodb://127.0.0.1:{port}/?directConnection=true"
    mongo_url = f"mongodb://127.0.0.1:{port}/?replicaSet=rs0&directConnection=true"

    client = None
    try:
        from pymongo import MongoClient

        client = MongoClient(direct_url, serverSelectionTimeoutMS=5000)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if mongod_proc.poll() is not None:
                raise RuntimeError("ephemeral mongod child exited before ping")
            try:
                client.admin.command("ping")
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise RuntimeError("ephemeral mongod timed out waiting for ping")

        client.admin.command("replSetInitiate", {
            "_id": "rs0",
            "members": [{"_id": 0, "host": f"127.0.0.1:{port}"}],
        })

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if mongod_proc.poll() is not None:
                raise RuntimeError("ephemeral mongod child exited before primary election")
            try:
                hello = client.admin.command("hello")
                if hello.get("isWritablePrimary"):
                    break
            except Exception:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("ephemeral mongod timed out waiting for primary election")
    except BaseException:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        _stop_child(mongod_proc)
        shutil.rmtree(mongo_tmpdir, ignore_errors=True)
        raise
    else:
        client.close()

    return mongod_proc, mongo_tmpdir, port, mongo_url


def _seed_initial_company_and_owner(mongo_url: str, jwt_secret: str) -> str:
    import datetime
    import jwt
    from pymongo import MongoClient

    client = MongoClient(mongo_url, serverSelectionTimeoutMS=5000)
    try:
        db = client[SECURITY_DB_NAME]
        company_id = "co-tscheck-primary"
        user_id = "usr-tscheck-owner"
        session_id = f"sess-tscheck-owner-{secrets.token_hex(8)}"
        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat(timespec="seconds")
        exp_iso = (now + datetime.timedelta(hours=2)).isoformat(timespec="seconds")

        db.companies.replace_one(
            {"id": company_id},
            {
                "id": company_id,
                "name": "FactoryOS Security Test Co",
                "departments": ["CAM", "Drilling", "Plating", "QC", "Machining", "Finishing", "Packaging"],
                "production_departments": ["Machining", "Finishing", "Packaging"],
                "status": "active",
            },
            upsert=True,
        )
        db.users.replace_one(
            {"id": user_id},
            {
                "id": user_id,
                "name": "Test Owner",
                "email": "owner@factoryos-test.invalid",
                "email_normalized": "owner@factoryos-test.invalid",
                "password_hash": "!test-owner-no-password!",
                "role": "owner",
                "status": "active",
                "auth_version": 1,
                "created_at": now_iso,
            },
            upsert=True,
        )
        db.company_members.replace_one(
            {"id": f"mem-{user_id}"},
            {
                "id": f"mem-{user_id}",
                "company_id": company_id,
                "user_id": user_id,
                "role": "owner",
                "status": "active",
                "department": None,
                "department_ids": [],
                "created_at": now_iso,
            },
            upsert=True,
        )
        db.sessions.replace_one(
            {"id": f"sdoc-{user_id}"},
            {
                "id": f"sdoc-{user_id}",
                "session_id": session_id,
                "user_id": user_id,
                "company_id": company_id,
                "issued_at": now_iso,
                "expires_at": exp_iso,
                "refresh_expires_at": exp_iso,
                "refresh_token_hash": secrets.token_hex(32),
                "refresh_token_history": [],
                "refresh_version": 1,
                "status": "active",
                "auth_version": 1,
            },
            upsert=True,
        )

        payload = {
            "sub": user_id,
            "company_id": company_id,
            "session_id": session_id,
            "role": "owner",
            "auth_version": 1,
            "iss": "factoryos",
            "aud": "factoryos",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + datetime.timedelta(hours=2)).timestamp()),
        }
        token = jwt.encode(payload, jwt_secret, algorithm="HS256")
        return token
    finally:
        client.close()


_SYNTHETIC_ACTORS: tuple[str, ...] = (
    "u-sales",
    "u-ops",
    "u-pm",
    "u-sup-machining",
    "u-sup-finishing",
    "u-sup-packaging",
)


def _write_credentials_file_0600(cred_file: str, credentials_data: dict[str, dict[str, str]]) -> None:
    fd = os.open(cred_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        file_obj = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with file_obj:
        json.dump(credentials_data, file_obj)


def _execute_isolated_backend_and_pytest(
    backend_dir: Path,
    env: dict[str, str],
    argv: list[str],
    *,
    before_session_check: Callable[[str, str], None] | None = None,
) -> int:
    validate_test_environment(env)
    run_id = uuid.uuid4().hex
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    process = None
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        host, port = listener.getsockname()
        backend_url = f"http://{host}:{port}"
        socket_fd = listener.fileno()

        child_env = env.copy()
        child_env.pop("BACKEND_URL", None)
        child_env.pop("TEST_ACCESS_TOKEN", None)
        child_env.pop("TEST_ROLE_CREDENTIALS_FILE", None)
        child_env["FACTORYOS_TEST_RUN_ID"] = run_id
        child_env["FACTORYOS_TEST_SOCKET_FD"] = str(socket_fd)
        child_env["PYTHON_DOTENV_DISABLED"] = "1"
        _remove_demo_environment(child_env)

        process = subprocess.Popen(
            [sys.executable, str(backend_dir / "tests" / "server.py")],
            cwd=backend_dir,
            env=child_env,
            close_fds=True,
            pass_fds=(socket_fd,),
        )
        listener.close()
        _wait_for_child_identity(backend_url, process, run_id=run_id)
        if before_session_check is not None:
            before_session_check(backend_url, run_id)
        _verify_active_test_session(backend_url, env["TEST_ACCESS_TOKEN"])

        pytest_env = _pytest_environment(
            env,
            backend_url=backend_url,
            run_id=run_id,
            child_pid=process.pid,
        )
        args = [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(backend_dir / "tests" / "pytest.ini"),
            *argv,
        ]
        return subprocess.run(args, cwd=backend_dir, env=pytest_env, check=False).returncode
    finally:
        listener.close()
        if process is not None:
            _stop_child(process)


def _run_integration_pytest(backend_dir: Path, env: dict[str, str], argv: list[str]) -> int:
    """Run pytest against an externally supplied isolated MongoDB and credentials."""
    original_env = dict(os.environ)
    try:
        return _execute_isolated_backend_and_pytest(backend_dir, env, argv)
    finally:
        os.environ.clear()
        os.environ.update(original_env)


def _run_native_ephemeral_pytest(
    backend_dir: Path,
    argv: list[str],
    *,
    source: Mapping[str, str] | None = None,
) -> int:
    """Explicit `--native-ephemeral` mode with ephemeral rs0 MongoDB and 6 random actors."""
    raw_source = dict(os.environ if source is None else source)
    original_env = dict(os.environ)

    explicit_lane = raw_source.get("FACTORYOS_TEST_LANE", "").strip()
    if explicit_lane not in {"", "integration"}:
        raise ValueError("--native-ephemeral cannot be combined with FACTORYOS_TEST_LANE != 'integration'")
    if raw_source.get("TEST_MONGO_URL", "").strip():
        raise ValueError("--native-ephemeral manages its own ephemeral MongoDB; omit TEST_MONGO_URL")
    if raw_source.get("TEST_ACCESS_TOKEN", "").strip():
        raise ValueError("--native-ephemeral mints its own ephemeral owner token; omit TEST_ACCESS_TOKEN")
    if raw_source.get("TEST_ROLE_CREDENTIALS_FILE", "").strip():
        raise ValueError("--native-ephemeral manages its own credential file; omit TEST_ROLE_CREDENTIALS_FILE")

    inherited_secret = raw_source.get("JWT_SECRET", "").strip()
    explicit_test_secret = raw_source.get("TEST_JWT_SECRET", "").strip()
    if explicit_test_secret:
        test_secret = _validated_secret(raw_source, env_name="TEST_JWT_SECRET")
    else:
        test_secret = secrets.token_urlsafe(32)
    if inherited_secret and inherited_secret == test_secret:
        raise ValueError("TEST_JWT_SECRET must be separately provisioned")

    mongod_proc = None
    mongo_tmpdir = None
    run_tmpdir = None
    cred_file = None

    try:
        mongod_proc, mongo_tmpdir, _port, test_mongo_url = _start_ephemeral_mongod()
        inherited_mongo_url = raw_source.get("MONGO_URL", "").strip()
        if inherited_mongo_url and inherited_mongo_url == test_mongo_url:
            raise ValueError("TEST_MONGO_URL must be separately provisioned")

        test_access_token = _seed_initial_company_and_owner(test_mongo_url, test_secret)

        ephemeral_source = dict(raw_source)
        _remove_mongo_environment(ephemeral_source)
        _remove_demo_environment(ephemeral_source)
        ephemeral_source.pop("JWT_SECRET", None)
        ephemeral_source["FACTORYOS_TEST_LANE"] = "integration"
        ephemeral_source["TEST_JWT_SECRET"] = test_secret
        ephemeral_source["TEST_MONGO_URL"] = test_mongo_url
        ephemeral_source["TEST_ACCESS_TOKEN"] = test_access_token
        env = build_test_environment(ephemeral_source)

        company_id = "co-tscheck-primary"
        credentials_data = {
            actor: {
                "email": f"{actor}@factoryos-test.invalid",
                "password": secrets.token_urlsafe(24),
                "company_id": company_id,
            }
            for actor in _SYNTHETIC_ACTORS
        }

        run_tmpdir = tempfile.mkdtemp(prefix="factoryos_test_run_")
        cred_file = os.path.join(run_tmpdir, "test-role-credentials.json")
        _write_credentials_file_0600(cred_file, credentials_data)
        env["TEST_ROLE_CREDENTIALS_FILE"] = cred_file

        def _provision_actors(backend_url: str, run_id: str) -> None:
            _post_json(
                f"{backend_url}/__test__/provision-actors",
                {"company_id": company_id, "actors": credentials_data},
                headers={"X-Test-Run-ID": run_id},
            )

        return _execute_isolated_backend_and_pytest(
            backend_dir,
            env,
            argv,
            before_session_check=_provision_actors,
        )
    finally:
        if cred_file and os.path.exists(cred_file):
            try:
                os.remove(cred_file)
            except OSError:
                pass
        if run_tmpdir and os.path.exists(run_tmpdir):
            shutil.rmtree(run_tmpdir, ignore_errors=True)
        if mongod_proc is not None:
            _stop_child(mongod_proc)
        if mongo_tmpdir and os.path.exists(mongo_tmpdir):
            shutil.rmtree(mongo_tmpdir, ignore_errors=True)
        os.environ.clear()
        os.environ.update(original_env)


def main(argv: list[str] | None = None) -> int:
    """Run mock tests, external integration tests, or explicit `--native-ephemeral` mode."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    native_ephemeral = "--native-ephemeral" in raw_argv
    test_args = [arg for arg in raw_argv if arg != "--native-ephemeral"]

    backend_dir = Path(__file__).resolve().parents[1]
    if native_ephemeral:
        try:
            return _run_native_ephemeral_pytest(backend_dir, test_args)
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"Native ephemeral integration preflight failed: {exc}", file=sys.stderr)
            return 2

    try:
        env = build_test_environment()
    except ValueError as exc:
        print(f"Isolated test configuration error: {exc}", file=sys.stderr)
        return 2

    if env["FACTORYOS_TEST_LANE"] == "mock":
        return _run_mock_pytest(backend_dir, env, test_args)
    try:
        validate_test_environment(env)
        return _run_integration_pytest(backend_dir, env, test_args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Isolated integration preflight failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
