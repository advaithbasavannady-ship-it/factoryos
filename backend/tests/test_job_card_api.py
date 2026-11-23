"""HTTP API tests for the Job Card router (Day 1.7B).

Mock lane only (FACTORYOS_TEST_LANE=mock). Requests run through FastAPI's TestClient
against the real router, the real authentication chain (JWT decode, session, user and
membership lookups in ``lib.tenant``), the real permission catalogue, the real
``services.job_cards`` service layer and the real ``lib.audit`` / ``lib.idempotency``
helpers. Only the MongoDB handle is replaced by an in-memory mock.

These tests do NOT establish real MongoDB transaction, rollback, unique-index or
concurrent-writer guarantees; those require the separately approved live replica-set
integration milestone.
"""

from __future__ import annotations

import copy
import importlib
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError, PyMongoError

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ==============================================================================
# In-memory MongoDB mock (transaction staging + read-your-own-writes)
# ==============================================================================

class MockSession:
    def __init__(self) -> None:
        self.staged_inserts: list[tuple["MockCollection", dict[str, Any]]] = []
        self.staged_updates: list[tuple["MockCollection", dict[str, Any], dict[str, Any]]] = []
        self.is_active = True

    async def __aenter__(self) -> "MockSession":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self._abort()
        self.is_active = False
        return False

    def _abort(self) -> None:
        self.staged_inserts.clear()
        self.staged_updates.clear()

    async def with_transaction(self, callback: Any) -> Any:
        try:
            result = await callback(self)
        except Exception:
            self._abort()
            raise
        for col, doc in self.staged_inserts:
            col.docs.append(doc)
        for col, query, update in self.staged_updates:
            col._apply_update(query, update)
        self._abort()
        return result


class MockUpdateResult:
    def __init__(self, matched: int) -> None:
        self.matched_count = matched
        self.modified_count = matched


class MockCollection:
    def __init__(self, name: str) -> None:
        self.name = name
        self.docs: list[dict[str, Any]] = []
        self.fail_writes: Exception | None = None

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            if key == "$or":
                if not any(self._matches(doc, branch) for branch in expected):
                    return False
            elif isinstance(expected, dict):
                if "$exists" in expected and (key in doc) != expected["$exists"]:
                    return False
                if "$ne" in expected and doc.get(key) == expected["$ne"]:
                    return False
                if "$in" in expected and doc.get(key) not in expected["$in"]:
                    return False
            elif doc.get(key) != expected:
                return False
        return True

    def _apply_update(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        for doc in self.docs:
            if self._matches(doc, query):
                doc.update(copy.deepcopy(update.get("$set", {})))
                for inc_k, inc_v in update.get("$inc", {}).items():
                    doc[inc_k] = int(doc.get(inc_k) or 0) + inc_v
                break

    @staticmethod
    def _project(doc: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
        out = copy.deepcopy(doc)
        if projection and projection.get("_id") == 0:
            out.pop("_id", None)
        return out

    async def insert_one(self, doc: dict[str, Any], session: MockSession | None = None) -> Any:
        if self.fail_writes is not None:
            raise self.fail_writes
        cloned = copy.deepcopy(doc)
        if session is not None and session.is_active:
            session.staged_inserts.append((self, cloned))
        else:
            self.docs.append(cloned)
        return type("InsertResult", (), {"inserted_id": cloned.get("_id") or cloned.get("id")})()

    async def find_one(self, query: dict[str, Any], projection: dict[str, Any] | None = None,
                       session: MockSession | None = None) -> dict[str, Any] | None:
        if session is not None and session.is_active:
            for col, staged in reversed(session.staged_inserts):
                if col is self and self._matches(staged, query):
                    return self._project(staged, projection)
        for doc in self.docs:
            if self._matches(doc, query):
                return self._project(doc, projection)
        return None

    async def update_one(self, query: dict[str, Any], update: dict[str, Any],
                         session: MockSession | None = None, upsert: bool = False) -> MockUpdateResult:
        if self.fail_writes is not None:
            raise self.fail_writes
        matched = 1 if any(self._matches(d, query) for d in self.docs) else 0
        if session is not None and session.is_active:
            session.staged_updates.append((self, query, update))
        elif matched:
            self._apply_update(query, update)
        return MockUpdateResult(matched)

    async def find_one_and_update(self, query: dict[str, Any], update: dict[str, Any],
                                  projection: dict[str, Any] | None = None,
                                  session: MockSession | None = None,
                                  return_document: Any = None) -> dict[str, Any] | None:
        if self.fail_writes is not None:
            raise self.fail_writes
        found = await self.find_one(query, projection=projection, session=session)
        if found is None:
            return None
        if session is not None and session.is_active:
            session.staged_updates.append((self, query, update))
        else:
            self._apply_update(query, update)
        return found


class MockDatabase:
    """Lazily creates any collection the application touches."""

    def __init__(self) -> None:
        self._collections: dict[str, MockCollection] = {}

    def __getattr__(self, name: str) -> MockCollection:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._collections.setdefault(name, MockCollection(name))

    def __getitem__(self, name: str) -> MockCollection:
        return getattr(self, name)


class MockClient:
    async def start_session(self) -> MockSession:
        return MockSession()

    def close(self) -> None:  # pragma: no cover - parity with Motor
        pass


# ==============================================================================
# Fixtures
# ==============================================================================

COMPANY_ALPHA = "company-alpha"
COMPANY_BETA = "company-beta"


class Api:
    """Bundle of the test client, mock database and token factory."""

    def __init__(self, client: TestClient, db: MockDatabase, authz: ModuleType, router_module: ModuleType):
        self.client = client
        self.db = db
        self._authz = authz
        self.router_module = router_module

    def member(self, role: str, company_id: str = COMPANY_ALPHA, user_id: str | None = None) -> dict[str, str]:
        """Seed an active user, session and membership; return bearer auth headers."""
        user_id = user_id or f"usr-{role}-{company_id}"
        session_id = f"sess-{user_id}"
        self.db.users.docs.append({"id": user_id, "name": f"{role} user", "email": f"{user_id}@test.invalid",
                                   "status": "active", "auth_version": 1})
        self.db.sessions.docs.append({"session_id": session_id, "user_id": user_id, "company_id": company_id,
                                      "status": "active"})
        self.db.company_members.docs.append({"id": f"mem-{user_id}", "company_id": company_id, "user_id": user_id,
                                             "role": role, "status": "active"})
        token = self._authz.sign_access_token(user_id, company_id, session_id, role)
        return {"Authorization": f"Bearer {token}"}

    def seed_order(
        self,
        order_id: str = "ord-001",
        order_no: str = "SO-2026-0042",
        customer_id: str = "cust-001",
        status: str = "confirmed",
        company_id: str = COMPANY_ALPHA,
    ) -> dict[str, Any]:
        doc = {
            "company_id": company_id,
            "id": order_id,
            "order_no": order_no,
            "customer_id": customer_id,
            "status": status,
        }
        self.db.orders.docs = [
            d for d in self.db.orders.docs
            if not (d.get("company_id") == company_id and d.get("id") == order_id)
        ]
        self.db.orders.docs.append(doc)
        return doc


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Api:
    # Never import the real Mongo-backed lib.db in the mock lane: install a stub module
    # when no lib.db is loaded, then point every module-level db/client binding used by
    # the request path at a fresh in-memory database for this test.
    if "lib.db" not in sys.modules:
        stub = ModuleType("lib.db")
        stub.db = None
        stub.client = None
        monkeypatch.setitem(sys.modules, "lib.db", stub)

    db, client = MockDatabase(), MockClient()
    db_module = sys.modules["lib.db"]
    monkeypatch.setattr(db_module, "db", db, raising=False)
    monkeypatch.setattr(db_module, "client", client, raising=False)

    tenant = importlib.import_module("lib.tenant")
    authz = importlib.import_module("lib.authz")
    idempotency = importlib.import_module("lib.idempotency")
    middleware = importlib.import_module("lib.middleware")
    router_module = importlib.import_module("routers.job_cards")

    monkeypatch.setattr(tenant, "db", db)
    monkeypatch.setattr(authz, "db", db)
    monkeypatch.setattr(idempotency, "db", db)
    monkeypatch.setattr(idempotency, "client", client)

    # Mirror server.py composition: /api prefix and the request-audit middleware.
    app = FastAPI()
    api_router = APIRouter(prefix="/api")
    api_router.include_router(router_module.router)
    app.add_middleware(middleware.RequestAuditMiddleware)
    app.include_router(api_router)

    with TestClient(app) as test_client:
        api_bundle = Api(test_client, db, authz, router_module)
        api_bundle.seed_order(company_id=COMPANY_ALPHA)
        api_bundle.seed_order(company_id=COMPANY_BETA)
        yield api_bundle


def _package_body(job_card_no: str = "JC-2026-0001", **master_overrides: Any) -> dict[str, Any]:
    master = {
        "job_card_no": job_card_no,
        "order_id": "ord-001",
        "order_no": "SO-2026-0042",
        "part_number": "PCB-INV-REV1",
        "customer_id": "cust-001",
        "customer_name": "Nordic Dynamics",
        "order_quantity_pcb": 500,
        "layers": 4,
        **master_overrides,
    }
    return {
        "job_card": master,
        "drill_sheet": {"primary_drill": [{"tool_no": "T01", "drill_size_mm": 0.8, "hole_type": "PTH"}]},
    }


def _idem(key_suffix: str = "default") -> dict[str, str]:
    return {"Idempotency-Key": f"jc-api-test-key-{key_suffix}-0001"}


def _create(api: Api, headers: dict[str, str], job_card_no: str = "JC-2026-0001", key: str | None = None,
            **overrides: Any) -> dict[str, Any]:
    response = api.client.post("/api/job-cards", json=_package_body(job_card_no, **overrides),
                               headers={**headers, **_idem(key or job_card_no)})
    assert response.status_code == 201, response.text
    return response.json()


# ==============================================================================
# POST /api/job-cards
# ==============================================================================

def test_create_package_success(api: Api) -> None:
    owner = api.member("owner")
    response = api.client.post("/api/job-cards", json=_package_body(), headers={**owner, **_idem("create")})

    assert response.status_code == 201, response.text
    pkg = response.json()
    assert set(pkg) == {"job_card", "drill_sheet", "process_sequence_sheet"}
    jc, drill, proc = pkg["job_card"], pkg["drill_sheet"], pkg["process_sequence_sheet"]

    assert jc["status"] == "draft" and jc["version"] == 1 and jc["revision"] == 1
    assert jc["company_id"] == drill["company_id"] == proc["company_id"] == COMPANY_ALPHA
    for field in ("job_card_id", "job_card_no", "order_id", "order_no", "part_number", "revision", "company_id"):
        assert jc[field] == drill[field] == proc[field]
    assert jc["drill_sheet_id"] == drill["id"]
    assert jc["process_sequence_sheet_id"] == proc["id"]
    assert drill["primary_drill"][0]["tool_no"] == "T01"
    assert len(proc["processes"]) == 24  # default standard route when omitted
    assert jc["created_by"] == "usr-owner-company-alpha"

    assert len(api.db.job_cards.docs) == 1
    assert len(api.db.drill_detail_sheets.docs) == 1
    assert len(api.db.process_sequence_sheets.docs) == 1
    assert len(api.db.idempotency_records.docs) == 1
    created_audits = [a for a in api.db.audit_logs.docs if a["action"] == "job_card.created"]
    assert len(created_audits) == 1 and created_audits[0]["tenant_id"] == COMPANY_ALPHA


def test_create_rejects_client_company_id_mismatch(api: Api) -> None:
    owner = api.member("owner")
    response = api.client.post("/api/job-cards", json=_package_body(company_id=COMPANY_BETA),
                               headers={**owner, **_idem("spoof")})
    assert response.status_code == 422
    assert "company_id" in response.json()["detail"]
    assert api.db.job_cards.docs == []


@pytest.mark.parametrize("field,value", [
    ("status", "approved"),
    ("status", "released_to_production"),
    ("version", 7),
    ("created_by", "someone-else"),
    ("approved_by", "forged-approver"),
])
def test_create_rejects_server_managed_master_fields(api: Api, field: str, value: Any) -> None:
    owner = api.member("owner")
    response = api.client.post("/api/job-cards", json=_package_body(**{field: value}),
                               headers={**owner, **_idem(f"managed-{field}")})
    assert response.status_code == 422
    assert field in response.json()["detail"]
    assert api.db.job_cards.docs == []


def test_create_rejects_client_supplied_drill_sheet_is_locked(api: Api) -> None:
    owner = api.member("owner")
    body = _package_body("JC-DRILL-LOCK-FORGE")
    body["drill_sheet"]["is_locked"] = True
    response = api.client.post("/api/job-cards", json=body, headers={**owner, **_idem("drill-is-locked")})
    assert response.status_code == 422
    assert "is_locked" in response.json()["detail"]
    assert api.db.job_cards.docs == []
    assert api.db.drill_detail_sheets.docs == []


def test_create_rejects_unknown_top_level_section(api: Api) -> None:
    owner = api.member("owner")
    body = {**_package_body(), "company_id": COMPANY_BETA}
    response = api.client.post("/api/job-cards", json=body, headers={**owner, **_idem("extra")})
    assert response.status_code == 422
    assert api.db.job_cards.docs == []


def test_create_rejects_invalid_master_payload(api: Api) -> None:
    owner = api.member("owner")
    response = api.client.post("/api/job-cards", json=_package_body(job_card_no="   "),
                               headers={**owner, **_idem("blank-no")})
    assert response.status_code == 422
    assert api.db.job_cards.docs == []


def test_create_rejects_mismatched_sub_document_identity(api: Api) -> None:
    owner = api.member("owner")
    body = _package_body()
    body["drill_sheet"]["part_number"] = "DIFFERENT-PART"
    response = api.client.post("/api/job-cards", json=body, headers={**owner, **_idem("drill-mismatch")})
    assert response.status_code == 422
    assert "part_number" in response.json()["detail"]
    assert api.db.job_cards.docs == []


def test_create_requires_idempotency_key(api: Api) -> None:
    owner = api.member("owner")
    response = api.client.post("/api/job-cards", json=_package_body(), headers=owner)
    assert response.status_code == 422
    assert api.db.job_cards.docs == []


def test_create_rejects_malformed_idempotency_key(api: Api) -> None:
    owner = api.member("owner")
    response = api.client.post("/api/job-cards", json=_package_body(),
                               headers={**owner, "Idempotency-Key": "short"})
    assert response.status_code == 422
    assert "Idempotency-Key" in response.json()["detail"]
    assert api.db.job_cards.docs == []


def test_idempotency_replay_and_conflict(api: Api) -> None:
    owner = api.member("owner")
    headers = {**owner, **_idem("replay")}

    first = api.client.post("/api/job-cards", json=_package_body("JC-IDEM-01"), headers=headers)
    replay = api.client.post("/api/job-cards", json=_package_body("JC-IDEM-01"), headers=headers)
    assert first.status_code == replay.status_code == 201
    assert replay.json()["job_card"]["id"] == first.json()["job_card"]["id"]
    assert replay.json()["drill_sheet"]["id"] == first.json()["drill_sheet"]["id"]
    assert len(api.db.job_cards.docs) == 1

    conflict = api.client.post("/api/job-cards", json=_package_body("JC-IDEM-01", layers=8), headers=headers)
    assert conflict.status_code == 409
    assert "idempotency key" in conflict.json()["detail"]
    assert len(api.db.job_cards.docs) == 1


def test_duplicate_package_with_new_idempotency_key_returns_409(api: Api) -> None:
    owner = api.member("owner")
    _create(api, owner, "JC-DUP-01", key="dup-first")
    response = api.client.post("/api/job-cards", json=_package_body("JC-DUP-01"),
                               headers={**owner, **_idem("dup-second")})
    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    assert len(api.db.job_cards.docs) == 1


def test_database_write_failure_returns_generic_error_without_leaking_details(api: Api) -> None:
    owner = api.member("owner")
    api.db.process_sequence_sheets.fail_writes = RuntimeError("primary at mongo-internal:27017 secret=hunter2")
    response = api.client.post("/api/job-cards", json=_package_body("JC-WRITE-FAIL"),
                               headers={**owner, **_idem("write-fail")})
    assert response.status_code == 500
    assert response.json() == {"detail": "job card request could not be completed"}
    assert "hunter2" not in response.text and "mongo-internal" not in response.text
    assert api.db.job_cards.docs == [] and api.db.drill_detail_sheets.docs == []


# ==============================================================================
# Authentication and authorization
# ==============================================================================

@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/job-cards", _package_body()),
    ("get", "/api/job-cards/any-id", None),
    ("patch", "/api/job-cards/any-id", {"expected_version": 1, "layers": 2}),
])
def test_missing_authentication_returns_401(api: Api, method: str, path: str, body: Any) -> None:
    kwargs: dict[str, Any] = {"headers": _idem("noauth")}
    if body is not None:
        kwargs["json"] = body
    response = getattr(api.client, method)(path, **kwargs)
    assert response.status_code == 401


def test_invalid_or_revoked_authentication_returns_401(api: Api) -> None:
    bad = api.client.get("/api/job-cards/any-id", headers={"Authorization": "Bearer not-a-jwt"})
    assert bad.status_code == 401
    wrong_scheme = api.client.get("/api/job-cards/any-id", headers={"Authorization": "Basic abc"})
    assert wrong_scheme.status_code == 401

    owner = api.member("owner")
    api.db.sessions.docs[0]["status"] = "revoked"
    revoked = api.client.get("/api/job-cards/any-id", headers=owner)
    assert revoked.status_code == 401


def test_insufficient_permissions_return_403(api: Api) -> None:
    owner = api.member("owner")
    created = _create(api, owner, "JC-PERM-01")
    jc_id = created["job_card"]["id"]

    sales = api.member("sales_staff")  # has job_cards.read, lacks job_cards.manage
    accountant = api.member("accountant")  # lacks job_cards.read

    assert api.client.get(f"/api/job-cards/{jc_id}", headers=sales).status_code == 200
    post = api.client.post("/api/job-cards", json=_package_body("JC-PERM-02"), headers={**sales, **_idem("perm")})
    assert post.status_code == 403
    patch = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1, "layers": 2}, headers=sales)
    assert patch.status_code == 403
    get = api.client.get(f"/api/job-cards/{jc_id}", headers=accountant)
    assert get.status_code == 403

    # Authorization is evaluated before body validation (no schema disclosure).
    invalid_body = api.client.post("/api/job-cards", json={"unexpected": True}, headers=sales)
    assert invalid_body.status_code == 403

    assert len(api.db.job_cards.docs) == 1
    assert api.db.job_cards.docs[0]["layers"] == 4


def test_production_manager_can_manage_job_cards(api: Api) -> None:
    pm = api.member("production_manager")
    created = _create(api, pm, "JC-PM-01")
    assert created["job_card"]["status"] == "draft"


# ==============================================================================
# Tenant isolation
# ==============================================================================

def test_tenant_isolation(api: Api) -> None:
    alpha = api.member("owner", COMPANY_ALPHA)
    beta = api.member("owner", COMPANY_BETA)
    created = _create(api, alpha, "JC-SHARED-NO", key="alpha")
    jc_id = created["job_card"]["id"]

    assert api.client.get(f"/api/job-cards/{jc_id}", headers=beta).status_code == 404
    patch = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1, "layers": 10}, headers=beta)
    assert patch.status_code == 404
    assert api.db.job_cards.docs[0]["layers"] == 4 and api.db.job_cards.docs[0]["version"] == 1

    # The same business number is independent per tenant; company comes from the token.
    beta_created = _create(api, beta, "JC-SHARED-NO", key="beta")
    assert beta_created["job_card"]["company_id"] == COMPANY_BETA
    assert beta_created["job_card"]["id"] != jc_id
    assert api.client.get(f"/api/job-cards/{jc_id}", headers=alpha).json()["job_card"]["company_id"] == COMPANY_ALPHA


# ==============================================================================
# GET /api/job-cards/{job_card_id}
# ==============================================================================

def test_retrieve_package_success(api: Api) -> None:
    owner = api.member("owner")
    created = _create(api, owner, "JC-FETCH-01")
    reader = api.member("qc_inspector")

    response = api.client.get(f"/api/job-cards/{created['job_card']['id']}", headers=reader)
    assert response.status_code == 200
    pkg = response.json()
    assert pkg["job_card"]["id"] == created["job_card"]["id"]
    assert pkg["drill_sheet"]["id"] == created["drill_sheet"]["id"]
    assert pkg["process_sequence_sheet"]["id"] == created["process_sequence_sheet"]["id"]


def test_retrieve_missing_job_card_returns_404(api: Api) -> None:
    owner = api.member("owner")
    response = api.client.get("/api/job-cards/does-not-exist", headers=owner)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


@pytest.mark.parametrize("collection,update,expected_fragment", [
    ("drill_detail_sheets", {"id": "corrupted-drill-id"}, "drill_sheet_id mismatch"),
    ("process_sequence_sheets", {"id": "corrupted-proc-id"}, "process_sequence_sheet_id mismatch"),
    ("job_cards", {"drill_sheet_id": None}, "drill_sheet_id mismatch"),
    ("job_cards", {"process_sequence_sheet_id": None}, "process_sequence_sheet_id mismatch"),
    # Empty persisted pointers fail JobCard model validation (JobCardValidationError);
    # the router maps the common JobCardServiceError base to the same HTTP 422.
    ("job_cards", {"drill_sheet_id": ""}, "drill_sheet_id"),
    ("job_cards", {"process_sequence_sheet_id": "   "}, "process_sequence_sheet_id"),
])
def test_retrieve_invalid_linked_document_identity_returns_422(api: Api, collection: str, update: dict[str, Any],
                                                               expected_fragment: str) -> None:
    owner = api.member("owner")
    created = _create(api, owner, "JC-CORRUPT-01")
    jc_id = created["job_card"]["id"]
    for doc in getattr(api.db, collection).docs:
        doc.update(update)

    response = api.client.get(f"/api/job-cards/{jc_id}", headers=owner)
    assert response.status_code == 422
    assert expected_fragment in response.json()["detail"]


# ==============================================================================
# PATCH /api/job-cards/{job_card_id}
# ==============================================================================

def test_draft_update_success(api: Api) -> None:
    owner = api.member("owner")
    created = _create(api, owner, "JC-UPDATE-01")
    jc_id = created["job_card"]["id"]

    response = api.client.patch(f"/api/job-cards/{jc_id}", headers=owner, json={
        "expected_version": 1,
        "layers": 6,
        "pcb_dimensions": {"length_mm": 150.0, "width_mm": 80.0, "thickness_mm": 1.6},
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2 and body["layers"] == 6
    assert body["pcb_dimensions"]["length_mm"] == 150.0
    assert body["order_quantity_pcb"] == 500  # untouched fields preserved
    assert body["drill_sheet_id"] == created["job_card"]["drill_sheet_id"]
    assert body["process_sequence_sheet_id"] == created["job_card"]["process_sequence_sheet_id"]

    stored = api.db.job_cards.docs[0]
    assert stored["version"] == 2 and stored["layers"] == 6
    assert [a for a in api.db.audit_logs.docs if a["action"] == "job_card.updated"]

    # Package remains retrievable and identity-consistent after the update.
    assert api.client.get(f"/api/job-cards/{jc_id}", headers=owner).status_code == 200


def test_stale_expected_version_returns_409(api: Api) -> None:
    owner = api.member("owner")
    jc_id = _create(api, owner, "JC-STALE-01")["job_card"]["id"]

    first = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1, "layers": 6}, headers=owner)
    assert first.status_code == 200
    stale = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1, "layers": 8}, headers=owner)
    assert stale.status_code == 409
    assert "version conflict" in stale.json()["detail"]
    assert api.db.job_cards.docs[0]["layers"] == 6 and api.db.job_cards.docs[0]["version"] == 2


@pytest.mark.parametrize("body", [
    {"layers": 6},
    {"expected_version": 0, "layers": 6},
    {"expected_version": "abc", "layers": 6},
])
def test_missing_or_invalid_expected_version_returns_422(api: Api, body: dict[str, Any]) -> None:
    owner = api.member("owner")
    jc_id = _create(api, owner, "JC-NOVER-01")["job_card"]["id"]
    response = api.client.patch(f"/api/job-cards/{jc_id}", json=body, headers=owner)
    assert response.status_code == 422
    assert api.db.job_cards.docs[0]["version"] == 1


def test_empty_patch_returns_422(api: Api) -> None:
    owner = api.member("owner")
    jc_id = _create(api, owner, "JC-EMPTY-01")["job_card"]["id"]
    response = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1}, headers=owner)
    assert response.status_code == 422
    assert api.db.job_cards.docs[0]["version"] == 1


@pytest.mark.parametrize("status", ["approved", "released_to_production", "superseded", "engineering_review"])
def test_updating_locked_job_card_returns_409(api: Api, status: str) -> None:
    owner = api.member("owner")
    jc_id = _create(api, owner, f"JC-LOCK-{status}")["job_card"]["id"]
    api.db.job_cards.docs[0]["status"] = status

    response = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1, "layers": 6}, headers=owner)
    assert response.status_code == 409
    assert status in response.json()["detail"]
    assert api.db.job_cards.docs[0]["layers"] == 4


@pytest.mark.parametrize("field,value", [
    ("job_card_no", "JC-TAMPERED"),
    ("job_card_id", "tampered-id"),
    ("id", "tampered-id"),
    ("order_id", "ORD-TAMPERED"),
    ("order_no", "SO-TAMPERED"),
    ("customer_id", "CUST-TAMPERED"),
    ("part_number", "PN-TAMPERED"),
    ("revision", 2),
    ("company_id", COMPANY_BETA),
    ("drill_sheet_id", "drill-tampered"),
    ("process_sequence_sheet_id", "proc-tampered"),
    ("status", "approved"),
    ("version", 99),
    ("approved_by", "forged"),
    ("created_by", "forged"),
])
def test_identity_and_protected_field_modification_rejected(api: Api, field: str, value: Any) -> None:
    owner = api.member("owner")
    created = _create(api, owner, "JC-IMMUTABLE-01")
    jc_id = created["job_card"]["id"]
    before = copy.deepcopy(api.db.job_cards.docs[0])

    response = api.client.patch(f"/api/job-cards/{jc_id}", json={"expected_version": 1, field: value}, headers=owner)
    assert response.status_code == 422
    assert api.db.job_cards.docs[0] == before


# ==============================================================================
# Error mapping
# ==============================================================================

def _service_error_cases() -> list[tuple[type, int]]:
    from services import job_cards as svc
    return [
        (svc.JobCardNotFoundError, 404),
        (svc.DuplicateJobCardPackageError, 409),
        (svc.JobCardLockError, 409),
        (svc.JobCardIdentityMismatchError, 422),
        (svc.JobCardValidationError, 422),
    ]


@pytest.mark.parametrize("error_class,status", _service_error_cases())
def test_every_job_card_service_error_subclass_maps_to_its_status(api: Api, monkeypatch: pytest.MonkeyPatch,
                                                                  error_class: type, status: int) -> None:
    owner = api.member("owner")
    detail = f"descriptive {error_class.__name__} detail"

    async def raising(*args: Any, **kwargs: Any) -> Any:
        raise error_class(detail)

    for name in ("get_job_card_package", "update_draft_job_card", "create_job_card_package"):
        monkeypatch.setattr(api.router_module, name, raising)

    get = api.client.get("/api/job-cards/x", headers=owner)
    patch = api.client.patch("/api/job-cards/x", json={"expected_version": 1, "layers": 2}, headers=owner)
    post = api.client.post("/api/job-cards", json=_package_body(), headers={**owner, **_idem("mapping")})
    for response in (get, patch, post):
        assert response.status_code == status
        assert response.json() == {"detail": detail}


def test_base_job_card_service_error_preserves_status_and_detail(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    from services.job_cards import JobCardServiceError

    owner = api.member("owner")

    async def raising(*args: Any, **kwargs: Any) -> Any:
        raise JobCardServiceError(status_code=422, detail="base service error detail")

    monkeypatch.setattr(api.router_module, "get_job_card_package", raising)
    response = api.client.get("/api/job-cards/x", headers=owner)
    assert response.status_code == 422
    assert response.json() == {"detail": "base service error detail"}


@pytest.mark.parametrize("raised,status,detail", [
    (DuplicateKeyError("E11000 duplicate key collection: factoryos.job_cards index: secret_idx"), 409,
     "job card package already exists"),
    (PyMongoError("connection to mongo-internal:27017 refused; user=admin"), 503,
     "job card storage is temporarily unavailable"),
    (RuntimeError("Traceback ... internal stack detail"), 500, "job card request could not be completed"),
])
def test_database_and_unexpected_errors_are_sanitized(api: Api, monkeypatch: pytest.MonkeyPatch,
                                                      raised: Exception, status: int, detail: str) -> None:
    owner = api.member("owner")

    async def raising(*args: Any, **kwargs: Any) -> Any:
        raise raised

    monkeypatch.setattr(api.router_module, "get_job_card_package", raising)
    response = api.client.get("/api/job-cards/x", headers=owner)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    for secret in ("secret_idx", "mongo-internal", "admin", "Traceback"):
        assert secret not in response.text


# ==============================================================================
# server.py registration (isolated subprocess, fake Motor client, no DB access)
# ==============================================================================

def test_server_registers_job_card_routes() -> None:
    script = textwrap.dedent("""
        import motor.motor_asyncio as motor_asyncio

        class _FakeMotorClient:
            def __init__(self, *args, **kwargs):
                pass
            def __getitem__(self, name):
                return object()
            def close(self):
                pass

        motor_asyncio.AsyncIOMotorClient = _FakeMotorClient
        import server

        def _iter_route_entries(route_list):
            for item in route_list:
                effective = getattr(item, "effective_route_contexts", None)
                if callable(effective):
                    yield from effective()
                else:
                    yield item

        routes = sorted(
            f"{method} {path}"
            for entry in _iter_route_entries(server.app.routes)
            for path in [getattr(entry, "path", None)]
            if isinstance(path, str) and path.startswith("/api/job-cards")
            for method in (getattr(entry, "methods", None) or ())
        )
        print("\\n".join(routes))
    """)
    env = {key: value for key, value in os.environ.items() if "MONGO" not in key.upper()}
    env.update({
        "MONGO_URL": "mongodb://fake-user:fake@job-card-route-check.invalid:27017",
        "DB_NAME": "factoryos_security_test",
        "PYTHON_DOTENV_DISABLED": "1",
        "JWT_SECRET": os.environ["JWT_SECRET"],
    })
    result = subprocess.run([sys.executable, "-c", script], cwd=BACKEND_DIR, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    routes = set(result.stdout.split())
    lines = set(result.stdout.strip().splitlines())
    assert "POST /api/job-cards" in lines, routes
    assert "GET /api/job-cards/{job_card_id}" in lines
    assert "PATCH /api/job-cards/{job_card_id}" in lines
    assert "PATCH /api/job-cards/{job_card_id}/drill-sheet" in lines
    assert "PATCH /api/job-cards/{job_card_id}/process-sequence" in lines
    assert "POST /api/job-cards/{job_card_id}/submit-review" in lines
    assert "POST /api/job-cards/{job_card_id}/return-to-draft" in lines
    assert "POST /api/job-cards/{job_card_id}/approve" in lines
    assert "POST /api/job-cards/{job_card_id}/release" in lines
    assert "POST /api/job-cards/{job_card_id}/revisions" in lines


# ==============================================================================
# Day 1.9 — Parent Order validation, sub-sheet PATCH & lifecycle HTTP tests
# ==============================================================================


def test_create_parent_order_validation_errors(api: Api) -> None:
    owner = api.member("owner")

    # 1. Missing parent Order -> 404
    missing_ord = api.client.post(
        "/api/job-cards",
        json=_package_body("JC-NO-ORD", order_id="ord-missing"),
        headers={**owner, **_idem("no-ord")},
    )
    assert missing_ord.status_code == 404

    # 2. Cross-tenant parent Order -> 404
    api.seed_order(order_id="ord-beta-only", order_no="SO-BETA", customer_id="cust-beta", company_id=COMPANY_BETA)
    cross_ord = api.client.post(
        "/api/job-cards",
        json=_package_body("JC-CROSS-ORD", order_id="ord-beta-only", order_no="SO-BETA", customer_id="cust-beta"),
        headers={**owner, **_idem("cross-ord")},
    )
    assert cross_ord.status_code == 404

    # 3. Non-confirmed parent Order status -> 409
    for bad_status in ("awaiting_approval", "pending_review", "rejected", "in_production", "dispatched"):
        oid = f"ord-{bad_status}"
        api.seed_order(order_id=oid, status=bad_status)
        resp = api.client.post(
            "/api/job-cards",
            json=_package_body(f"JC-{bad_status}", order_id=oid),
            headers={**owner, **_idem(f"status-{bad_status}")},
        )
        assert resp.status_code == 409, resp.text

    # 4. Mismatched order_no or customer_id -> 422
    bad_no = api.client.post(
        "/api/job-cards",
        json=_package_body("JC-BAD-NO", order_no="SO-WRONG"),
        headers={**owner, **_idem("bad-no")},
    )
    assert bad_no.status_code == 422

    bad_cust = api.client.post(
        "/api/job-cards",
        json=_package_body("JC-BAD-CUST", customer_id="cust-wrong"),
        headers={**owner, **_idem("bad-cust")},
    )
    assert bad_cust.status_code == 422

    # 5. Initial creation with revision > 1 -> 422
    rev2 = api.client.post(
        "/api/job-cards",
        json=_package_body("JC-REV2-INIT", revision=2, revision_reason="Direct rev 2"),
        headers={**owner, **_idem("rev2-init")},
    )
    assert rev2.status_code == 422


def test_subsheet_patch_endpoints_and_lifecycle_transitions_e2e(api: Api) -> None:
    owner = api.member("owner")
    sales = api.member("sales_staff")  # read-only

    created = _create(api, owner, "JC-FLOW-01")
    jc_id = created["job_card"]["id"]

    # 1. PATCH /drill-sheet
    drill_res = api.client.patch(
        f"/api/job-cards/{jc_id}/drill-sheet",
        headers=owner,
        json={
            "expected_version": 1,
            "primary_drill": [{"tool_no": "T01", "drill_size_mm": 1.0, "hole_type": "PTH"}],
            "setup": {"stack_height": 2},
        },
    )
    assert drill_res.status_code == 200, drill_res.text
    assert drill_res.json()["version"] == 2
    assert drill_res.json()["primary_drill"][0]["drill_size_mm"] == 1.0

    # Stale drill sheet version -> 409
    stale_drill = api.client.patch(
        f"/api/job-cards/{jc_id}/drill-sheet",
        headers=owner,
        json={"expected_version": 1, "setup": {"stack_height": 3}},
    )
    assert stale_drill.status_code == 409

    # Read-only user gets 403 on sub-sheet PATCH and transitions
    assert api.client.patch(
        f"/api/job-cards/{jc_id}/drill-sheet",
        headers=sales,
        json={"expected_version": 2, "setup": {"stack_height": 3}},
    ).status_code == 403
    assert api.client.post(
        f"/api/job-cards/{jc_id}/submit-review",
        headers=sales,
        json={"expected_version": 1},
    ).status_code == 403

    # 2. PATCH /process-sequence (set stage 1 inactive, stage 2 and 3 active)
    proc_res = api.client.patch(
        f"/api/job-cards/{jc_id}/process-sequence",
        headers=owner,
        json={
            "expected_version": 1,
            "route_name": "Custom API Route",
            "processes": [
                {"process_seq": 1, "process_name": "PPC", "is_active": False},
                {"process_seq": 2, "process_name": "CNC Drilling", "is_active": True},
                {"process_seq": 3, "process_name": "Packing", "is_active": True},
            ],
        },
    )
    assert proc_res.status_code == 200, proc_res.text
    assert proc_res.json()["version"] == 2
    assert proc_res.json()["route_name"] == "Custom API Route"

    # Invalid process route (0 active processes) -> 422
    invalid_proc = api.client.patch(
        f"/api/job-cards/{jc_id}/process-sequence",
        headers=owner,
        json={
            "expected_version": 2,
            "processes": [{"process_seq": 1, "process_name": "PPC", "is_active": False}],
        },
    )
    assert invalid_proc.status_code == 422

    # 3. POST /submit-review (draft -> engineering_review)
    sub_res = api.client.post(
        f"/api/job-cards/{jc_id}/submit-review",
        headers=owner,
        json={"expected_version": 1},
    )
    assert sub_res.status_code == 200, sub_res.text
    assert sub_res.json()["job_card"]["status"] == "engineering_review"
    assert sub_res.json()["job_card"]["version"] == 2

    # 4. POST /return-to-draft (engineering_review -> draft)
    bad_ret = api.client.post(
        f"/api/job-cards/{jc_id}/return-to-draft",
        headers=owner,
        json={"expected_version": 2, "reason": "no"},
    )
    assert bad_ret.status_code == 422

    ret_res = api.client.post(
        f"/api/job-cards/{jc_id}/return-to-draft",
        headers=owner,
        json={"expected_version": 2, "reason": "Verify stack height with CAM lead"},
    )
    assert ret_res.status_code == 200, ret_res.text
    assert ret_res.json()["job_card"]["status"] == "draft"
    assert ret_res.json()["job_card"]["version"] == 3

    # Cannot return-to-draft from draft -> 409
    assert api.client.post(
        f"/api/job-cards/{jc_id}/return-to-draft",
        headers=owner,
        json={"expected_version": 3, "reason": "Already in draft"},
    ).status_code == 409

    # Re-submit -> engineering_review (version 4)
    sub_res_2 = api.client.post(
        f"/api/job-cards/{jc_id}/submit-review",
        headers=owner,
        json={"expected_version": 3},
    )
    assert sub_res_2.status_code == 200
    assert sub_res_2.json()["job_card"]["version"] == 4

    # 5. POST /approve (engineering_review -> approved, requires Idempotency-Key)
    assert api.client.post(
        f"/api/job-cards/{jc_id}/approve",
        headers=owner,
        json={"expected_version": 4},
    ).status_code == 422  # missing Idempotency-Key

    app_headers = {**owner, **_idem("approve-1")}
    app_res = api.client.post(
        f"/api/job-cards/{jc_id}/approve",
        headers=app_headers,
        json={"expected_version": 4},
    )
    assert app_res.status_code == 200, app_res.text
    assert app_res.json()["job_card"]["status"] == "approved"
    assert app_res.json()["job_card"]["version"] == 5
    assert app_res.json()["drill_sheet"]["is_locked"] is True
    assert app_res.json()["drill_sheet"]["version"] == 3

    # Idempotent replay on /approve returns 200 with identical body
    app_replay = api.client.post(
        f"/api/job-cards/{jc_id}/approve",
        headers=app_headers,
        json={"expected_version": 4},
    )
    assert app_replay.status_code == 200
    assert app_replay.json() == app_res.json()

    # Cannot return-to-draft from approved -> 409
    assert api.client.post(
        f"/api/job-cards/{jc_id}/return-to-draft",
        headers=owner,
        json={"expected_version": 5, "reason": "Attempt reopen approved card"},
    ).status_code == 409

    # 6. POST /revisions (approved -> rev 1 superseded + rev 2 draft, status 201)
    rev_headers = {**owner, **_idem("rev-create-1")}
    rev_res = api.client.post(
        f"/api/job-cards/{jc_id}/revisions",
        headers=rev_headers,
        json={"expected_version": 5, "revision_reason": "ECN-901: Add slot drill tool"},
    )
    assert rev_res.status_code == 201, rev_res.text
    rev2_pkg = rev_res.json()
    rev2_id = rev2_pkg["job_card"]["id"]
    assert rev2_pkg["job_card"]["revision"] == 2
    assert rev2_pkg["job_card"]["revision_code"] == "REV-02"
    assert rev2_pkg["job_card"]["status"] == "draft"
    assert rev2_pkg["job_card"]["previous_revision_id"] == jc_id
    assert rev2_pkg["drill_sheet"]["is_locked"] is False

    # Idempotent replay on /revisions returns 201 with identical package
    rev_replay = api.client.post(
        f"/api/job-cards/{jc_id}/revisions",
        headers=rev_headers,
        json={"expected_version": 5, "revision_reason": "ECN-901: Add slot drill tool"},
    )
    assert rev_replay.status_code == 201
    assert rev_replay.json() == rev2_pkg

    # Verify rev 1 is superseded
    rev1_get = api.client.get(f"/api/job-cards/{jc_id}", headers=owner)
    assert rev1_get.status_code == 200
    assert rev1_get.json()["job_card"]["status"] == "superseded"
    assert rev1_get.json()["job_card"]["superseded_by_revision_id"] == rev2_id

    # 7. Submit, approve, and release rev 2 (approved -> released_to_production)
    assert api.client.post(
        f"/api/job-cards/{rev2_id}/submit-review",
        headers=owner,
        json={"expected_version": 1},
    ).status_code == 200
    assert api.client.post(
        f"/api/job-cards/{rev2_id}/approve",
        headers={**owner, **_idem("approve-rev2")},
        json={"expected_version": 2},
    ).status_code == 200

    rel_headers = {**owner, **_idem("release-rev2")}
    rel_res = api.client.post(
        f"/api/job-cards/{rev2_id}/release",
        headers=rel_headers,
        json={"expected_version": 3},
    )
    assert rel_res.status_code == 200, rel_res.text
    rel_pkg = rel_res.json()
    assert rel_pkg["job_card"]["status"] == "released_to_production"
    assert rel_pkg["process_sequence_sheet"]["current_process_seq"] == 2
    assert rel_pkg["process_sequence_sheet"]["processes"][0]["status"] == "skipped"
    assert rel_pkg["process_sequence_sheet"]["processes"][1]["status"] == "waiting"

    # Idempotent replay on /release returns 200
    rel_replay = api.client.post(
        f"/api/job-cards/{rev2_id}/release",
        headers=rel_headers,
        json={"expected_version": 3},
    )
    assert rel_replay.status_code == 200
    assert rel_replay.json() == rel_pkg

    # ECN revision from released_to_production is rejected in Day 1.9 -> 409
    rel_rev = api.client.post(
        f"/api/job-cards/{rev2_id}/revisions",
        headers={**owner, **_idem("rev-from-released")},
        json={"expected_version": 4, "revision_reason": "Attempt revision from released"},
    )
    assert rel_rev.status_code == 409

