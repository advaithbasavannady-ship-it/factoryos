"""Real MongoDB replica-set integration tests for the Job Cards subsystem (Day 1.7D / 1.7D.1).

Executed via ``backend/tests/run_isolated.py`` under ``FACTORYOS_TEST_LANE=integration``
against the isolated ``factoryos_security_test`` database on a live single-node
MongoDB replica set (``rs0``).

Covers:
A. Atomic creation of all three Job Card documents.
B. Transaction rollback when a child document write fails after master insert.
C. Audit durability via the audit-sentinel mechanism (HTTP 503 + rollback, then retry).
D. Idempotency replay (full payload comparison) and conflicting-payload rejection (HTTP 409).
E. Concurrent idempotency requests sharing one Idempotency-Key (synchronized with Barrier, all 201).
F. Concurrent Job Card creation with identical (company_id, job_card_no, revision) and distinct keys.
G. Optimistic concurrency under concurrent PATCH updates with the same expected_version.
H. Tenant isolation across two companies in real MongoDB using a run-scoped secondary identity.
I. Unique index inspection (all 6 indexes + created_first/created_second), negative preflight
   anomaly detection (duplicates, missing vs null, missing company_id, empty/whitespace, wrong
   BSON types, array keys, invalid revisions), wrong-DB/DB_NAME rejection, and cross-tenant E11000.
J. Scoped cleanup of synthetic test data (including RequestAuditMiddleware 'job-cards' logs
   and run-scoped secondary tenant identities) with idempotent rerun verification.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from helpers import auth_headers, idempotency_headers, new_idempotency_key

FORBIDDEN_ROLLBACK_ROUTE = "__test_abort_after_master_insert__"


def _test_url(client: httpx.Client, path: str) -> str:
    return str(client.base_url.copy_with(path=path))


def _run_header(client: httpx.Client) -> tuple[dict[str, str], dict[str, Any]]:
    identity = client.get(_test_url(client, "/__test__/identity")).json()
    assert identity["database_name"] == "factoryos_security_test"
    return {"X-Test-Run-ID": identity["run_id"]}, identity


def _request_id_header(run_id: str) -> dict[str, str]:
    return {"X-Request-ID": f"tscheck-jc-{run_id[:12]}-{uuid.uuid4().hex[:8]}"}


def _primary_company_id(client: httpx.Client) -> str:
    run_header, identity = _run_header(client)
    client.headers.update({
        **auth_headers(os.environ["TEST_ACCESS_TOKEN"]),
        **_request_id_header(identity["run_id"]),
    })
    session = client.get("/auth/session")
    assert session.status_code == 200, session.text
    return session.json()["user"]["company_id"]


def _seed_confirmed_order(
    client: httpx.Client,
    company_id: str,
    *,
    order_id: str | None = None,
    order_no: str | None = None,
    customer_id: str = "cust-tscheck-001",
    customer_name: str = "Apex Precision Test Customer",
    status: str = "confirmed",
) -> dict[str, Any]:
    run_header, _ = _run_header(client)
    oid = order_id or f"ord-tscheck-{uuid.uuid4().hex[:10]}"
    ono = order_no or f"SO-TSCHECK-{oid[-8:].upper()}"
    res = client.post(
        _test_url(client, "/__test__/job-cards/seed-order"),
        headers=run_header,
        json={
            "company_id": company_id,
            "order_id": oid,
            "order_no": ono,
            "customer_id": customer_id,
            "customer_name": customer_name,
            "status": status,
        },
    )
    assert res.status_code == 200, res.text
    return res.json()


def _package_payload(
    job_card_no: str,
    order_info: dict[str, Any] | None = None,
    **master_overrides: Any,
) -> dict[str, Any]:
    ord_data = order_info or {}
    master = {
        "job_card_no": job_card_no,
        "order_id": ord_data.get("order_id", "ord-tscheck-001"),
        "order_no": ord_data.get("order_no", "SO-TSCHECK-0042"),
        "part_number": "PCB-TSCHECK-REV1",
        "customer_id": ord_data.get("customer_id", "cust-tscheck-001"),
        "customer_name": ord_data.get("customer_name", "Apex Precision Test Customer"),
        "order_quantity_pcb": 400,
        "planned_panel_quantity": 80,
        "layers": 4,
        **master_overrides,
    }
    return {
        "job_card": master,
        "drill_sheet": {
            "primary_drill": [
                {"tool_no": "T01", "drill_size_mm": 0.8, "finish_size_mm": 0.7, "hole_type": "PTH"}
            ]
        },
    }


@pytest.fixture(scope="module", autouse=True)
def _module_indexes_and_teardown(backend_url: str):
    """Apply Job Card indexes once at module start and guarantee scoped cleanup on module exit."""
    with httpx.Client(base_url=f"{backend_url}/api", timeout=30.0) as mod_client:
        run_header, _ = _run_header(mod_client)
        setup = mod_client.post(_test_url(mod_client, "/__test__/job-cards/setup-indexes"), headers=run_header)
        assert setup.status_code == 200, setup.text
        try:
            yield
        finally:
            mod_client.post(
                _test_url(mod_client, "/__test__/job-cards/rollback-validator"),
                headers=run_header,
                json={"enabled": False, "forbidden_route_name": FORBIDDEN_ROLLBACK_ROUTE},
            )
            mod_client.post(_test_url(mod_client, "/__test__/job-cards/cleanup"), headers=run_header)


@pytest.fixture(autouse=True)
def _reset_rollback_validator_after_test(client: httpx.Client):
    yield
    run_header, _ = _run_header(client)
    client.post(
        _test_url(client, "/__test__/job-cards/rollback-validator"),
        headers=run_header,
        json={"enabled": False, "forbidden_route_name": FORBIDDEN_ROLLBACK_ROUTE},
    )


# ==============================================================================
# A. Atomic package creation
# ==============================================================================

def test_a_atomic_package_creation_commits_all_three_documents(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)
    jc_no = f"JC-TSCHECK-ATOMIC-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)

    response = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert response.status_code == 201, response.text
    pkg = response.json()
    jc_id = pkg["job_card"]["id"]

    assert pkg["job_card"]["drill_sheet_id"] == pkg["drill_sheet"]["id"]
    assert pkg["job_card"]["process_sequence_sheet_id"] == pkg["process_sequence_sheet"]["id"]
    assert len(pkg["process_sequence_sheet"]["processes"]) == 24

    # Verify via HTTP GET
    fetched = client.get(f"/job-cards/{jc_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == pkg

    # Verify physical MongoDB collections contain all 3 linked documents + idempotency record
    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    )
    assert state.status_code == 200, state.text
    info = state.json()
    assert info["job_cards_count"] == 1
    assert info["drill_sheets_count"] == 1
    assert info["process_sheets_count"] == 1
    assert info["idempotency_count"] == 1


# ==============================================================================
# B. Transaction rollback when child document write fails after master insert
# ==============================================================================

def test_b_transaction_rollback_when_child_document_insert_fails(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)
    jc_no = f"JC-TSCHECK-ROLLBACK-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)

    # Enable a real MongoDB collection validator on process_sequence_sheets (the 3rd insert)
    enable = client.post(
        _test_url(client, "/__test__/job-cards/rollback-validator"),
        headers=run_header,
        json={"enabled": True, "forbidden_route_name": FORBIDDEN_ROLLBACK_ROUTE},
    )
    assert enable.status_code == 200, enable.text

    payload = _package_payload(jc_no, ord_info)
    payload["process_sequence_sheet"] = {"route_name": FORBIDDEN_ROLLBACK_ROUTE}

    failed = client.post(
        "/job-cards",
        json=payload,
        headers=idempotency_headers(new_idempotency_key()),
    )
    # PyMongoError (WriteError code 121 on the 3rd insert) aborts the transaction and maps to 503
    assert failed.status_code == 503, failed.text

    # Disable and restore previous validator state
    disable = client.post(
        _test_url(client, "/__test__/job-cards/rollback-validator"),
        headers=run_header,
        json={"enabled": False, "forbidden_route_name": FORBIDDEN_ROLLBACK_ROUTE},
    )
    assert disable.status_code == 200, disable.text

    # Verify WiredTiger rolled back the already-executed job_cards and drill_detail_sheets inserts
    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["job_cards_count"] == 0
    assert state["drill_sheets_count"] == 0
    assert state["process_sheets_count"] == 0
    assert state["idempotency_count"] == 0


# ==============================================================================
# C. Audit durability via audit-sentinel mechanism
# ==============================================================================

def test_c_audit_durability_aborts_package_on_audit_failure_and_succeeds_on_retry(
    client: httpx.Client,
) -> None:
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)
    jc_no = f"JC-TSCHECK-AUDIT-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)

    key = new_idempotency_key()
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    event_id = hashlib.sha256(f"factoryos-audit:{company_id}:{key_hash}".encode("utf-8")).hexdigest()

    sentinel = client.post(
        _test_url(client, "/__test__/audit-sentinel"),
        headers=run_header,
        json={"event_id": event_id},
    )
    assert sentinel.status_code == 200, sentinel.text

    # Attempt creation while audit _id collides -> must return 503 and abort all 3 document writes
    failed = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(key),
    )
    assert failed.status_code == 503, failed.text

    state_after_fail = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state_after_fail["job_cards_count"] == 0
    assert state_after_fail["drill_sheets_count"] == 0
    assert state_after_fail["process_sheets_count"] == 0
    assert state_after_fail["idempotency_count"] == 0

    # Remove sentinel and retry with the same Idempotency-Key
    removed = client.delete(
        _test_url(client, f"/__test__/audit-sentinel/{event_id}"),
        headers=run_header,
    )
    assert removed.status_code == 200 and removed.json()["deleted"] is True

    succeeded = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(key),
    )
    assert succeeded.status_code == 201, succeeded.text

    state_after_retry = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state_after_retry["job_cards_count"] == 1
    assert state_after_retry["drill_sheets_count"] == 1
    assert state_after_retry["process_sheets_count"] == 1
    assert state_after_retry["idempotency_count"] == 1

    audit = client.get(_test_url(client, f"/__test__/audit-event/{event_id}"), headers=run_header)
    assert audit.status_code == 200
    event = audit.json()["event"]
    assert event and event["action"] == "job_card.created"
    assert event["tenant_id"] == company_id
    assert event["user_id"]


# ==============================================================================
# D. Idempotency replay and conflict
# ==============================================================================

def test_d_idempotency_replay_and_payload_conflict(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)
    jc_no = f"JC-TSCHECK-IDEM-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)
    key = new_idempotency_key()

    first = client.post("/job-cards", json=_package_payload(jc_no, ord_info), headers=idempotency_headers(key))
    assert first.status_code == 201, first.text

    replay = client.post("/job-cards", json=_package_payload(jc_no, ord_info), headers=idempotency_headers(key))
    assert replay.status_code == 201, replay.text
    assert replay.json() == first.json()

    conflict = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info, layers=8),
        headers=idempotency_headers(key),
    )
    assert conflict.status_code == 409, conflict.text

    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["job_cards_count"] == 1
    assert state["drill_sheets_count"] == 1
    assert state["process_sheets_count"] == 1
    assert state["idempotency_count"] == 1


# ==============================================================================
# E. Concurrent idempotency with the same Idempotency-Key
# ==============================================================================

def test_e_concurrent_idempotency_same_key_recovers_without_duplicates(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, identity = _run_header(client)
    jc_no = f"JC-TSCHECK-CONCIDEM-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)
    shared_key = new_idempotency_key()
    base_url = str(client.base_url).rstrip("/")
    headers = {
        **auth_headers(os.environ["TEST_ACCESS_TOKEN"]),
        **idempotency_headers(shared_key),
        **_request_id_header(identity["run_id"]),
    }
    payload = _package_payload(jc_no, ord_info)
    workers = 6
    barrier = threading.Barrier(workers)

    def submit_once(_idx: int) -> httpx.Response:
        with httpx.Client(base_url=base_url, headers=headers, timeout=30.0) as w:
            barrier.wait(timeout=15.0)
            return w.post("/job-cards", json=payload)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(submit_once, range(workers)))

    # Every worker sharing the same tenant, actor, payload, and Idempotency-Key must
    # receive HTTP 201 with the canonical package (HTTP 409 is not an acceptable replay).
    assert all(r.status_code == 201 for r in responses), [
        (r.status_code, r.text) for r in responses
    ]

    canonical_body = responses[0].json()
    canonical_jc_id = canonical_body["job_card"]["id"]
    canonical_drill_id = canonical_body["drill_sheet"]["id"]
    canonical_proc_id = canonical_body["process_sequence_sheet"]["id"]
    for resp in responses:
        body = resp.json()
        assert body["job_card"]["id"] == canonical_jc_id
        assert body["drill_sheet"]["id"] == canonical_drill_id
        assert body["process_sequence_sheet"]["id"] == canonical_proc_id

    # A subsequent deterministic replay with the shared key must return 201 with the exact package
    replay = client.post("/job-cards", json=payload, headers=idempotency_headers(shared_key))
    assert replay.status_code == 201, replay.text
    assert replay.json() == canonical_body

    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["job_cards_count"] == 1
    assert state["drill_sheets_count"] == 1
    assert state["process_sheets_count"] == 1
    assert state["idempotency_count"] == 1


# ==============================================================================
# F. Concurrent Job Card creation with identical business key & distinct keys
# ==============================================================================

def test_f_concurrent_job_card_creation_enforces_unique_business_key(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, identity = _run_header(client)
    jc_no = f"JC-TSCHECK-RACE-{uuid.uuid4().hex[:8].upper()}"
    base_url = str(client.base_url).rstrip("/")
    base_headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    workers = 8
    keys = [new_idempotency_key() for _ in range(workers)]
    orders = [_seed_confirmed_order(client, company_id) for _ in range(workers)]
    barrier = threading.Barrier(workers)

    def create_competing(idx: int) -> httpx.Response:
        h = {
            **base_headers,
            **idempotency_headers(keys[idx]),
            **_request_id_header(identity["run_id"]),
        }
        with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
            barrier.wait(timeout=15.0)
            return w.post("/job-cards", json=_package_payload(jc_no, orders[idx], order_quantity_pcb=100 + idx))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(create_competing, range(workers)))

    statuses = [r.status_code for r in responses]
    created_201 = [r for r in responses if r.status_code == 201]
    conflicts_409 = [r for r in responses if r.status_code == 409]
    assert len(created_201) == 1, f"Expected exactly 1 winner (201), got statuses={statuses}"
    assert len(conflicts_409) == workers - 1, f"Expected {workers - 1} conflicts (409), got statuses={statuses}"

    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["job_cards_count"] == 1
    assert state["drill_sheets_count"] == 1
    assert state["process_sheets_count"] == 1
    assert state["idempotency_count"] == 1


# ==============================================================================
# G. Optimistic concurrency under concurrent PATCH updates
# ==============================================================================

def test_g_optimistic_concurrency_under_concurrent_patch_updates(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, identity = _run_header(client)
    jc_no = f"JC-TSCHECK-OCC-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)
    created = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert created.status_code == 201, created.text
    jc_id = created.json()["job_card"]["id"]

    base_url = str(client.base_url).rstrip("/")
    base_headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    workers = 8
    barrier = threading.Barrier(workers)

    def patch_once(idx: int) -> httpx.Response:
        h = {**base_headers, **_request_id_header(identity["run_id"])}
        with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
            barrier.wait(timeout=15.0)
            return w.patch(
                f"/job-cards/{jc_id}",
                json={"expected_version": 1, "order_quantity_pcb": 600 + idx},
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(patch_once, range(workers)))

    statuses = [r.status_code for r in responses]
    winners = [r for r in responses if r.status_code == 200]
    losers = [r for r in responses if r.status_code == 409]
    assert len(winners) == 1, f"Expected exactly 1 PATCH winner (200), got statuses={statuses}"
    assert len(losers) == workers - 1, f"Expected {workers - 1} stale conflicts (409), got statuses={statuses}"
    winning_body = winners[0].json()
    assert winning_body["version"] == 2

    fetched = client.get(f"/job-cards/{jc_id}")
    assert fetched.status_code == 200
    assert fetched.json()["job_card"]["version"] == 2
    assert fetched.json()["job_card"]["order_quantity_pcb"] == winning_body["order_quantity_pcb"]

    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["update_audit_count"] == 1


# ==============================================================================
# H. Tenant isolation across two companies in real MongoDB
# ==============================================================================

def test_h_tenant_isolation_across_two_companies_in_real_mongodb(client: httpx.Client) -> None:
    alpha_company = _primary_company_id(client)
    run_header, identity = _run_header(client)
    alpha_headers = {
        **auth_headers(os.environ["TEST_ACCESS_TOKEN"]),
        **_request_id_header(identity["run_id"]),
    }
    shared_jc_no = f"JC-TSCHECK-TENANT-{uuid.uuid4().hex[:8].upper()}"
    shared_ord_id = f"ord-tscheck-shared-{uuid.uuid4().hex[:8]}"
    alpha_ord = _seed_confirmed_order(client, alpha_company, order_id=shared_ord_id)

    # Create package in Company Alpha
    alpha_res = client.post(
        "/job-cards",
        json=_package_payload(shared_jc_no, alpha_ord),
        headers={**alpha_headers, **idempotency_headers(new_idempotency_key())},
    )
    assert alpha_res.status_code == 201, alpha_res.text
    alpha_jc_id = alpha_res.json()["job_card"]["id"]

    # Provision a run-scoped secondary company identity with job_cards.manage (production_manager)
    provisioned = client.post(
        _test_url(client, "/__test__/job-cards/secondary-tenant-session"),
        headers=run_header,
    )
    assert provisioned.status_code == 200, provisioned.text
    sec_info = provisioned.json()
    beta_company = sec_info["company_id"]
    assert beta_company != alpha_company
    beta_headers = {
        **auth_headers(sec_info["access_token"]),
        **_request_id_header(identity["run_id"]),
    }

    # Company Beta cannot GET or PATCH Company Alpha's Job Card
    assert client.get(f"/job-cards/{alpha_jc_id}", headers=beta_headers).status_code == 404
    beta_patch = client.patch(
        f"/job-cards/{alpha_jc_id}",
        json={"expected_version": 1, "layers": 12},
        headers=beta_headers,
    )
    assert beta_patch.status_code == 404

    # Company Beta can create a Job Card with the exact same (job_card_no, revision) and order_id
    beta_ord = _seed_confirmed_order(client, beta_company, order_id=shared_ord_id)
    beta_res = client.post(
        "/job-cards",
        json=_package_payload(shared_jc_no, beta_ord, layers=6),
        headers={**beta_headers, **idempotency_headers(new_idempotency_key())},
    )
    assert beta_res.status_code == 201, beta_res.text
    beta_pkg = beta_res.json()
    assert beta_pkg["job_card"]["company_id"] == beta_company
    assert beta_pkg["job_card"]["id"] != alpha_jc_id

    # Verify Company Alpha's document remains intact
    alpha_check = client.get(f"/job-cards/{alpha_jc_id}", headers=alpha_headers)
    assert alpha_check.status_code == 200
    assert alpha_check.json()["job_card"]["company_id"] == alpha_company
    assert alpha_check.json()["job_card"]["layers"] == 4


# ==============================================================================
# I. Unique index verification, negative preflight anomalies & E11000 enforcement
# ==============================================================================

def test_i_unique_index_verification_and_duplicate_preflight_enforcement(
    client: httpx.Client,
) -> None:
    run_header, _ = _run_header(client)
    setup = client.post(_test_url(client, "/__test__/job-cards/setup-indexes"), headers=run_header)
    assert setup.status_code == 200, setup.text
    data = setup.json()
    assert data["preflight"] == {}
    indexes = data["indexes"]

    expected_specs = {
        "job_cards": {
            "company_job_card_no_revision_unique": [["company_id", 1], ["job_card_no", 1], ["revision", 1]],
            "company_job_card_id_unique": [["company_id", 1], ["id", 1]],
            "company_active_order_job_card_unique": [["company_id", 1], ["order_id", 1]],
        },
        "drill_detail_sheets": {
            "company_drill_job_card_unique": [["company_id", 1], ["job_card_id", 1]],
            "company_drill_sheet_id_unique": [["company_id", 1], ["id", 1]],
        },
        "process_sequence_sheets": {
            "company_process_job_card_unique": [["company_id", 1], ["job_card_id", 1]],
            "company_process_sheet_id_unique": [["company_id", 1], ["id", 1]],
        },
    }
    for col_name, col_expected in expected_specs.items():
        assert set(data["created_first"][col_name]) == set(col_expected.keys())
        assert set(data["created_second"][col_name]) == set(col_expected.keys())
        col_indexes = indexes[col_name]
        for idx_name, expected_key in col_expected.items():
            assert idx_name in col_indexes, f"Missing index {idx_name} on {col_name}"
            assert col_indexes[idx_name]["unique"] is True
            assert col_indexes[idx_name]["key"] == expected_key

    # Verify partialFilterExpression on company_active_order_job_card_unique
    active_ord_idx = indexes["job_cards"]["company_active_order_job_card_unique"]
    assert active_ord_idx["partialFilterExpression"] == {
        "status": {
            "$in": ["draft", "engineering_review", "approved", "released_to_production"],
        }
    }

    enforcement = client.post(
        _test_url(client, "/__test__/job-cards/verify-unique-indexes"),
        headers=run_header,
    )
    assert enforcement.status_code == 200, enforcement.text
    result = enforcement.json()
    assert result["wrong_db_rejected"] is True
    assert result["wrong_db_preflight_rejected"] is True
    assert result["wrong_db_apply_rejected"] is True
    assert result["wrong_env_preflight_rejected"] is True
    assert result["wrong_env_apply_rejected"] is True
    assert result["superseded_coexistence_allowed"] is True
    assert set(result["cross_tenant_allowed"]) == {
        "job_cards",
        "drill_detail_sheets",
        "process_sequence_sheets",
    }
    assert set(result["enforced"]) == {
        "job_cards.company_job_card_no_revision_unique",
        "job_cards.company_job_card_id_unique",
        "job_cards.company_active_order_job_card_unique",
        "drill_detail_sheets.company_drill_job_card_unique",
        "drill_detail_sheets.company_drill_sheet_id_unique",
        "process_sequence_sheets.company_process_job_card_unique",
        "process_sequence_sheets.company_process_sheet_id_unique",
    }

    anomalies = client.post(
        _test_url(client, "/__test__/job-cards/verify-preflight-anomalies"),
        headers=run_header,
    )
    assert anomalies.status_code == 200, anomalies.text
    anomaly_results = anomalies.json()
    expected_anomaly_cases = {
        "missing_vs_null_collision",
        "duplicate_business_keys",
        "missing_company_id",
        "empty_and_whitespace_strings",
        "wrong_bson_types",
        "array_valued_keys",
        "invalid_revision_values",
        "duplicate_active_order_job_cards",
        "superseded_order_job_cards_allowed",
        "invalid_status_and_order_id_anomalies",
        "clean_collections_restored",
    }
    assert set(anomaly_results.keys()) == expected_anomaly_cases
    for case_name, passed in anomaly_results.items():
        assert passed is True, f"Preflight anomaly check failed for {case_name}: {anomaly_results}"


# ==============================================================================
# K. Day 1.9 — Parent Order validation & concurrent single active Job Card per Order
# ==============================================================================

def test_k_parent_order_validation_and_concurrent_single_active_job_card_per_order(
    client: httpx.Client,
) -> None:
    company_id = _primary_company_id(client)
    run_header, identity = _run_header(client)

    # 1. Missing parent Order -> 404
    missing_res = client.post(
        "/job-cards",
        json=_package_payload(
            f"JC-TSCHECK-NOORD-{uuid.uuid4().hex[:6].upper()}",
            {"order_id": "ord-tscheck-nonexistent", "order_no": "SO-TSCHECK-NONE", "customer_id": "cust-1"},
        ),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert missing_res.status_code == 404, missing_res.text

    # 2. Ineligible parent Order status -> 409
    pending_ord = _seed_confirmed_order(client, company_id, status="pending_review")
    pending_res = client.post(
        "/job-cards",
        json=_package_payload(f"JC-TSCHECK-PEND-{uuid.uuid4().hex[:6].upper()}", pending_ord),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert pending_res.status_code == 409, pending_res.text

    # 2. Cross-tenant parent Order -> 404 (with zero documents, idempotency, or audit records persisted)
    sec_resp = client.post(_test_url(client, "/__test__/job-cards/secondary-tenant-session"), headers=run_header)
    assert sec_resp.status_code == 200, sec_resp.text
    sec_company_id = sec_resp.json()["company_id"]
    cross_tenant_ord = _seed_confirmed_order(client, sec_company_id, status="confirmed")
    cross_jc_no = f"JC-TSCHECK-XORD-{uuid.uuid4().hex[:6].upper()}"
    cross_res = client.post(
        "/job-cards",
        json=_package_payload(cross_jc_no, cross_tenant_ord),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert cross_res.status_code == 404, cross_res.text
    cross_state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{cross_jc_no}"),
        headers=run_header,
    ).json()
    assert cross_state["job_cards_count"] == 0
    assert cross_state["drill_sheets_count"] == 0
    assert cross_state["process_sheets_count"] == 0
    assert cross_state["idempotency_count"] == 0
    assert cross_state["audit_actions"] == []

    # 3. All eight non-confirmed Order statuses rejected on initial Job Card creation -> 409
    non_confirmed_statuses = (
        "awaiting_approval",
        "correction_requested",
        "rejected",
        "pending_review",
        "in_production",
        "quality_check",
        "ready_for_dispatch",
        "dispatched",
    )
    for bad_status in non_confirmed_statuses:
        non_conf_ord = _seed_confirmed_order(client, company_id, status=bad_status)
        non_conf_jc_no = f"JC-TSCHECK-ST-{bad_status[:6].upper()}-{uuid.uuid4().hex[:6].upper()}"
        non_conf_res = client.post(
            "/job-cards",
            json=_package_payload(non_conf_jc_no, non_conf_ord),
            headers=idempotency_headers(new_idempotency_key()),
        )
        assert non_conf_res.status_code == 409, f"Expected 409 for Order status={bad_status}, got {non_conf_res.status_code}: {non_conf_res.text}"
        non_conf_state = client.get(
            _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{non_conf_jc_no}"),
            headers=run_header,
        ).json()
        assert non_conf_state["job_cards_count"] == 0
        assert non_conf_state["drill_sheets_count"] == 0
        assert non_conf_state["process_sheets_count"] == 0
        assert non_conf_state["idempotency_count"] == 0
        assert non_conf_state["audit_actions"] == []

    # 4. Initial revision > 1 rejected with 422
    valid_ord = _seed_confirmed_order(client, company_id)
    rev_gt1_jc_no = f"JC-TSCHECK-REVGT1-{uuid.uuid4().hex[:6].upper()}"
    rev_gt1_payload = _package_payload(rev_gt1_jc_no, valid_ord)
    rev_gt1_payload["job_card"]["revision"] = 2
    rev_gt1_payload["job_card"]["revision_reason"] = "Attempted revision > 1 on initial create"
    rev_gt1_payload["drill_sheet"]["revision"] = 2
    rev_gt1_res = client.post(
        "/job-cards",
        json=rev_gt1_payload,
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert rev_gt1_res.status_code == 422, rev_gt1_res.text
    rev_gt1_state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{rev_gt1_jc_no}"),
        headers=run_header,
    ).json()
    assert rev_gt1_state["job_cards_count"] == 0
    assert rev_gt1_state["idempotency_count"] == 0
    assert rev_gt1_state["audit_actions"] == []

    # 5. Mismatched order_no / customer_id -> 422
    bad_no_res = client.post(
        "/job-cards",
        json=_package_payload(
            f"JC-TSCHECK-BADNO-{uuid.uuid4().hex[:6].upper()}",
            valid_ord,
            order_no="SO-TSCHECK-WRONG",
        ),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert bad_no_res.status_code == 422, bad_no_res.text

    bad_cust_res = client.post(
        "/job-cards",
        json=_package_payload(
            f"JC-TSCHECK-BADCUST-{uuid.uuid4().hex[:6].upper()}",
            valid_ord,
            customer_id="cust-tscheck-wrong",
        ),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert bad_cust_res.status_code == 422, bad_cust_res.text

    # 4. Concurrent Job Card creation for the SAME confirmed Order with DISTINCT job_card_no values
    shared_ord = _seed_confirmed_order(client, company_id)
    base_url = str(client.base_url).rstrip("/")
    base_headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    workers = 8
    tag = uuid.uuid4().hex[:6].upper()
    jc_nos = [f"JC-TSCHECK-ORDRACE-{tag}-{idx}" for idx in range(workers)]
    keys = [new_idempotency_key() for _ in range(workers)]
    barrier = threading.Barrier(workers)

    def create_for_same_order(idx: int) -> httpx.Response:
        h = {
            **base_headers,
            **idempotency_headers(keys[idx]),
            **_request_id_header(identity["run_id"]),
        }
        with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
            barrier.wait(timeout=15.0)
            return w.post("/job-cards", json=_package_payload(jc_nos[idx], shared_ord))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        responses = list(pool.map(create_for_same_order, range(workers)))

    statuses = [r.status_code for r in responses]
    winners = [r for r in responses if r.status_code == 201]
    losers = [r for r in responses if r.status_code == 409]
    assert len(winners) == 1, f"Expected 1 winner (201) for same order, got statuses={statuses}"
    assert len(losers) == workers - 1, f"Expected {workers - 1} conflicts (409), got statuses={statuses}"


# ==============================================================================
# L. Day 1.9 — Full pre-production lifecycle, sub-sheet updates & ECN revision chain
# ==============================================================================

def test_l_full_pre_production_lifecycle_subsheet_updates_and_ecn_revision_chain(
    client: httpx.Client,
) -> None:
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)
    ord_info = _seed_confirmed_order(client, company_id)
    jc_no = f"JC-TSCHECK-LIFE-{uuid.uuid4().hex[:8].upper()}"

    # 1. Create initial draft revision 1
    create_res = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert create_res.status_code == 201, create_res.text
    rev1_id = create_res.json()["job_card"]["id"]

    # 2. Update Drill Details Sheet and Process Sequence Sheet in draft
    drill_res = client.patch(
        f"/job-cards/{rev1_id}/drill-sheet",
        json={
            "expected_version": 1,
            "primary_drill": [
                {"tool_no": "T01", "drill_size_mm": 0.85, "finish_size_mm": 0.75, "hole_type": "PTH"}
            ],
            "setup": {"stack_height": 3, "drill_program_name": "TSCHECK_REV1.DRL"},
        },
    )
    assert drill_res.status_code == 200, drill_res.text
    assert drill_res.json()["version"] == 2

    # Stale expected_version on drill-sheet PATCH rejected with 409 and leaves sheet unchanged
    stale_drill_res = client.patch(
        f"/job-cards/{rev1_id}/drill-sheet",
        json={
            "expected_version": 1,
            "setup": {"stack_height": 9, "drill_program_name": "STALE_OVERWRITE.DRL"},
        },
    )
    assert stale_drill_res.status_code == 409, stale_drill_res.text

    proc_res = client.patch(
        f"/job-cards/{rev1_id}/process-sequence",
        json={
            "expected_version": 1,
            "route_name": "Live 3-Stage PCB Traveler",
            "processes": [
                {"process_seq": 1, "process_name": "PPC", "is_active": False},
                {"process_seq": 2, "process_name": "CNC Drilling", "is_active": True},
                {"process_seq": 3, "process_name": "Packing", "is_active": True},
            ],
        },
    )
    assert proc_res.status_code == 200, proc_res.text
    assert proc_res.json()["version"] == 2

    # Stale expected_version on process-sequence PATCH rejected with 409 and leaves sheet unchanged
    stale_proc_res = client.patch(
        f"/job-cards/{rev1_id}/process-sequence",
        json={
            "expected_version": 1,
            "route_name": "Stale Overwrite Traveler",
        },
    )
    assert stale_proc_res.status_code == 409, stale_proc_res.text

    pkg_after_stale = client.get(f"/job-cards/{rev1_id}").json()
    assert pkg_after_stale["drill_sheet"]["version"] == 2
    assert pkg_after_stale["drill_sheet"]["setup"]["drill_program_name"] == "TSCHECK_REV1.DRL"
    assert pkg_after_stale["process_sequence_sheet"]["version"] == 2
    assert pkg_after_stale["process_sequence_sheet"]["route_name"] == "Live 3-Stage PCB Traveler"

    # Invalid lifecycle transitions from 'draft' (version 1) -> 409
    assert client.post(
        f"/job-cards/{rev1_id}/return-to-draft",
        json={"expected_version": 1, "reason": "Cannot return from draft"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/approve",
        json={"expected_version": 1},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/release",
        json={"expected_version": 1},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/revisions",
        json={"expected_version": 1, "revision_reason": "Cannot revise from draft"},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409

    # 3. Submit for engineering review -> verify invalid transitions -> return to draft -> re-submit -> approve
    sub1 = client.post(f"/job-cards/{rev1_id}/submit-review", json={"expected_version": 1})
    assert sub1.status_code == 200, sub1.text
    assert sub1.json()["job_card"]["status"] == "engineering_review"
    assert sub1.json()["job_card"]["version"] == 2

    # Invalid edits and transitions from 'engineering_review' (version 2) -> 409
    assert client.patch(
        f"/job-cards/{rev1_id}",
        json={"expected_version": 2, "layers": 8},
    ).status_code == 409
    assert client.patch(
        f"/job-cards/{rev1_id}/drill-sheet",
        json={"expected_version": 2, "setup": {"stack_height": 2}},
    ).status_code == 409
    assert client.patch(
        f"/job-cards/{rev1_id}/process-sequence",
        json={"expected_version": 2, "route_name": "Blocked in review"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/submit-review",
        json={"expected_version": 2},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/release",
        json={"expected_version": 2},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/revisions",
        json={"expected_version": 2, "revision_reason": "Cannot revise from engineering_review"},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409

    ret1 = client.post(
        f"/job-cards/{rev1_id}/return-to-draft",
        json={"expected_version": 2, "reason": "Verify drill program name with CAM lead"},
    )
    assert ret1.status_code == 200, ret1.text
    assert ret1.json()["job_card"]["status"] == "draft"
    assert ret1.json()["job_card"]["version"] == 3

    sub2 = client.post(f"/job-cards/{rev1_id}/submit-review", json={"expected_version": 3})
    assert sub2.status_code == 200, sub2.text
    assert sub2.json()["job_card"]["version"] == 4

    app1 = client.post(
        f"/job-cards/{rev1_id}/approve",
        json={"expected_version": 4},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert app1.status_code == 200, app1.text
    assert app1.json()["job_card"]["status"] == "approved"
    assert app1.json()["job_card"]["version"] == 5
    assert app1.json()["drill_sheet"]["is_locked"] is True
    assert app1.json()["drill_sheet"]["version"] == 3

    # Invalid edits and transitions from 'approved' (master version 5, drill version 3, proc version 2) -> 409
    assert client.patch(
        f"/job-cards/{rev1_id}",
        json={"expected_version": 5, "layers": 8},
    ).status_code == 409
    assert client.patch(
        f"/job-cards/{rev1_id}/drill-sheet",
        json={"expected_version": 3, "setup": {"stack_height": 1}},
    ).status_code == 409
    assert client.patch(
        f"/job-cards/{rev1_id}/process-sequence",
        json={"expected_version": 2, "route_name": "Blocked in approved"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/submit-review",
        json={"expected_version": 5},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/return-to-draft",
        json={"expected_version": 5, "reason": "Forbidden reopen"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/approve",
        json={"expected_version": 5},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409

    # 4. Create ECN Revision 2 from Approved Revision 1 (rev 1 -> superseded at version 6, rev 2 -> draft)
    rev2_res = client.post(
        f"/job-cards/{rev1_id}/revisions",
        json={"expected_version": 5, "revision_reason": "ECN-LIVE-01: Update finish hole size"},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert rev2_res.status_code == 201, rev2_res.text
    rev2_id = rev2_res.json()["job_card"]["id"]
    assert rev2_res.json()["job_card"]["revision"] == 2
    assert rev2_res.json()["job_card"]["status"] == "draft"
    assert rev2_res.json()["job_card"]["previous_revision_id"] == rev1_id
    assert rev2_res.json()["drill_sheet"]["is_locked"] is False

    # Invalid edits and transitions from 'superseded' (rev1_id at version 6) -> 409
    assert client.patch(
        f"/job-cards/{rev1_id}",
        json={"expected_version": 6, "layers": 8},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/submit-review",
        json={"expected_version": 6},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/return-to-draft",
        json={"expected_version": 6, "reason": "Superseded cannot return to draft"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/approve",
        json={"expected_version": 6},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/release",
        json={"expected_version": 6},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev1_id}/revisions",
        json={"expected_version": 6, "revision_reason": "Superseded cannot fork another revision"},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409

    # 5. Submit & approve Revision 2, then create ECN Revision 3 (proving multiple superseded revisions coexist!)
    assert client.post(f"/job-cards/{rev2_id}/submit-review", json={"expected_version": 1}).status_code == 200
    assert client.post(
        f"/job-cards/{rev2_id}/approve",
        json={"expected_version": 2},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 200

    rev3_res = client.post(
        f"/job-cards/{rev2_id}/revisions",
        json={"expected_version": 3, "revision_reason": "ECN-LIVE-02: Second post-approval ECN"},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert rev3_res.status_code == 201, rev3_res.text
    rev3_id = rev3_res.json()["job_card"]["id"]
    assert rev3_res.json()["job_card"]["revision"] == 3
    assert rev3_res.json()["job_card"]["revision_code"] == "REV-03"
    assert rev3_res.json()["job_card"]["previous_revision_id"] == rev2_id

    # 6. Submit, approve, and release Revision 3 to production
    assert client.post(f"/job-cards/{rev3_id}/submit-review", json={"expected_version": 1}).status_code == 200
    assert client.post(
        f"/job-cards/{rev3_id}/approve",
        json={"expected_version": 2},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 200

    rel_res = client.post(
        f"/job-cards/{rev3_id}/release",
        json={"expected_version": 3},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert rel_res.status_code == 200, rel_res.text
    rel_pkg = rel_res.json()
    assert rel_pkg["job_card"]["status"] == "released_to_production"
    assert rel_pkg["process_sequence_sheet"]["current_process_seq"] == 2
    assert rel_pkg["process_sequence_sheet"]["processes"][0]["status"] == "skipped"
    assert rel_pkg["process_sequence_sheet"]["processes"][1]["status"] == "waiting"

    # Invalid edits and transitions from 'released_to_production' (rev3_id at version 4) -> 409
    assert client.patch(
        f"/job-cards/{rev3_id}",
        json={"expected_version": 4, "layers": 8},
    ).status_code == 409
    assert client.patch(
        f"/job-cards/{rev3_id}/drill-sheet",
        json={"expected_version": 2, "setup": {"stack_height": 1}},
    ).status_code == 409
    assert client.patch(
        f"/job-cards/{rev3_id}/process-sequence",
        json={"expected_version": 2, "route_name": "Blocked after release"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev3_id}/submit-review",
        json={"expected_version": 4},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev3_id}/return-to-draft",
        json={"expected_version": 4, "reason": "Cannot return released card"},
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev3_id}/approve",
        json={"expected_version": 4},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev3_id}/release",
        json={"expected_version": 4},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409
    assert client.post(
        f"/job-cards/{rev3_id}/revisions",
        json={"expected_version": 4, "revision_reason": "Cannot revise released card"},
        headers=idempotency_headers(new_idempotency_key()),
    ).status_code == 409

    # Inspect MongoDB: 3 revisions exist (rev 1 superseded, rev 2 superseded, rev 3 released_to_production)
    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["job_cards_count"] == 3
    assert state["drill_sheets_count"] == 3
    assert state["process_sheets_count"] == 3
    by_rev = {d["revision"]: d for d in state["job_cards"]}
    assert by_rev[1]["status"] == "superseded" and by_rev[1]["superseded_by_revision_id"] == rev2_id
    assert by_rev[2]["status"] == "superseded" and by_rev[2]["superseded_by_revision_id"] == rev3_id
    assert by_rev[3]["status"] == "released_to_production"


# ==============================================================================
# M. Day 1.9 — Concurrent ECN revisions & concurrent lifecycle transitions
# ==============================================================================

def test_m_concurrent_lifecycle_transitions_and_concurrent_ecn_revisions(
    client: httpx.Client,
) -> None:
    company_id = _primary_company_id(client)
    run_header, identity = _run_header(client)
    ord_info = _seed_confirmed_order(client, company_id)
    jc_no = f"JC-TSCHECK-CONCREV-{uuid.uuid4().hex[:8].upper()}"

    created = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert created.status_code == 201, created.text
    jc_id = created.json()["job_card"]["id"]

    # Advance to engineering_review (version 2)
    assert client.post(f"/job-cards/{jc_id}/submit-review", json={"expected_version": 1}).status_code == 200

    # 1. Concurrent POST /approve with distinct Idempotency-Keys and same expected_version=2
    base_url = str(client.base_url).rstrip("/")
    base_headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])
    workers = 6
    approve_keys = [new_idempotency_key() for _ in range(workers)]
    barrier_approve = threading.Barrier(workers)

    def approve_competing(idx: int) -> httpx.Response:
        h = {
            **base_headers,
            **idempotency_headers(approve_keys[idx]),
            **_request_id_header(identity["run_id"]),
        }
        with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
            barrier_approve.wait(timeout=15.0)
            return w.post(f"/job-cards/{jc_id}/approve", json={"expected_version": 2})

    with ThreadPoolExecutor(max_workers=workers) as pool:
        app_responses = list(pool.map(approve_competing, range(workers)))

    app_winners = [r for r in app_responses if r.status_code == 200]
    app_losers = [r for r in app_responses if r.status_code == 409]
    assert len(app_winners) == 1, [(r.status_code, r.text) for r in app_responses]
    assert len(app_losers) == workers - 1

    # 2. Concurrent POST /revisions on the approved Job Card (version 3) with distinct Idempotency-Keys
    rev_keys = [new_idempotency_key() for _ in range(workers)]
    barrier_rev = threading.Barrier(workers)

    def revise_competing(idx: int) -> httpx.Response:
        h = {
            **base_headers,
            **idempotency_headers(rev_keys[idx]),
            **_request_id_header(identity["run_id"]),
        }
        with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
            barrier_rev.wait(timeout=15.0)
            return w.post(
                f"/job-cards/{jc_id}/revisions",
                json={"expected_version": 3, "revision_reason": f"Competing ECN worker {idx}"},
            )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        rev_responses = list(pool.map(revise_competing, range(workers)))

    rev_winners = [r for r in rev_responses if r.status_code == 201]
    rev_losers = [r for r in rev_responses if r.status_code == 409]
    assert len(rev_winners) == 1, [(r.status_code, r.text) for r in rev_responses]
    assert len(rev_losers) == workers - 1

    state = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert state["job_cards_count"] == 2
    assert state["drill_sheets_count"] == 2
    assert state["process_sheets_count"] == 2


# ==============================================================================
# N. Day 1.9 — Concurrent Job Card creation vs Order legacy assign() mutual exclusion
# ==============================================================================

def test_n_concurrent_job_card_creation_vs_order_legacy_assign_mutual_exclusion(
    client: httpx.Client,
) -> None:
    company_id = _primary_company_id(client)
    run_header, identity = _run_header(client)
    base_url = str(client.base_url).rstrip("/")
    base_headers = auth_headers(os.environ["TEST_ACCESS_TOKEN"])

    # 1. Sequential check A (Job Card wins): Order with active Job Card rejects POST /orders/{order_id}/assign with 409
    ord_seq_a = _seed_confirmed_order(client, company_id)
    dept = ord_seq_a["departments"][0]
    seq_a_jc_no = f"JC-TSCHECK-SEQA-{uuid.uuid4().hex[:6].upper()}"
    jc_seq_a = client.post(
        "/job-cards",
        json=_package_payload(seq_a_jc_no, ord_seq_a),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert jc_seq_a.status_code == 201, jc_seq_a.text

    assign_blocked = client.post(
        f"/orders/{ord_seq_a['order_id']}/assign",
        json={"departments": [dept], "route_name": "Should Roll Back Route"},
    )
    assert assign_blocked.status_code == 409, assign_blocked.text
    assert "active job card" in assign_blocked.json()["detail"].lower()

    # Verify losing assignment's Order changes and timeline changes were completely rolled back
    ord_seq_a_after = client.get(f"/orders/{ord_seq_a['order_id']}")
    assert ord_seq_a_after.status_code == 200, ord_seq_a_after.text
    ord_seq_a_doc = ord_seq_a_after.json()
    assert ord_seq_a_doc["status"] == "confirmed"
    assert ord_seq_a_doc["stages"] == []
    assert ord_seq_a_doc["route_name"] is None
    assert len(ord_seq_a_doc["timeline"]) == 1
    assert all(t["stage"] != "Assigned to production" for t in ord_seq_a_doc["timeline"])

    state_seq_a = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{seq_a_jc_no}"),
        headers=run_header,
    ).json()
    assert state_seq_a["job_cards_count"] == 1
    assert state_seq_a["drill_sheets_count"] == 1
    assert state_seq_a["process_sheets_count"] == 1
    assert state_seq_a["job_cards"][0]["status"] == "draft"

    # 2. Sequential check B (Assignment wins): Order already assigned to in_production rejects POST /job-cards with 409
    ord_seq_b = _seed_confirmed_order(client, company_id)
    assign_ok = client.post(
        f"/orders/{ord_seq_b['order_id']}/assign",
        json={"departments": [dept], "route_name": "Committed Legacy Route"},
    )
    assert assign_ok.status_code == 200, assign_ok.text
    assert assign_ok.json()["status"] == "in_production"

    seq_b_jc_no = f"JC-TSCHECK-SEQB-{uuid.uuid4().hex[:6].upper()}"
    jc_blocked = client.post(
        "/job-cards",
        json=_package_payload(seq_b_jc_no, ord_seq_b),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert jc_blocked.status_code == 409, jc_blocked.text

    ord_seq_b_after = client.get(f"/orders/{ord_seq_b['order_id']}")
    assert ord_seq_b_after.status_code == 200, ord_seq_b_after.text
    ord_seq_b_doc = ord_seq_b_after.json()
    assert ord_seq_b_doc["status"] == "in_production"
    assert len(ord_seq_b_doc["stages"]) == 1
    assert ord_seq_b_doc["timeline"][-1]["stage"] == "Assigned to production"

    state_seq_b = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{seq_b_jc_no}"),
        headers=run_header,
    ).json()
    assert state_seq_b["job_cards_count"] == 0
    assert state_seq_b["drill_sheets_count"] == 0
    assert state_seq_b["process_sheets_count"] == 0
    assert state_seq_b["idempotency_count"] == 0
    assert state_seq_b["lifecycle_idempotency_count"] == 0
    assert state_seq_b["audit_actions"] == []
    assert state_seq_b["update_audit_count"] == 0

    # 3. True concurrent race: POST /api/job-cards vs POST /api/orders/{order_id}/assign
    rounds = 8
    observed_outcomes: set[tuple[int, int]] = set()
    for round_idx in range(rounds):
        race_ord = _seed_confirmed_order(client, company_id)
        race_oid = race_ord["order_id"]
        race_jc_no = f"JC-TSCHECK-ASSIGNRACE-{round_idx}-{uuid.uuid4().hex[:6].upper()}"
        race_barrier = threading.Barrier(2)

        def run_create_jc() -> httpx.Response:
            h = {
                **base_headers,
                **idempotency_headers(new_idempotency_key()),
                **_request_id_header(identity["run_id"]),
            }
            with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
                race_barrier.wait(timeout=15.0)
                if round_idx % 2 == 1:
                    # Compensate for assign()'s 3 pre-transaction reads so both transactions
                    # collide inside MongoDB and both winner outcomes are exercised live.
                    time.sleep(0.0025 * ((round_idx // 2) + 1))
                return w.post("/job-cards", json=_package_payload(race_jc_no, race_ord))

        def run_assign_order() -> httpx.Response:
            h = {
                **base_headers,
                **_request_id_header(identity["run_id"]),
            }
            with httpx.Client(base_url=base_url, headers=h, timeout=30.0) as w:
                race_barrier.wait(timeout=15.0)
                return w.post(
                    f"/orders/{race_oid}/assign",
                    json={"departments": [dept], "route_name": f"Concurrent Legacy Route {round_idx}"},
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            if round_idx % 2 == 0:
                fut_jc = pool.submit(run_create_jc)
                fut_assign = pool.submit(run_assign_order)
            else:
                fut_assign = pool.submit(run_assign_order)
                fut_jc = pool.submit(run_create_jc)
            res_jc = fut_jc.result()
            res_assign = fut_assign.result()

        # Exactly one must win and the other must receive 409 Conflict; never both succeed
        outcome = (res_jc.status_code, res_assign.status_code)
        assert outcome in {(201, 409), (409, 200)}, (
            f"Round {round_idx} mutual-exclusion violation: jc={res_jc.status_code} ({res_jc.text}), "
            f"assign={res_assign.status_code} ({res_assign.text})"
        )
        observed_outcomes.add(outcome)

        order_res = client.get(f"/orders/{race_oid}")
        assert order_res.status_code == 200, order_res.text
        order_after = order_res.json()

        state = client.get(
            _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{race_jc_no}"),
            headers=run_header,
        ).json()
        if outcome == (201, 409):
            # When Job Card creation wins:
            # - Order remains 'confirmed' and losing assignment's stage/route/timeline changes rolled back
            # - Exactly one active Job Card and its two child sheets exist
            assert order_after["status"] == "confirmed"
            assert order_after["stages"] == []
            assert order_after["route_name"] is None
            assert len(order_after["timeline"]) == 1
            assert all(t["stage"] != "Assigned to production" for t in order_after["timeline"])
            assert state["job_cards_count"] == 1
            assert state["drill_sheets_count"] == 1
            assert state["process_sheets_count"] == 1
            assert state["job_cards"][0]["status"] == "draft"
            assert state["idempotency_count"] == 1
            assert "job_card.created" in state["audit_actions"]
        else:
            # When assignment wins:
            # - Order is 'in_production' with committed stages and timeline
            # - No Job Card, drill sheet, process sheet, idempotency record, or orphaned Job Card audit record remains
            assert order_after["status"] == "in_production"
            assert len(order_after["stages"]) == 1
            assert order_after["stages"][0]["department"] == dept
            assert order_after["route_name"] == f"Concurrent Legacy Route {round_idx}"
            assert order_after["timeline"][-1]["stage"] == "Assigned to production"
            assert state["job_cards_count"] == 0
            assert state["drill_sheets_count"] == 0
            assert state["process_sheets_count"] == 0
            assert state["idempotency_count"] == 0
            assert state["lifecycle_idempotency_count"] == 0
            assert state["audit_actions"] == []
            assert state["update_audit_count"] == 0

    assert (201, 409) in observed_outcomes and (409, 200) in observed_outcomes, (
        f"Expected both race winners across {rounds} rounds, observed={observed_outcomes}"
    )


# ==============================================================================
# O. Day 1.9.2 — Fail-closed legacy assign() guard for malformed & superseded statuses
# ==============================================================================

def test_o_fail_closed_legacy_assign_guard_for_malformed_and_superseded_job_cards(
    client: httpx.Client,
) -> None:
    """Verify _check_no_active_job_card ignores a Job Card ONLY when status is the scalar string 'superseded'.

    Proves against live MongoDB replica set that:
    1. All four valid active Job Card statuses ('draft', 'engineering_review', 'approved', 'released_to_production')
       block legacy assignment (409) and roll back Order status, stages, route, and timeline.
    2. A malformed Job Card with a missing status field blocks legacy assignment (409) and rolls back Order changes.
    3. A Job Card with null (None) status blocks legacy assignment (409) and rolls back Order changes.
    4. A Job Card with an unexpected string status or non-string scalar status blocks legacy assignment (409) and rolls back Order changes.
    5. A Job Card with an array-valued status containing 'superseded' (['superseded'] and ['draft', 'superseded'])
       blocks legacy assignment (409) and rolls back Order changes.
    6. A Job Card with an array-valued status containing an active status (['draft']) blocks legacy assignment (409) and rolls back Order changes.
    7. A properly superseded historical Job Card (scalar string 'superseded') does NOT block legacy assignment
       when no active Job Card exists (200).
    """
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)

    def _assert_order_unchanged_confirmed(order_id: str) -> None:
        r = client.get(f"/orders/{order_id}")
        assert r.status_code == 200, r.text
        doc = r.json()
        assert doc["status"] == "confirmed"
        assert doc["stages"] == []
        assert doc["route_name"] is None
        assert len(doc["timeline"]) == 1
        assert all(t["stage"] != "Assigned to production" for t in doc["timeline"])

    # 1. All four valid active statuses ('draft', 'engineering_review', 'approved', 'released_to_production') block assignment
    ord_active = _seed_confirmed_order(client, company_id)
    dept = ord_active["departments"][0]
    active_jc_no = f"JC-TSCHECK-FCL-ACT-{uuid.uuid4().hex[:6].upper()}"
    res_active_jc = client.post(
        "/job-cards",
        json=_package_payload(active_jc_no, ord_active),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert res_active_jc.status_code == 201, res_active_jc.text
    active_jc_id = res_active_jc.json()["job_card"]["id"]
    assert res_active_jc.json()["job_card"]["status"] == "draft"

    # 1a. Active status: 'draft'
    res_assign_draft = client.post(
        f"/orders/{ord_active['order_id']}/assign",
        json={"departments": [dept], "route_name": "Blocked By Draft JC"},
    )
    assert res_assign_draft.status_code == 409, res_assign_draft.text
    _assert_order_unchanged_confirmed(ord_active["order_id"])

    # 1b. Active status: 'engineering_review'
    sub_res = client.post(f"/job-cards/{active_jc_id}/submit-review", json={"expected_version": 1})
    assert sub_res.status_code == 200, sub_res.text
    assert sub_res.json()["job_card"]["status"] == "engineering_review"
    res_assign_review = client.post(
        f"/orders/{ord_active['order_id']}/assign",
        json={"departments": [dept], "route_name": "Blocked By Engineering Review JC"},
    )
    assert res_assign_review.status_code == 409, res_assign_review.text
    _assert_order_unchanged_confirmed(ord_active["order_id"])

    # 1c. Active status: 'approved'
    app_res = client.post(
        f"/job-cards/{active_jc_id}/approve",
        json={"expected_version": 2},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert app_res.status_code == 200, app_res.text
    assert app_res.json()["job_card"]["status"] == "approved"
    res_assign_approved = client.post(
        f"/orders/{ord_active['order_id']}/assign",
        json={"departments": [dept], "route_name": "Blocked By Approved JC"},
    )
    assert res_assign_approved.status_code == 409, res_assign_approved.text
    _assert_order_unchanged_confirmed(ord_active["order_id"])

    # 1d. Active status: 'released_to_production'
    rel_res = client.post(
        f"/job-cards/{active_jc_id}/release",
        json={"expected_version": 3},
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert rel_res.status_code == 200, rel_res.text
    assert rel_res.json()["job_card"]["status"] == "released_to_production"
    res_assign_released = client.post(
        f"/orders/{ord_active['order_id']}/assign",
        json={"departments": [dept], "route_name": "Blocked By Released JC"},
    )
    assert res_assign_released.status_code == 409, res_assign_released.text
    _assert_order_unchanged_confirmed(ord_active["order_id"])

    # 2..6. Malformed Job Cards (missing, null, unexpected string, non-string scalar, array with 'superseded', array with active status)
    malformed_cases: list[tuple[str, Any, str]] = [
        ("missing", None, f"JC-TSCHECK-FCL-MISS-{uuid.uuid4().hex[:6].upper()}"),
        ("null", None, f"JC-TSCHECK-FCL-NULL-{uuid.uuid4().hex[:6].upper()}"),
        ("value", "unexpected_corrupt_status", f"JC-TSCHECK-FCL-UNEXP-{uuid.uuid4().hex[:6].upper()}"),
        ("value", 123, f"JC-TSCHECK-FCL-INT-{uuid.uuid4().hex[:6].upper()}"),
        ("value", ["superseded"], f"JC-TSCHECK-FCL-ARRSUP1-{uuid.uuid4().hex[:6].upper()}"),
        ("value", ["draft", "superseded"], f"JC-TSCHECK-FCL-ARRSUP2-{uuid.uuid4().hex[:6].upper()}"),
        ("value", ["draft"], f"JC-TSCHECK-FCL-ARRACT-{uuid.uuid4().hex[:6].upper()}"),
    ]
    created_raw_jc_nos: list[str] = []
    try:
        for status_mode, status_value, raw_jc_no in malformed_cases:
            ord_mal = _seed_confirmed_order(client, company_id)
            created_raw_jc_nos.append(raw_jc_no)
            seed_raw = client.post(
                _test_url(client, "/__test__/job-cards/raw-status-fixture"),
                headers=run_header,
                json={
                    "action": "upsert",
                    "company_id": company_id,
                    "order_id": ord_mal["order_id"],
                    "order_no": ord_mal["order_no"],
                    "customer_id": ord_mal["customer_id"],
                    "job_card_no": raw_jc_no,
                    "status_mode": status_mode,
                    "status_value": status_value,
                },
            )
            assert seed_raw.status_code == 200, seed_raw.text

            res_assign_mal = client.post(
                f"/orders/{ord_mal['order_id']}/assign",
                json={"departments": [dept], "route_name": f"Blocked By {status_mode} {status_value}"},
            )
            assert res_assign_mal.status_code == 409, (
                f"Expected 409 for malformed Job Card status_mode={status_mode} value={status_value!r}, "
                f"got {res_assign_mal.status_code}: {res_assign_mal.text}"
            )
            assert "active job card" in res_assign_mal.json()["detail"].lower()
            _assert_order_unchanged_confirmed(ord_mal["order_id"])
    finally:
        # Remove raw malformed documents so collection remains clean for any subsequent index preflight
        for raw_jc_no in created_raw_jc_nos:
            client.post(
                _test_url(client, "/__test__/job-cards/raw-status-fixture"),
                headers=run_header,
                json={"action": "delete", "company_id": company_id, "job_card_no": raw_jc_no},
            )

    # 7. Scalar string 'superseded' historical Job Card does NOT block assignment when no active Job Card exists
    ord_sup = _seed_confirmed_order(client, company_id)
    sup_jc_no = f"JC-TSCHECK-FCL-SUP-{uuid.uuid4().hex[:6].upper()}"
    seed_sup = client.post(
        _test_url(client, "/__test__/job-cards/raw-status-fixture"),
        headers=run_header,
        json={
            "action": "upsert",
            "company_id": company_id,
            "order_id": ord_sup["order_id"],
            "order_no": ord_sup["order_no"],
            "customer_id": ord_sup["customer_id"],
            "job_card_no": sup_jc_no,
            "status_mode": "value",
            "status_value": "superseded",
        },
    )
    assert seed_sup.status_code == 200, seed_sup.text

    res_assign_sup = client.post(
        f"/orders/{ord_sup['order_id']}/assign",
        json={"departments": [dept], "route_name": "Allowed After Superseded Only"},
    )
    assert res_assign_sup.status_code == 200, res_assign_sup.text
    ord_sup_after = client.get(f"/orders/{ord_sup['order_id']}").json()
    assert ord_sup_after["status"] == "in_production"
    assert ord_sup_after["route_name"] == "Allowed After Superseded Only"
    assert len(ord_sup_after["stages"]) == 1
    assert ord_sup_after["stages"][0]["department"] == dept
    assert ord_sup_after["stages"][0]["received"] == ord_sup_after["quantity"]
    assert ord_sup_after["timeline"][-1]["stage"] == "Assigned to production"


# ==============================================================================
# Z. Scoped cleanup of synthetic Job Card and Order test data
# ==============================================================================

def test_z_scoped_cleanup_removes_synthetic_job_card_test_data(client: httpx.Client) -> None:
    company_id = _primary_company_id(client)
    run_header, _ = _run_header(client)
    jc_no = f"JC-TSCHECK-CLEANUP-{uuid.uuid4().hex[:8].upper()}"
    ord_info = _seed_confirmed_order(client, company_id)

    # Ensure a run-scoped secondary identity exists even if test_z is run standalone (-k test_z)
    sec = client.post(_test_url(client, "/__test__/job-cards/secondary-tenant-session"), headers=run_header)
    assert sec.status_code == 200, sec.text

    created = client.post(
        "/job-cards",
        json=_package_payload(jc_no, ord_info),
        headers=idempotency_headers(new_idempotency_key()),
    )
    assert created.status_code == 201, created.text
    jc_id = created.json()["job_card"]["id"]

    # Trigger a rejected stale PATCH so RequestAuditMiddleware writes a 'job-cards' failure event
    stale = client.patch(
        f"/job-cards/{jc_id}",
        json={"expected_version": 99, "layers": 10},
    )
    assert stale.status_code == 409

    cleaned = client.post(_test_url(client, "/__test__/job-cards/cleanup"), headers=run_header)
    assert cleaned.status_code == 200, cleaned.text
    summary = cleaned.json()
    assert summary["deleted_job_cards"] >= 1
    assert summary["deleted_drill_sheets"] >= 1
    assert summary["deleted_process_sheets"] >= 1
    assert summary["deleted_test_orders"] >= 1
    assert summary["deleted_idempotency_records"] >= 1
    assert summary["deleted_audit_logs"] >= 2
    assert summary["deleted_secondary_users"] >= 1
    assert summary["deleted_secondary_members"] >= 1
    assert summary["deleted_secondary_sessions"] >= 1

    after = client.get(
        _test_url(client, f"/__test__/job-cards/inspect/{company_id}/{jc_no}"),
        headers=run_header,
    ).json()
    assert after["job_cards_count"] == 0
    assert after["drill_sheets_count"] == 0
    assert after["process_sheets_count"] == 0
    assert after["idempotency_count"] == 0
    assert after["update_audit_count"] == 0

    # Verify cleanup is idempotent and safe to rerun
    rerun = client.post(_test_url(client, "/__test__/job-cards/cleanup"), headers=run_header)
    assert rerun.status_code == 200, rerun.text
    rerun_summary = rerun.json()
    assert rerun_summary["deleted_job_cards"] == 0
    assert rerun_summary["deleted_drill_sheets"] == 0
    assert rerun_summary["deleted_process_sheets"] == 0
    assert rerun_summary["deleted_test_orders"] == 0
    assert rerun_summary["deleted_idempotency_records"] == 0
    assert rerun_summary["deleted_audit_logs"] == 0
    assert rerun_summary["deleted_secondary_users"] == 0
    assert rerun_summary["deleted_secondary_members"] == 0
    assert rerun_summary["deleted_secondary_sessions"] == 0
