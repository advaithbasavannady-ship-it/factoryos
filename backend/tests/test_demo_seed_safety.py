"""Mocked tests for demo deployment identity, authorization and seed routing."""

from __future__ import annotations

import asyncio
import importlib
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


class FakeCursor:
    def __init__(self, database, collection):
        self.database, self.collection = database, collection

    def sort(self, *args):
        return self

    async def to_list(self, _length):
        self.database.operations.append((self.collection, "find"))
        return []


class FakeDatabase:
    def __init__(self, name: str, client: "FakeMotorClient"):
        self.name, self.client = name, client
        self.operations: list[tuple[str, str]] = []
        self.requested_collections: set[str] = set()

    def __getitem__(self, collection: str):
        self.requested_collections.add(collection)
        return FakeCollection(self, collection)

    def __getattr__(self, collection: str):
        if collection.startswith("_"):
            raise AttributeError(collection)
        self.requested_collections.add(collection)
        return FakeCollection(self, collection)


class FakeCollection:
    def __init__(self, database, name):
        self.database, self.name = database, name

    async def _write(self, method, *args, **kwargs):
        self.database.operations.append((self.name, method))
        if method == "delete_many" and self.database.client.failure_mode == "seed_delete_error":
            raise ConnectionError("mock failure during seed deletion")
        return SimpleNamespace(inserted_id="mock-id")

    async def insert_one(self, *args, **kwargs):
        return await self._write("insert_one", *args, **kwargs)

    async def insert_many(self, *args, **kwargs):
        return await self._write("insert_many", *args, **kwargs)

    async def delete_one(self, *args, **kwargs):
        return await self._write("delete_one", *args, **kwargs)

    async def delete_many(self, *args, **kwargs):
        return await self._write("delete_many", *args, **kwargs)

    async def update_one(self, *args, **kwargs):
        return await self._write("update_one", *args, **kwargs)

    async def update_many(self, *args, **kwargs):
        return await self._write("update_many", *args, **kwargs)

    async def replace_one(self, *args, **kwargs):
        return await self._write("replace_one", *args, **kwargs)

    async def count_documents(self, *args, **kwargs):
        self.database.operations.append((self.name, "count_documents"))
        return 0

    async def find_one(self, *args, **kwargs):
        self.database.operations.append((self.name, "find_one"))
        return None

    def find(self, *args, **kwargs):
        return FakeCursor(self.database, self.name)


class FakeAdmin:
    def __init__(self, client):
        self.client = client
        self.commands: list[str] = []

    async def command(self, command: str, **kwargs):
        self.commands.append(command)
        if self.client.failure_mode == "connection_drop" and command == "connectionStatus":
            raise ConnectionError("mock connection dropped during authorization")
        if command == "hello":
            if self.client.failure_mode == "hello_timeout":
                raise TimeoutError("mock hello timed out")
            members = [f"{self.client.host}:27017"]
            if self.client.failure_mode == "unexpected_member":
                members.append("production.invalid:27017")
            return {
                "ok": 1,
                "setName": self.client.deployment_id,
                "me": f"{self.client.host}:27017",
                "hosts": members,
            }
        if command == "connectionStatus":
            users = [{"user": self.client.username or "", "db": "admin"}] if self.client.authenticated else []
            return {"ok": 1, "authInfo": {"authenticatedUsers": users}}
        raise AssertionError(f"unexpected mocked command: {command}")


class FakeMotorClient:
    instances: list["FakeMotorClient"] = []
    allowed_host = "demo.invalid"
    allowed_deployment = "factoryos-demo-rs"
    allowed_database = "factoryos_isolated_demo_test"
    failure_mode: str | None = None

    def __init__(self, uri: str, *args: Any, **kwargs: Any):
        parsed = urlsplit(uri)
        self.uri = uri
        self.host = (parsed.hostname or "").lower()
        self.username = parsed.username
        self.deployment_id = (
            self.allowed_deployment if self.host == self.allowed_host else "factoryos-production-rs"
        )
        self.authenticated = (
            (self.host == self.allowed_host and self.username == "demo-seed")
            or (self.host == "production.invalid" and self.username == "prod-admin")
        )
        self.failure_mode = type(self).failure_mode
        self.client_options = kwargs
        self.admin = FakeAdmin(self)
        self.databases: dict[str, FakeDatabase] = {}
        self.closed = False
        self.instances.append(self)

    def __getitem__(self, name: str):
        if name not in self.databases:
            self.databases[name] = FakeDatabase(name, self)
        return self.databases[name]

    def close(self):
        self.closed = True


def _clear_backend_modules():
    for name in tuple(sys.modules):
        if name in {"server", "lib", "routers"} or name.startswith(("lib.", "routers.")):
            sys.modules.pop(name, None)


def _install_fake_motor(monkeypatch):
    import motor.motor_asyncio

    FakeMotorClient.instances.clear()
    FakeMotorClient.failure_mode = None
    monkeypatch.setattr(motor.motor_asyncio, "AsyncIOMotorClient", FakeMotorClient)


def _demo_env(monkeypatch, uri="mongodb://demo-seed:secret@demo.invalid:27017"):
    monkeypatch.setenv("DEMO_MONGO_URL", uri)
    monkeypatch.setenv("DEMO_DB_NAME", FakeMotorClient.allowed_database)
    monkeypatch.setenv("DEMO_COMPANY_ID", "demo-company-test")
    monkeypatch.setenv("DEMO_DEPLOYMENT_ID", FakeMotorClient.allowed_deployment)
    monkeypatch.setenv("DEMO_ALLOWED_HOSTS", "demo.invalid:27017")


def _load_seed_data(monkeypatch):
    _install_fake_motor(monkeypatch)
    monkeypatch.setenv("MONGO_URL", "mongodb://app-user:secret@app.invalid:27017")
    monkeypatch.setenv("DB_NAME", "factoryos_application_test")
    _clear_backend_modules()
    return importlib.import_module("lib.seed_data")


def _seed_deletes(client):
    return [
        (db_name, collection, method)
        for db_name, database in client.databases.items()
        for collection, method in database.operations
        if method == "delete_many"
    ]


def test_normal_server_import_does_not_require_demo_configuration_or_use_demo_client(monkeypatch):
    _install_fake_motor(monkeypatch)
    monkeypatch.setenv("MONGO_URL", "mongodb://app-user:secret@app.invalid:27017")
    monkeypatch.setenv("DB_NAME", "factoryos_application_test")
    for key in (
        "DEMO_MONGO_URL", "DEMO_DB_NAME", "DEMO_COMPANY_ID", "DEMO_DEPLOYMENT_ID", "DEMO_ALLOWED_HOSTS"
    ):
        monkeypatch.delenv(key, raising=False)
    _clear_backend_modules()

    server = importlib.import_module("server")
    app_client = FakeMotorClient.instances[0]
    seed_data = importlib.import_module("lib.seed_data")
    from lib.demo_safety import DemoSeedSafetyError

    with pytest.raises(DemoSeedSafetyError):
        asyncio.run(seed_data.seed_all())
    assert len(FakeMotorClient.instances) == 1
    assert FakeMotorClient.instances[0] is app_client
    assert server.api_router.dependencies == []
    assert app_client.databases["factoryos_application_test"].operations == []


def test_missing_invalid_or_shared_demo_configuration_fails_closed(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    from lib.demo_safety import DemoSeedSafetyError

    keys = ("DEMO_MONGO_URL", "DEMO_DB_NAME", "DEMO_COMPANY_ID", "DEMO_DEPLOYMENT_ID", "DEMO_ALLOWED_HOSTS")
    for key in keys:
        _demo_env(monkeypatch)
        monkeypatch.delenv(key, raising=False)
        with pytest.raises(DemoSeedSafetyError):
            asyncio.run(seed_data.reset_demo())
    _demo_env(monkeypatch, "not-a-mongodb-uri")
    with pytest.raises(DemoSeedSafetyError):
        asyncio.run(seed_data.reset_demo())
    same_uri = "mongodb://same-user:secret@production.invalid:27017"
    monkeypatch.setenv("MONGO_URL", same_uri)
    _demo_env(monkeypatch, same_uri)
    with pytest.raises(DemoSeedSafetyError, match="independently provisioned"):
        asyncio.run(seed_data.reset_demo())
    assert FakeMotorClient.instances == []


@pytest.mark.parametrize(("configured", "expected"), [(None, 30000), ("6500", 6500)])
def test_demo_client_receives_finite_configurable_socket_timeout(monkeypatch, configured, expected):
    _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    if configured is not None:
        monkeypatch.setenv("DEMO_SOCKET_TIMEOUT_MS", configured)
    else:
        monkeypatch.delenv("DEMO_SOCKET_TIMEOUT_MS", raising=False)
    safety = importlib.import_module("lib.demo_safety")

    context = asyncio.run(safety.open_demo_context())
    client = FakeMotorClient.instances[-1]
    try:
        assert client.client_options["socketTimeoutMS"] == expected
        assert client.client_options["serverSelectionTimeoutMS"] == 5000
    finally:
        asyncio.run(context.close())
    assert client.closed


@pytest.mark.parametrize("timeout", ["not-a-number", "0", "120001"])
def test_invalid_socket_timeout_fails_closed_before_client_construction(monkeypatch, timeout):
    _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    monkeypatch.setenv("DEMO_SOCKET_TIMEOUT_MS", timeout)
    from lib.demo_safety import DemoSeedSafetyError

    with pytest.raises(DemoSeedSafetyError, match="socket timeout"):
        asyncio.run(importlib.import_module("lib.demo_safety").open_demo_context())
    assert FakeMotorClient.instances == []


def test_production_host_is_rejected_even_with_the_approved_demo_database(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    from lib.demo_safety import DemoSeedSafetyError

    _demo_env(monkeypatch, "mongodb://demo-seed:secret@production.invalid:27017")
    with pytest.raises(DemoSeedSafetyError, match="deployment"):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert not client.authenticated
    assert "factoryos_isolated_demo_test" in client.databases
    assert client.databases["factoryos_isolated_demo_test"].operations == []
    assert _seed_deletes(client) == []
    assert client.closed


def test_production_host_authentication_fails_even_if_host_allowlist_is_misconfigured(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch, "mongodb://demo-seed:secret@production.invalid:27017")
    # Simulate the deployment ID and host allowlist also being mistakenly set to
    # production. The restricted demo identity still is not provisioned there.
    monkeypatch.setenv("MONGO_URL", "mongodb://app-user:secret@production.invalid:27017")
    monkeypatch.setenv("DB_NAME", "factoryos_production")
    monkeypatch.setenv("DEMO_DEPLOYMENT_ID", "factoryos-production-rs")
    monkeypatch.setenv("DEMO_ALLOWED_HOSTS", "production.invalid:27017")
    from lib.demo_safety import DemoSeedSafetyError

    with pytest.raises(DemoSeedSafetyError, match="not authenticated"):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert not client.authenticated
    assert "factoryos_isolated_demo_test" in client.databases
    assert client.databases["factoryos_isolated_demo_test"].operations == []
    assert _seed_deletes(client) == []
    assert client.closed


def test_application_checks_cannot_prove_an_authenticated_principals_mongo_roles(monkeypatch):
    _load_seed_data(monkeypatch)
    # Deliberately misconfigure the demo target identity and host allowlist to
    # production. This fake reports authentication only; it does not simulate
    # MongoDB roles or demonstrate that this credential may perform any write.
    uri = "mongodb://prod-admin:secret@production.invalid:27017"
    _demo_env(monkeypatch, uri)
    monkeypatch.setenv("MONGO_URL", "mongodb://app-user:other-secret@production.invalid:27017")
    monkeypatch.setenv("DEMO_DB_NAME", "factoryos_production")
    monkeypatch.setenv("DEMO_DEPLOYMENT_ID", "factoryos-production-rs")
    monkeypatch.setenv("DEMO_ALLOWED_HOSTS", "production.invalid:27017")
    safety = importlib.import_module("lib.demo_safety")

    context = asyncio.run(safety.open_demo_context())
    client = FakeMotorClient.instances[-1]
    try:
        assert client.authenticated
        assert context.database.name == "factoryos_production"
        assert client.databases["factoryos_production"].operations == []
        assert client.client_options["socketTimeoutMS"] == 30000
    finally:
        asyncio.run(context.close())
    assert client.closed


def test_demo_identity_must_authenticate_on_the_allowed_host(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    from lib.demo_safety import DemoSeedSafetyError

    _demo_env(monkeypatch, "mongodb://wrong-user:secret@demo.invalid:27017")
    with pytest.raises(DemoSeedSafetyError, match="not authenticated"):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert _seed_deletes(client) == []
    assert client.closed


def test_configured_demo_host_allowlist_must_match_server_identity(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    monkeypatch.setenv("DEMO_ALLOWED_HOSTS", "other-demo.invalid:27017")
    from lib.demo_safety import DemoSeedSafetyError

    with pytest.raises(DemoSeedSafetyError, match="host allowlist"):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert _seed_deletes(client) == []
    assert client.closed


def test_every_discovered_replica_member_must_be_allowlisted(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    FakeMotorClient.failure_mode = "unexpected_member"
    from lib.demo_safety import DemoSeedSafetyError

    with pytest.raises(DemoSeedSafetyError, match="host allowlist"):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert _seed_deletes(client) == []
    assert client.closed


@pytest.mark.parametrize(
    "failure_mode",
    [
        "connection_drop", "hello_timeout",
    ],
)
def test_verification_failures_close_client_before_any_seed_delete(monkeypatch, failure_mode):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    FakeMotorClient.failure_mode = failure_mode
    from lib.demo_safety import DemoSeedSafetyError

    with pytest.raises(DemoSeedSafetyError):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert _seed_deletes(client) == []
    assert client.closed


def test_application_and_demo_clients_are_distinct_and_seed_writes_use_demo_database(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    app_database = importlib.import_module("lib.db").db
    app_client = app_database.client

    asyncio.run(seed_data.seed_all())
    demo_client = FakeMotorClient.instances[-1]
    assert demo_client is not app_client
    assert len(FakeMotorClient.instances) == 2
    assert app_database.operations == []
    assert set(demo_client.databases) == {FakeMotorClient.allowed_database}
    demo_db = demo_client.databases[FakeMotorClient.allowed_database]
    assert all(name != "__factoryos_demo_seed_probe" for name, _ in demo_db.operations)
    assert "__factoryos_demo_seed_probe" not in demo_db.requested_collections
    assert {name for name, operation in demo_db.operations if operation == "delete_many"} == set(seed_data.COLLECTIONS)
    assert demo_client.closed


def test_manually_constructed_and_application_bound_contexts_are_rejected(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    from lib.demo_safety import DemoContext, DemoSeedSafetyError

    app_database = importlib.import_module("lib.db").db
    app_client = app_database.client
    handmade = DemoContext(app_client, app_database, "demo-company-test")
    with pytest.raises(DemoSeedSafetyError, match="issued by"):
        asyncio.run(seed_data._seed_all(handmade))
    forged = DemoContext(
        app_client,
        app_database,
        "demo-company-test",
        _provenance=importlib.import_module("lib.demo_safety")._CONTEXT_PROVENANCE,
    )
    with pytest.raises(DemoSeedSafetyError, match="application MongoDB client"):
        asyncio.run(seed_data._seed_all(forged))
    assert app_database.operations == []


def test_reset_and_cli_reuse_one_verified_context(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    calls = []

    async def fake_seed(context):
        calls.append(("seed", context))

    async def fake_upgrade(context):
        calls.append(("upgrade", context))

    monkeypatch.setattr(seed_data, "_seed_all", fake_seed)
    monkeypatch.setattr(seed_data, "_upgrade_v2", fake_upgrade)
    asyncio.run(seed_data.reset_demo())
    assert calls[0][1] is calls[1][1]
    assert len(FakeMotorClient.instances) == 1
    assert FakeMotorClient.instances[0].closed

    calls.clear()
    cli = importlib.import_module("tests.seed")
    asyncio.run(cli.main(demo_opt_in=True))
    assert calls[0][1] is calls[1][1]
    assert len(FakeMotorClient.instances) == 2
    assert FakeMotorClient.instances[1].closed


def test_client_closes_when_seed_operation_fails_after_deletion_begins(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    _demo_env(monkeypatch)
    FakeMotorClient.failure_mode = "seed_delete_error"

    with pytest.raises(ConnectionError, match="seed deletion"):
        asyncio.run(seed_data.reset_demo())
    client = FakeMotorClient.instances[-1]
    assert client.closed
    assert len(_seed_deletes(client)) == 1


def test_reset_route_enforces_owner_and_delegates_without_assuming_database_state(monkeypatch):
    _load_seed_data(monkeypatch)
    monkeypatch.setenv("ENABLE_DEMO_SEED", "true")
    monkeypatch.setenv("DEMO_COMPANY_ID", "demo-company-test")
    route = importlib.import_module("routers.company")
    delegated = []
    from fastapi import HTTPException

    async def fake_reset():
        delegated.append(True)

    monkeypatch.setattr(route, "reset_demo", fake_reset)
    result = asyncio.run(route.reset_demo_data({"role": "owner"}, "demo-company-test"))
    assert delegated == [True]
    assert result.today
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route.reset_demo_data({"role": "operator"}, "demo-company-test"))
    assert exc.value.status_code == 403
    assert delegated == [True]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(route.reset_demo_data({"role": "owner"}, "another-company"))
    assert exc.value.status_code == 404
    assert delegated == [True]


def test_seed_generation_shapes_and_calculations_match_baseline_contract(monkeypatch):
    seed_data = _load_seed_data(monkeypatch)
    seed_data.CID = "demo-company-test"
    seed_data.COMPANY["id"] = seed_data.CID
    employees = seed_data._employees()
    assert len(employees) == sum(count for _, count, _ in seed_data.DEPTS)
    assert set(employees[0]) == {
        "id", "company_id", "emp_code", "name", "department", "role", "shift", "phone", "email", "joined_on"
    }
    assert employees[0]["emp_code"] == "EMP-1001"
    assert employees[0]["company_id"] == "demo-company-test"
    assert re.fullmatch(r"[0-9a-f-]{36}", employees[0]["id"])
    assert employees[0]["email"].endswith("@apexprecision.demo")
    assert len(seed_data._customers(date(2025, 1, 1))) == len(seed_data.CUSTOMERS)

    today = date(2025, 1, 1)
    idle_machine = {"id": "m-idle", "code": "PRB-01", "status": "idle", "department": "Fabrication"}
    production = seed_data._production_for(today, [idle_machine], True, today)
    assert len(production) == 1
    assert production[0]["planned"] == int(seed_data.CAPACITY["PRB-01"] * 0.5)
    assert production[0]["company_id"] == "demo-company-test"
    assert production[0]["actual"] <= production[0]["planned"]
    zero_capacity = {"id": "m-zero", "code": "CMM-01", "status": "running", "department": "Quality Control"}
    assert seed_data._production_for(today, [zero_capacity], True, today) == []

    customers = seed_data._customers(today)
    order = seed_data._orders_for(today, today, 1, 1, customers)[0]
    assert set(order) == {
        "id", "company_id", "order_no", "customer_id", "customer", "product", "quantity", "unit_price",
        "status", "priority", "order_date", "expected_delivery", "dispatched_date", "notes", "department",
        "production_target_date", "produced", "qc_passed", "qc_rejected", "qc_inspector", "dispatch_carrier",
        "dispatch_ref", "timeline",
    }
    assert order["status"] == "new"
    assert order["order_no"] == "SO-2025-0001"
    assert order["company_id"] == "demo-company-test"
    assert order["expected_delivery"] > order["order_date"]
    assert (order["product"], order["unit_price"]) in seed_data.PRODUCTS
    assert order["quantity"] in {50, 100, 150, 200, 250, 400, 500, 750, 1000}


def test_cli_requires_explicit_opt_in(monkeypatch):
    _load_seed_data(monkeypatch)
    safety = importlib.import_module("lib.demo_safety")
    DemoSeedSafetyError = safety.DemoSeedSafetyError

    cli = importlib.import_module("tests.seed")
    monkeypatch.setattr(cli, "DemoSeedSafetyError", DemoSeedSafetyError)
    with pytest.raises(DemoSeedSafetyError, match="--demo"):
        asyncio.run(cli.main())
