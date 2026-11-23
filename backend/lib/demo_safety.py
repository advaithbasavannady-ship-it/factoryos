"""Create and validate the isolated client used by demo seed operations.

Application checks require ``DEMO_DEPLOYMENT_ID`` to match the replica-set name
returned by ``hello.setName`` and ``DEMO_ALLOWED_HOSTS`` to list canonical
replica-set member addresses as comma-separated ``host:port`` values. The set
reported by ``hello`` and the responding member must fit that allowlist. These
checks identify the intended deployment; they do not prove MongoDB authorization.
An administrator must provision a separate demo identity with write access only
to the isolated demo database and no production write privileges. The identity
must not be provisioned on production.

Authentication and deployment identity checks do not establish write
authorization. The MongoDB administrator must provision a separate demo
principal, grant it write access only to the isolated demo database, deny it all
production write privileges, and ensure it is not an overprivileged production
identity. Application-level host and replica-set checks do not prove these
permissions.

The demo client uses ``DEMO_SOCKET_TIMEOUT_MS`` for socket operations. It
defaults to 30000 ms and accepts values from 1 through 120000 ms. A timeout
during seed execution can still leave a partial reset.
Seed/reset operations themselves are not atomic: a later failure can leave a
partial demo reset.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from motor.motor_asyncio import AsyncIOMotorClient


class DemoSeedSafetyError(RuntimeError):
    """Raised when a dedicated demo target cannot be verified safely."""


_CONTEXT_PROVENANCE = object()
_DEFAULT_SOCKET_TIMEOUT_MS = 30000
_MAX_SOCKET_TIMEOUT_MS = 120000


@dataclass(frozen=True)
class DemoContext:
    """Verified client, database and company binding for one public operation.

    The private provenance field is issued only by ``open_demo_context``.
    Direct construction remains technically possible in Python, but such a
    context is rejected by ``validate_demo_context``.
    """

    client: Any
    database: Any
    company_id: str
    _provenance: object = field(default=None, repr=False, compare=False)

    async def close(self) -> None:
        _close_client(self.client)


def validate_demo_context(context: DemoContext) -> None:
    """Reject hand-built contexts and any context inconsistent with its client."""
    if not isinstance(context, DemoContext) or context._provenance is not _CONTEXT_PROVENANCE:
        raise DemoSeedSafetyError("demo operation requires a context issued by the demo safety module")
    try:
        if context.database.client is not context.client:
            raise DemoSeedSafetyError("demo context client and database do not match")
        if not context.database.name or not context.company_id:
            raise DemoSeedSafetyError("demo context is incomplete")
    except DemoSeedSafetyError:
        raise
    except Exception:
        raise DemoSeedSafetyError("demo context cannot be validated") from None

    # Avoid importing lib.db, which would create the normal application client.
    app_db_module = sys.modules.get("lib.db")
    if app_db_module is not None and getattr(app_db_module, "client", None) is context.client:
        raise DemoSeedSafetyError("the application MongoDB client cannot be used for demo seeding")


def _normalized_address(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("missing address")
    parsed = urlsplit(f"//{value.strip()}")
    if not parsed.hostname:
        raise ValueError("invalid address")
    return parsed.hostname.rstrip(".").lower(), parsed.port or 27017


def _configured_hosts(raw: str) -> set[tuple[str, int]]:
    hosts = {_normalized_address(item) for item in raw.split(",") if item.strip()}
    if not hosts:
        raise ValueError("empty allowlist")
    return hosts


def _close_client(client: Any) -> None:
    if client is not None:
        try:
            client.close()
        except Exception:
            # Do not leak a driver exception that could include connection data.
            pass


def _configured_socket_timeout_ms() -> int:
    raw = os.environ.get("DEMO_SOCKET_TIMEOUT_MS", "").strip()
    if not raw:
        return _DEFAULT_SOCKET_TIMEOUT_MS
    try:
        timeout_ms = int(raw)
    except ValueError:
        raise DemoSeedSafetyError("demo socket timeout configuration is invalid") from None
    if not 1 <= timeout_ms <= _MAX_SOCKET_TIMEOUT_MS:
        raise DemoSeedSafetyError("demo socket timeout configuration is outside the allowed range")
    return timeout_ms


async def open_demo_context() -> DemoContext:
    """Construct and authorize the dedicated demo client, failing closed."""
    uri = os.environ.get("DEMO_MONGO_URL", "").strip()
    db_name = os.environ.get("DEMO_DB_NAME", "").strip()
    company_id = os.environ.get("DEMO_COMPANY_ID", "").strip()
    deployment_id = os.environ.get("DEMO_DEPLOYMENT_ID", "").strip()
    raw_allowed_hosts = os.environ.get("DEMO_ALLOWED_HOSTS", "").strip()
    if not all((uri, db_name, company_id, deployment_id, raw_allowed_hosts)):
        raise DemoSeedSafetyError("dedicated demo target configuration is incomplete")
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise DemoSeedSafetyError("dedicated demo URI is invalid")
    try:
        allowed_hosts = _configured_hosts(raw_allowed_hosts)
    except (TypeError, ValueError):
        raise DemoSeedSafetyError("dedicated demo host allowlist is invalid") from None
    socket_timeout_ms = _configured_socket_timeout_ms()

    # This is an additional configuration error check, never proof of isolation.
    app_uri = os.environ.get("MONGO_URL", "").strip()
    if app_uri and uri == app_uri:
        raise DemoSeedSafetyError("demo URI must use an independently provisioned identity")

    client = None
    try:
        # This constructor is intentionally independent of lib.db and has no fallback.
        client = AsyncIOMotorClient(
            uri,
            serverSelectionTimeoutMS=5000,
            socketTimeoutMS=socket_timeout_ms,
        )
        database = client[db_name]
        hello = await client.admin.command("hello")
        if not isinstance(hello, dict) or hello.get("ok") != 1:
            raise DemoSeedSafetyError("demo MongoDB deployment identity could not be verified")
        if hello.get("setName") != deployment_id:
            raise DemoSeedSafetyError("MongoDB server is outside the configured demo deployment")
        try:
            actual_host = _normalized_address(hello.get("me"))
            replica_hosts = hello.get("hosts")
            if not isinstance(replica_hosts, (list, tuple)) or not replica_hosts:
                raise ValueError("replica set has no reported members")
            reported_hosts = {
                _normalized_address(host)
                for key in ("hosts", "passives", "arbiters")
                for host in (hello.get(key) or [])
            }
        except (TypeError, ValueError):
            raise DemoSeedSafetyError("MongoDB server address could not be verified") from None
        if actual_host not in allowed_hosts or actual_host not in reported_hosts or not reported_hosts <= allowed_hosts:
            raise DemoSeedSafetyError("MongoDB server is outside the configured demo host allowlist")

        status = await client.admin.command("connectionStatus", showPrivileges=False)
        users = status.get("authInfo", {}).get("authenticatedUsers", []) if isinstance(status, dict) else []
        if not users:
            raise DemoSeedSafetyError("demo MongoDB identity is not authenticated")

        # This confirms authentication only. MongoDB role provisioning, not this
        # check, determines whether the principal may write to production.
        context = DemoContext(
            client=client,
            database=database,
            company_id=company_id,
            _provenance=_CONTEXT_PROVENANCE,
        )
        validate_demo_context(context)
        return context
    except DemoSeedSafetyError:
        _close_client(client)
        raise
    except Exception:
        _close_client(client)
        # Never include driver exceptions, which may contain credential material.
        raise DemoSeedSafetyError("dedicated demo MongoDB setup or authorization failed") from None
