"""Guarded, test-only FactoryOS backend entry point.

This module validates its explicit test configuration before importing application
modules. It is launched by run_isolated.py with an already-bound loopback socket.
"""

from __future__ import annotations

import datetime
import errno
import os
import re
import secrets
import socket
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


SECURITY_DB_NAME = "factoryos_security_test"
TEST_SERVER_MARKER = "factoryos-isolated-test-server-v1"
_PLACEHOLDERS = {
    "dev-change-me", "change-me", "example-secret", "your-jwt-secret",
    "your-secret-key", "test-secret", "test-secret-change-me", "changeme", "password",
}
_DEMO_ENV_NAMES = (
    "ENABLE_DEMO_SEED", "DEMO_MONGO_URL", "DEMO_DB_NAME", "DEMO_COMPANY_ID",
    "DEMO_DEPLOYMENT_ID", "DEMO_ALLOWED_HOSTS", "DEMO_SOCKET_TIMEOUT_MS",
)


def _validate_and_prepare_environment() -> tuple[str, str, str]:
    """Validate test-only inputs, then set application variables without fallbacks."""
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name in _DEMO_ENV_NAMES:
        os.environ.pop(name, None)

    secret = os.environ.get("TEST_JWT_SECRET", "").strip()
    mongo_url = os.environ.get("TEST_MONGO_URL", "").strip()
    db_name = os.environ.get("DB_NAME", "")
    run_id = os.environ.get("FACTORYOS_TEST_RUN_ID", "").strip()
    socket_fd = os.environ.get("FACTORYOS_TEST_SOCKET_FD", "").strip()

    if not secret or secret.lower() in _PLACEHOLDERS or len(secret.encode("utf-8")) < 32:
        raise RuntimeError("TEST_JWT_SECRET is missing or invalid")
    if not mongo_url.startswith(("mongodb://", "mongodb+srv://")):
        raise RuntimeError("TEST_MONGO_URL is missing or invalid")
    if db_name != SECURITY_DB_NAME:
        raise RuntimeError(f"DB_NAME must be exactly {SECURITY_DB_NAME}")
    if not run_id or len(run_id) > 128:
        raise RuntimeError("FACTORYOS_TEST_RUN_ID is missing or invalid")
    try:
        fd = int(socket_fd)
        if fd < 0:
            raise ValueError
    except ValueError:
        raise RuntimeError("FACTORYOS_TEST_SOCKET_FD is missing or invalid") from None

    # The runner validates that original generic credentials were not reused,
    # then deliberately maps these TEST_* values into the app's variable names.
    # The child verifies that the mappings match; neither check proves MongoDB
    # roles or the physical deployment are isolated.
    if os.environ.get("JWT_SECRET", "").strip() != secret:
        raise RuntimeError("JWT_SECRET must be mapped from TEST_JWT_SECRET by the runner")
    if os.environ.get("MONGO_URL", "").strip() != mongo_url:
        raise RuntimeError("MONGO_URL must be mapped from TEST_MONGO_URL by the runner")

    os.environ["JWT_SECRET"] = secret
    os.environ["MONGO_URL"] = mongo_url
    return run_id, db_name, str(fd)


# Do not import dotenv or application modules above this point. lib.db calls
# load_dotenv itself; PYTHON_DOTENV_DISABLED is supported by the installed
# python-dotenv package and is set before that import.
TEST_RUN_ID, TEST_DB_NAME, _SOCKET_FD = _validate_and_prepare_environment()
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import APIRouter, FastAPI, Header, HTTPException  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
import uvicorn  # noqa: E402

from pymongo.errors import DuplicateKeyError  # noqa: E402

from lib.authz import sign_access_token  # noqa: E402
from lib.db import (  # noqa: E402
    JOB_CARD_INDEXES,
    LEGACY_INDEXES,
    apply_job_card_indexes_after_preflight,
    client,
    db,
    job_card_index_duplicate_preflight,
)
from lib.middleware import RequestAuditMiddleware  # noqa: E402
from routers import assistant, attendance, auth, company, dashboard, inventory, job_cards, machines, orders, production, reports, schedule, session  # noqa: E402


# Saved collection validator state for process_sequence_sheets before test injection.
_SAVED_PROCESS_SHEET_VALIDATOR: dict[str, Any] | None = None
_SECONDARY_TENANT_PREFIX = f"tscheck-jc-sec-{TEST_RUN_ID[:12]}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        # Read-only startup check. It creates no collections, indexes, or records.
        await db.command("ping")
    except BaseException:
        client.close()
        raise
    try:
        yield
    finally:
        client.close()


app = FastAPI(title="FactoryOS Isolated Test API", lifespan=lifespan)


# NOTE ON LOCAL-LOOPBACK TRUST BOUNDARY:
# This server binds exclusively to an inherited 127.0.0.1 listener socket created by
# run_isolated.py. GET /__test__/identity intentionally exposes TEST_RUN_ID on loopback so
# run_isolated.py and conftest.py can verify they are talking to the exact child process
# they spawned. Consequently, X-Test-Run-ID guards against accidental cross-run or
# cross-target misuse, NOT against untrusted local processes sharing the host loopback.
@app.get("/__test__/identity", include_in_schema=False)
async def test_identity() -> dict[str, Any]:
    return {
        "marker": TEST_SERVER_MARKER,
        "run_id": TEST_RUN_ID,
        "database_name": TEST_DB_NAME,
        "pid": os.getpid(),
    }


def _require_test_run(run_id: str) -> None:
    if not secrets.compare_digest(run_id, TEST_RUN_ID):
        raise HTTPException(status_code=403, detail="invalid isolated test run")


def _require_security_db() -> None:
    if db.name != SECURITY_DB_NAME or os.environ.get("DB_NAME") != SECURITY_DB_NAME:
        raise HTTPException(status_code=403, detail="refusing test helper outside factoryos_security_test")


async def _restore_process_sheet_validator() -> None:
    global _SAVED_PROCESS_SHEET_VALIDATOR
    existing_cols = await db.list_collection_names()
    if "process_sequence_sheets" not in existing_cols:
        _SAVED_PROCESS_SHEET_VALIDATOR = None
        return
    if _SAVED_PROCESS_SHEET_VALIDATOR is not None:
        await db.command(
            "collMod",
            "process_sequence_sheets",
            validator=_SAVED_PROCESS_SHEET_VALIDATOR["validator"],
            validationLevel=_SAVED_PROCESS_SHEET_VALIDATOR["validationLevel"],
        )
        _SAVED_PROCESS_SHEET_VALIDATOR = None


@app.post("/__test__/audit-sentinel", include_in_schema=False)
async def insert_audit_sentinel(body: dict[str, str], x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    _require_test_run(x_test_run_id)
    event_id = body.get("event_id", "")
    if len(event_id) != 64 or any(char not in "0123456789abcdef" for char in event_id):
        raise HTTPException(status_code=422, detail="event_id must be a lowercase SHA-256 digest")
    await db.audit_logs.insert_one({"_id": event_id, "id": event_id, "tenant_id": "test-only",
                                    "action": "test.audit_sentinel", "resource_type": "test"})
    return {"created": True}


@app.delete("/__test__/audit-sentinel/{event_id}", include_in_schema=False)
async def delete_audit_sentinel(event_id: str, x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    _require_test_run(x_test_run_id)
    result = await db.audit_logs.delete_one({"_id": event_id, "action": "test.audit_sentinel"})
    return {"deleted": result.deleted_count == 1}


@app.get("/__test__/audit-event/{event_id}", include_in_schema=False)
async def get_audit_event(event_id: str, x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    _require_test_run(x_test_run_id)
    event = await db.audit_logs.find_one({"_id": event_id}, {"_id": 0})
    return {"event": event}


@app.post("/__test__/job-cards/setup-indexes", include_in_schema=False)
async def setup_job_card_indexes(x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    _require_test_run(x_test_run_id)
    _require_security_db()
    preflight = await job_card_index_duplicate_preflight(db)
    created_first = await apply_job_card_indexes_after_preflight(db)
    created_second = await apply_job_card_indexes_after_preflight(db)
    await db.idempotency_records.create_indexes(LEGACY_INDEXES["idempotency_records"])
    index_info: dict[str, dict[str, Any]] = {}
    for col_name in (*JOB_CARD_INDEXES.keys(), "idempotency_records"):
        raw_info = await db[col_name].index_information()
        index_info[col_name] = {
            idx_name: {
                "key": [list(pair) for pair in spec.get("key", [])],
                "unique": bool(spec.get("unique", False)),
                "partialFilterExpression": spec.get("partialFilterExpression"),
            }
            for idx_name, spec in raw_info.items()
        }
    return {
        "preflight": preflight,
        "created_first": created_first,
        "created_second": created_second,
        "indexes": index_info,
    }


@app.post("/__test__/job-cards/seed-order", include_in_schema=False)
async def seed_job_card_test_order(body: dict[str, Any], x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    """Seed or update a synthetic Order in factoryos_security_test for Job Card integration tests."""
    _require_test_run(x_test_run_id)
    _require_security_db()
    company_id = str(body["company_id"]).strip()
    order_id = str(body.get("order_id") or f"ord-tscheck-{uuid.uuid4().hex[:8]}").strip()
    order_no = str(body.get("order_no") or f"SO-TSCHECK-{order_id[-6:].upper()}").strip()
    customer_id = str(body.get("customer_id") or "cust-tscheck-001").strip()
    customer_name = str(body.get("customer_name") or "Apex Precision Test Customer").strip()
    status = str(body.get("status") or "confirmed").strip()
    quantity = int(body.get("quantity") or 400)
    unit_price = float(body.get("unit_price") or 125.0)
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    today_str = now_str[:10]

    company_doc = await db.companies.find_one({"id": company_id}, {"_id": 0, "departments": 1}) or {}
    departments = company_doc.get("departments") or ["Production"]

    order_doc = {
        "id": order_id,
        "order_no": order_no,
        "company_id": company_id,
        "source": "offline",
        "status": status,
        "order_date": today_str,
        "expected_delivery": today_str,
        "customer_id": customer_id,
        "customer": {
            "name": customer_name,
            "contact_person": "Test Contact",
            "phone": "9000000000",
            "email": "customer@tscheck.invalid",
            "city": "Bengaluru",
            "gst_no": "29AABCT1332L1ZT",
        },
        "product": "4L FR4 Industrial Controller PCB",
        "quantity": quantity,
        "unit_price": unit_price,
        "payment_terms": "credit",
        "priority": "high",
        "notes": "Synthetic test order for Job Card integration verification",
        "submitted_by": None,
        "submitted_at": now_str,
        "decision": "approved" if status == "confirmed" else None,
        "decided_by": None,
        "decided_at": now_str if status == "confirmed" else None,
        "decision_note": "",
        "holds": [],
        "payments": [],
        "amount_paid": round(quantity * unit_price, 2),
        "stages": [],
        "route_name": None,
        "production_target_date": None,
        "qc_status": None,
        "timeline": [
            {
                "stage": "Order confirmed",
                "date": today_str,
                "note": "Seeded confirmed order for Job Card test",
                "at": now_str,
                "user": "Test Approver",
                "role": "owner",
            }
        ],
        "_tscheck_job_card_order": True,
    }
    await db.orders.replace_one({"company_id": company_id, "id": order_id}, order_doc, upsert=True)
    return {
        "company_id": company_id,
        "order_id": order_id,
        "order_no": order_no,
        "customer_id": customer_id,
        "customer_name": customer_name,
        "status": status,
        "departments": departments,
    }


@app.post("/__test__/provision-actors", include_in_schema=False)
async def provision_test_actors(body: dict[str, Any], x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    """Provision the six synthetic actors for isolated multi-role tests in factoryos_security_test."""
    _require_test_run(x_test_run_id)
    _require_security_db()

    company_id = str(body.get("company_id") or "").strip()
    if not company_id:
        raise HTTPException(status_code=422, detail="company_id is required")
    actors = body.get("actors")
    if not isinstance(actors, dict) or not actors:
        raise HTTPException(status_code=422, detail="non-empty actors dictionary required")

    from lib.authz import hash_password

    required_departments = ["Machining", "Finishing", "Packaging"]
    company = await db.companies.find_one({"id": company_id})
    if not company:
        await db.companies.insert_one({
            "id": company_id,
            "name": "FactoryOS Security Test Co",
            "departments": ["CAM", "Drilling", "Plating", "QC", *required_departments],
            "production_departments": required_departments,
            "status": "active",
        })
    else:
        existing = company.get("departments") or []
        updated = list(existing)
        for dept in required_departments:
            if dept not in updated:
                updated.append(dept)
        prod_existing = company.get("production_departments") or []
        prod_updated = list(prod_existing)
        for dept in required_departments:
            if dept not in prod_updated:
                prod_updated.append(dept)
        await db.companies.update_one(
            {"id": company_id},
            {"$set": {"departments": updated, "production_departments": prod_updated}},
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat(timespec="seconds")

    actor_specs = {
        "u-sales": {"name": "Emily Ross", "role": "sales_staff", "department": None},
        "u-ops": {"name": "Sarah Mitchell", "role": "manager", "department": None},
        "u-pm": {"name": "Daniel Brooks", "role": "production_manager", "department": None},
        "u-sup-machining": {"name": "Machining Supervisor", "role": "supervisor", "department": "Machining"},
        "u-sup-finishing": {"name": "Finishing Supervisor", "role": "supervisor", "department": "Finishing"},
        "u-sup-packaging": {"name": "Packaging Supervisor", "role": "supervisor", "department": "Packaging"},
    }

    validated_actors: list[tuple[str, str, str, dict[str, Any]]] = []
    for actor_id, cred in actors.items():
        if actor_id not in actor_specs:
            raise HTTPException(status_code=422, detail=f"unsupported synthetic test actor: {actor_id}")
        if not isinstance(cred, dict):
            raise HTTPException(status_code=422, detail=f"invalid credential entry for actor {actor_id}")
        raw_email = cred.get("email")
        raw_password = cred.get("password")
        if not isinstance(raw_email, str) or not raw_email.strip() or not isinstance(raw_password, str) or not raw_password:
            raise HTTPException(status_code=422, detail=f"non-empty email and password are required for actor {actor_id}")
        validated_actors.append((actor_id, raw_email.strip(), raw_password, actor_specs[actor_id]))

    provisioned = []
    for actor_id, email, raw_password, spec in validated_actors:
        pw_hash = hash_password(raw_password)
        email_normalized = email.casefold()

        user_doc = {
            "id": actor_id,
            "company_id": company_id,
            "name": spec["name"],
            "email": email,
            "email_normalized": email_normalized,
            "password_hash": pw_hash,
            "role": spec["role"],
            "department": spec["department"],
            "status": "active",
            "auth_version": 1,
            "created_at": now_iso,
        }
        await db.users.replace_one({"id": actor_id}, user_doc, upsert=True)

        member_id = f"mem-{actor_id}-{company_id}"
        depts = [spec["department"]] if spec["department"] else []
        member_doc = {
            "id": member_id,
            "company_id": company_id,
            "user_id": actor_id,
            "role": spec["role"],
            "status": "active",
            "department": spec["department"],
            "department_ids": depts,
            "created_at": now_iso,
        }
        await db.company_members.replace_one({"id": member_id}, member_doc, upsert=True)
        provisioned.append(actor_id)

    return {"provisioned": provisioned, "company_id": company_id}


@app.post("/__test__/job-cards/secondary-tenant-session", include_in_schema=False)
async def provision_secondary_tenant_session(x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    """Provision a run-scoped secondary company identity in factoryos_security_test.

    Uses production_manager (minimum role carrying job_cards.read + job_cards.manage)
    and mints a database-backed session without hardcoded passwords or public login
    rate-limit consumption.
    """
    _require_test_run(x_test_run_id)
    _require_security_db()
    suffix = uuid.uuid4().hex[:8]
    company_id = f"{_SECONDARY_TENANT_PREFIX}-co-{suffix}"
    user_id = f"{_SECONDARY_TENANT_PREFIX}-usr-{suffix}"
    membership_id = f"{_SECONDARY_TENANT_PREFIX}-mem-{suffix}"
    session_id = f"{_SECONDARY_TENANT_PREFIX}-sess-{secrets.token_hex(16)}"

    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    exp_iso = (now + datetime.timedelta(hours=1)).isoformat(timespec="seconds")

    await db.users.insert_one({
        "id": user_id,
        "name": "Run-Scoped Secondary Production Manager",
        "email": f"{user_id}@tscheck.invalid",
        "email_normalized": f"{user_id}@tscheck.invalid",
        "password_hash": "!run-scoped-non-password-session!",
        "status": "active",
        "auth_version": 1,
        "created_at": now_iso,
    })
    await db.company_members.insert_one({
        "id": membership_id,
        "company_id": company_id,
        "user_id": user_id,
        "role": "production_manager",
        "status": "active",
        "department": "Production",
        "department_ids": ["Production"],
        "created_at": now_iso,
    })
    await db.sessions.insert_one({
        "id": f"{_SECONDARY_TENANT_PREFIX}-sdoc-{suffix}",
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
    })
    token = sign_access_token(
        user_id=user_id,
        company_id=company_id,
        session_id=session_id,
        role="production_manager",
        auth_version=1,
    )
    return {
        "company_id": company_id,
        "user_id": user_id,
        "role": "production_manager",
        "access_token": token,
    }


@app.post("/__test__/job-cards/verify-unique-indexes", include_in_schema=False)
async def verify_job_card_unique_indexes(x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    _require_test_run(x_test_run_id)
    _require_security_db()

    call_tag = uuid.uuid4().hex[:10]
    probe_company = f"tscheck-idx-probe-{call_tag}"
    other_company = f"tscheck-idx-other-{call_tag}"
    enforced: list[str] = []
    cross_tenant_allowed: list[str] = []
    superseded_coexistence_allowed = False
    try:
        # 1, 2 & 3: job_cards (company_id, job_card_no, revision), (company_id, id),
        # and partial unique (company_id, order_id) across active statuses
        await db.job_cards.insert_one({
            "company_id": probe_company,
            "id": f"probe-jc-id-1-{call_tag}",
            "job_card_id": f"probe-jc-id-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-1",
            "order_id": f"probe-ord-1-{call_tag}",
            "status": "draft",
            "revision": 1,
        })
        try:
            await db.job_cards.insert_one({
                "company_id": probe_company,
                "id": f"probe-jc-id-2-{call_tag}",
                "job_card_id": f"probe-jc-id-2-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-1",
                "order_id": f"probe-ord-2-{call_tag}",
                "status": "draft",
                "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("job_cards.company_job_card_no_revision_unique")
        try:
            await db.job_cards.insert_one({
                "company_id": probe_company,
                "id": f"probe-jc-id-1-{call_tag}",
                "job_card_id": f"probe-jc-id-1-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-2",
                "order_id": f"probe-ord-3-{call_tag}",
                "status": "draft",
                "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("job_cards.company_job_card_id_unique")
        try:
            await db.job_cards.insert_one({
                "company_id": probe_company,
                "id": f"probe-jc-id-3-{call_tag}",
                "job_card_id": f"probe-jc-id-3-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-3",
                "order_id": f"probe-ord-1-{call_tag}",
                "status": "approved",
                "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("job_cards.company_active_order_job_card_unique")

        # Multiple superseded documents for the same (company_id, order_id) can coexist with 1 active document
        await db.job_cards.insert_one({
            "company_id": probe_company,
            "id": f"probe-jc-sup-1-{call_tag}",
            "job_card_id": f"probe-jc-sup-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-SUP",
            "order_id": f"probe-ord-1-{call_tag}",
            "status": "superseded",
            "revision": 1,
        })
        await db.job_cards.insert_one({
            "company_id": probe_company,
            "id": f"probe-jc-sup-2-{call_tag}",
            "job_card_id": f"probe-jc-sup-2-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-SUP",
            "order_id": f"probe-ord-1-{call_tag}",
            "status": "superseded",
            "revision": 2,
        })
        superseded_coexistence_allowed = True

        # Another company can insert identical (job_card_no, revision), id, and active order_id in job_cards
        await db.job_cards.insert_one({
            "company_id": other_company,
            "id": f"probe-jc-id-1-{call_tag}",
            "job_card_id": f"probe-jc-id-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-1",
            "order_id": f"probe-ord-1-{call_tag}",
            "status": "draft",
            "revision": 1,
        })
        cross_tenant_allowed.append("job_cards")

        # 3 & 4: drill_detail_sheets (company_id, job_card_id) and (company_id, id)
        await db.drill_detail_sheets.insert_one({
            "company_id": probe_company, "id": f"probe-drill-id-1-{call_tag}", "job_card_id": f"probe-jc-id-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
        })
        try:
            await db.drill_detail_sheets.insert_one({
                "company_id": probe_company, "id": f"probe-drill-id-2-{call_tag}", "job_card_id": f"probe-jc-id-1-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("drill_detail_sheets.company_drill_job_card_unique")
        try:
            await db.drill_detail_sheets.insert_one({
                "company_id": probe_company, "id": f"probe-drill-id-1-{call_tag}", "job_card_id": f"probe-jc-id-2-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("drill_detail_sheets.company_drill_sheet_id_unique")

        await db.drill_detail_sheets.insert_one({
            "company_id": other_company, "id": f"probe-drill-id-1-{call_tag}", "job_card_id": f"probe-jc-id-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
        })
        cross_tenant_allowed.append("drill_detail_sheets")

        # 5 & 6: process_sequence_sheets (company_id, job_card_id) and (company_id, id)
        await db.process_sequence_sheets.insert_one({
            "company_id": probe_company, "id": f"probe-proc-id-1-{call_tag}", "job_card_id": f"probe-jc-id-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
        })
        try:
            await db.process_sequence_sheets.insert_one({
                "company_id": probe_company, "id": f"probe-proc-id-2-{call_tag}", "job_card_id": f"probe-jc-id-1-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("process_sequence_sheets.company_process_job_card_unique")
        try:
            await db.process_sequence_sheets.insert_one({
                "company_id": probe_company, "id": f"probe-proc-id-1-{call_tag}", "job_card_id": f"probe-jc-id-2-{call_tag}",
                "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
            })
        except DuplicateKeyError:
            enforced.append("process_sequence_sheets.company_process_sheet_id_unique")

        await db.process_sequence_sheets.insert_one({
            "company_id": other_company, "id": f"probe-proc-id-1-{call_tag}", "job_card_id": f"probe-jc-id-1-{call_tag}",
            "job_card_no": "JC-TSCHECK-IDX-1", "revision": 1,
        })
        cross_tenant_allowed.append("process_sequence_sheets")

        # Verify fail-closed DB name check on non-test database handles for BOTH helpers
        wrong_db_preflight_rejected = True
        wrong_db_apply_rejected = True
        for forbidden_db_name in ("factoryos", "factoryos_demo"):
            try:
                await job_card_index_duplicate_preflight(client[forbidden_db_name])
                wrong_db_preflight_rejected = False
            except RuntimeError:
                pass
            try:
                await apply_job_card_indexes_after_preflight(client[forbidden_db_name])
                wrong_db_apply_rejected = False
            except RuntimeError:
                pass

        # Verify fail-closed DB_NAME environment check for BOTH helpers
        wrong_env_preflight_rejected = False
        wrong_env_apply_rejected = False
        original_env_db = os.environ.get("DB_NAME", SECURITY_DB_NAME)
        try:
            os.environ["DB_NAME"] = "factoryos"
            try:
                await job_card_index_duplicate_preflight(db)
            except RuntimeError:
                wrong_env_preflight_rejected = True
            try:
                await apply_job_card_indexes_after_preflight(db)
            except RuntimeError:
                wrong_env_apply_rejected = True
        finally:
            os.environ["DB_NAME"] = original_env_db
    finally:
        for col_name in JOB_CARD_INDEXES:
            await db[col_name].delete_many({"company_id": {"$in": [probe_company, other_company]}})

    return {
        "enforced": enforced,
        "cross_tenant_allowed": cross_tenant_allowed,
        "superseded_coexistence_allowed": superseded_coexistence_allowed,
        "wrong_db_rejected": wrong_db_preflight_rejected and wrong_db_apply_rejected,
        "wrong_db_preflight_rejected": wrong_db_preflight_rejected,
        "wrong_db_apply_rejected": wrong_db_apply_rejected,
        "wrong_env_preflight_rejected": wrong_env_preflight_rejected,
        "wrong_env_apply_rejected": wrong_env_apply_rejected,
    }


@app.post("/__test__/job-cards/verify-preflight-anomalies", include_in_schema=False)
async def verify_job_card_preflight_anomalies(x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    """Exercise negative preflight and index-application cases in factoryos_security_test.

    Temporarily drops the six unique Job Card indexes so duplicate and malformed
    synthetic records can be inserted, verifies that job_card_index_duplicate_preflight
    detects every anomaly category and apply_job_card_indexes_after_preflight aborts
    before creating indexes or modifying documents, and restores all six unique indexes
    in finally.
    """
    _require_test_run(x_test_run_id)
    _require_security_db()

    tag = uuid.uuid4().hex[:8]
    marker_field = "_tscheck_preflight_probe"
    results: dict[str, bool] = {}

    async def _drop_custom_indexes() -> None:
        for col_name, models in JOB_CARD_INDEXES.items():
            info = await db[col_name].index_information()
            for model in models:
                idx_name = model.document["name"]
                if idx_name in info:
                    await db[col_name].drop_index(idx_name)

    async def _clear_probes() -> None:
        for col_name in JOB_CARD_INDEXES:
            await db[col_name].delete_many({marker_field: tag})

    async def _run_case(col_name: str, docs: list[dict[str, Any]], check_fn) -> bool:
        await _clear_probes()
        tagged_docs = [{**d, marker_field: tag} for d in docs]
        await db[col_name].insert_many(tagged_docs)
        before_count = await db[col_name].count_documents({marker_field: tag})
        preflight = await job_card_index_duplicate_preflight(db)
        apply_aborted = False
        try:
            await apply_job_card_indexes_after_preflight(db)
        except RuntimeError:
            apply_aborted = True
        after_count = await db[col_name].count_documents({marker_field: tag})
        col_indexes = await db[col_name].index_information()
        no_index_created = all(
            m.document["name"] not in col_indexes for m in JOB_CARD_INDEXES[col_name]
        )
        await _clear_probes()
        return (
            before_count == len(docs)
            and after_count == len(docs)
            and apply_aborted
            and no_index_created
            and bool(check_fn(preflight))
        )

    try:
        await _drop_custom_indexes()

        # 1. Missing versus explicit null collision on (company_id, id)
        results["missing_vs_null_collision"] = await _run_case(
            "job_cards",
            [
                {"company_id": f"co-null-{tag}", "job_card_no": f"JC-N1-{tag}", "revision": 1},
                {"company_id": f"co-null-{tag}", "id": None, "job_card_no": f"JC-N2-{tag}", "revision": 1},
            ],
            lambda pf: any(
                e["index"] == "company_job_card_id_unique"
                and e.get("duplicate_samples")
                and e["duplicate_samples"][0]["count"] == 2
                and len(e.get("anomalies", [])) == 2
                for e in pf.get("job_cards", [])
            ),
        )

        # 2. Duplicate business keys across all 3 collections
        results["duplicate_business_keys"] = (
            await _run_case(
                "job_cards",
                [
                    {"company_id": f"co-dup-{tag}", "id": f"id-1-{tag}", "job_card_no": f"JC-DUP-{tag}", "revision": 1},
                    {"company_id": f"co-dup-{tag}", "id": f"id-2-{tag}", "job_card_no": f"JC-DUP-{tag}", "revision": 1},
                ],
                lambda pf: any(
                    e["index"] == "company_job_card_no_revision_unique" and e.get("duplicate_samples")
                    for e in pf.get("job_cards", [])
                ),
            )
            and await _run_case(
                "drill_detail_sheets",
                [
                    {"company_id": f"co-dup-{tag}", "id": f"d1-{tag}", "job_card_id": f"jc-dup-{tag}"},
                    {"company_id": f"co-dup-{tag}", "id": f"d2-{tag}", "job_card_id": f"jc-dup-{tag}"},
                ],
                lambda pf: any(
                    e["index"] == "company_drill_job_card_unique" and e.get("duplicate_samples")
                    for e in pf.get("drill_detail_sheets", [])
                ),
            )
            and await _run_case(
                "process_sequence_sheets",
                [
                    {"company_id": f"co-dup-{tag}", "id": f"p-dup-{tag}", "job_card_id": f"jc-1-{tag}"},
                    {"company_id": f"co-dup-{tag}", "id": f"p-dup-{tag}", "job_card_id": f"jc-2-{tag}"},
                ],
                lambda pf: any(
                    e["index"] == "company_process_sheet_id_unique" and e.get("duplicate_samples")
                    for e in pf.get("process_sequence_sheets", [])
                ),
            )
        )

        # 3. Missing company_id on a single document
        results["missing_company_id"] = await _run_case(
            "job_cards",
            [{"id": f"id-mco-{tag}", "job_card_no": f"JC-MCO-{tag}", "revision": 1}],
            lambda pf: any(
                any(a["field"] == "company_id" and a["category"] == "missing_field" for a in row["anomalies"])
                for e in pf.get("job_cards", [])
                for row in e.get("anomalies", [])
            ),
        )

        # 4. Empty and whitespace-only indexed strings
        results["empty_and_whitespace_strings"] = await _run_case(
            "drill_detail_sheets",
            [
                {"company_id": f"co-str-{tag}", "id": "", "job_card_id": f"jc-empty-{tag}"},
                {"company_id": f"co-str-{tag}", "id": f"d-ws-{tag}", "job_card_id": "   \t\n  "},
            ],
            lambda pf: {
                a["category"]
                for e in pf.get("drill_detail_sheets", [])
                for row in e.get("anomalies", [])
                for a in row["anomalies"]
            }
            >= {"empty_string", "whitespace_only_string"},
        )

        # 5. Wrong BSON types
        results["wrong_bson_types"] = await _run_case(
            "process_sequence_sheets",
            [{"company_id": f"co-type-{tag}", "id": 12345, "job_card_id": {"bad": "type"}}],
            lambda pf: any(
                a["category"] == "wrong_bson_type"
                for e in pf.get("process_sequence_sheets", [])
                for row in e.get("anomalies", [])
                for a in row["anomalies"]
            ),
        )

        # 6. Array-valued indexed fields
        results["array_valued_keys"] = await _run_case(
            "job_cards",
            [{"company_id": f"co-arr-{tag}", "id": f"id-arr-{tag}", "job_card_no": ["JC-A1", "JC-A2"], "revision": 1}],
            lambda pf: any(
                a["field"] == "job_card_no" and a["category"] == "array_valued_key"
                for e in pf.get("job_cards", [])
                for row in e.get("anomalies", [])
                for a in row["anomalies"]
            ),
        )

        # 7. Invalid revision values (missing, null, string, float, array, 0, -1)
        results["invalid_revision_values"] = await _run_case(
            "job_cards",
            [
                {"company_id": f"co-rev-{tag}", "id": f"r1-{tag}", "job_card_no": f"JCR1-{tag}"},
                {"company_id": f"co-rev-{tag}", "id": f"r2-{tag}", "job_card_no": f"JCR2-{tag}", "revision": None},
                {"company_id": f"co-rev-{tag}", "id": f"r3-{tag}", "job_card_no": f"JCR3-{tag}", "revision": "1"},
                {"company_id": f"co-rev-{tag}", "id": f"r4-{tag}", "job_card_no": f"JCR4-{tag}", "revision": 1.5},
                {"company_id": f"co-rev-{tag}", "id": f"r5-{tag}", "job_card_no": f"JCR5-{tag}", "revision": [1]},
                {"company_id": f"co-rev-{tag}", "id": f"r6-{tag}", "job_card_no": f"JCR6-{tag}", "revision": 0},
                {"company_id": f"co-rev-{tag}", "id": f"r7-{tag}", "job_card_no": f"JCR7-{tag}", "revision": -3},
            ],
            lambda pf: {
                a["category"]
                for e in pf.get("job_cards", [])
                if e["index"] == "company_job_card_no_revision_unique"
                for row in e.get("anomalies", [])
                for a in row["anomalies"]
                if a["field"] == "revision"
            }
            == {
                "invalid_revision:missing_field",
                "invalid_revision:null_value",
                "invalid_revision:wrong_bson_type",
                "invalid_revision:array_valued_key",
                "invalid_revision:below_minimum",
            },
        )

        # 8. Duplicate active Job Cards for the same (company_id, order_id)
        results["duplicate_active_order_job_cards"] = await _run_case(
            "job_cards",
            [
                {
                    "company_id": f"co-act-{tag}",
                    "id": f"act-1-{tag}",
                    "job_card_no": f"JC-ACT1-{tag}",
                    "order_id": f"ord-act-{tag}",
                    "status": "draft",
                    "revision": 1,
                },
                {
                    "company_id": f"co-act-{tag}",
                    "id": f"act-2-{tag}",
                    "job_card_no": f"JC-ACT2-{tag}",
                    "order_id": f"ord-act-{tag}",
                    "status": "released_to_production",
                    "revision": 1,
                },
            ],
            lambda pf: any(
                e["index"] == "company_active_order_job_card_unique"
                and e.get("duplicate_samples")
                and e["duplicate_samples"][0]["count"] == 2
                and not e.get("anomalies")
                for e in pf.get("job_cards", [])
            ),
        )

        # 9. Multiple superseded Job Cards + one active Job Card for the same Order pass preflight
        await _clear_probes()
        await db.job_cards.insert_many([
            {
                "company_id": f"co-sup-ok-{tag}",
                "id": f"sup-1-{tag}",
                "job_card_no": f"JC-SUPOK-{tag}",
                "order_id": f"ord-sup-ok-{tag}",
                "status": "superseded",
                "revision": 1,
                marker_field: tag,
            },
            {
                "company_id": f"co-sup-ok-{tag}",
                "id": f"sup-2-{tag}",
                "job_card_no": f"JC-SUPOK-{tag}",
                "order_id": f"ord-sup-ok-{tag}",
                "status": "superseded",
                "revision": 2,
                marker_field: tag,
            },
            {
                "company_id": f"co-sup-ok-{tag}",
                "id": f"sup-3-{tag}",
                "job_card_no": f"JC-SUPOK-{tag}",
                "order_id": f"ord-sup-ok-{tag}",
                "status": "draft",
                "revision": 3,
                marker_field: tag,
            },
        ])
        sup_preflight = await job_card_index_duplicate_preflight(db)
        results["superseded_order_job_cards_allowed"] = sup_preflight == {}
        await _clear_probes()

        # 10. Invalid status and order_id anomalies on job_cards
        results["invalid_status_and_order_id_anomalies"] = await _run_case(
            "job_cards",
            [
                {
                    "company_id": f"co-st-{tag}",
                    "id": f"st1-{tag}",
                    "job_card_no": f"JCST1-{tag}",
                    "order_id": f"ord-st1-{tag}",
                    "revision": 1,
                },
                {
                    "company_id": f"co-st-{tag}",
                    "id": f"st2-{tag}",
                    "job_card_no": f"JCST2-{tag}",
                    "order_id": f"ord-st2-{tag}",
                    "status": None,
                    "revision": 1,
                },
                {
                    "company_id": f"co-st-{tag}",
                    "id": f"st3-{tag}",
                    "job_card_no": f"JCST3-{tag}",
                    "order_id": f"ord-st3-{tag}",
                    "status": ["draft"],
                    "revision": 1,
                },
                {
                    "company_id": f"co-st-{tag}",
                    "id": f"st4-{tag}",
                    "job_card_no": f"JCST4-{tag}",
                    "order_id": f"ord-st4-{tag}",
                    "status": 999,
                    "revision": 1,
                },
                {
                    "company_id": f"co-st-{tag}",
                    "id": f"st5-{tag}",
                    "job_card_no": f"JCST5-{tag}",
                    "order_id": f"ord-st5-{tag}",
                    "status": "archived_unknown",
                    "revision": 1,
                },
                {
                    "company_id": f"co-st-{tag}",
                    "id": f"st6-{tag}",
                    "job_card_no": f"JCST6-{tag}",
                    "order_id": "   ",
                    "status": "draft",
                    "revision": 1,
                },
            ],
            lambda pf: {
                a["category"]
                for e in pf.get("job_cards", [])
                if e["index"] == "company_active_order_job_card_unique"
                for row in e.get("anomalies", [])
                for a in row["anomalies"]
            }
            >= {
                "invalid_status:missing_field",
                "invalid_status:null_value",
                "invalid_status:array_valued_key",
                "invalid_status:wrong_bson_type",
                "invalid_status:unexpected_value",
                "whitespace_only_string",
            },
        )
    finally:
        await _clear_probes()
        clean_preflight = await job_card_index_duplicate_preflight(db)
        recreated = await apply_job_card_indexes_after_preflight(db)
        results["clean_collections_restored"] = clean_preflight == {} and set(recreated.keys()) == set(
            JOB_CARD_INDEXES.keys()
        )

    return results


@app.post("/__test__/job-cards/rollback-validator", include_in_schema=False)
async def set_job_card_rollback_validator(body: dict[str, Any], x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    global _SAVED_PROCESS_SHEET_VALIDATOR
    _require_test_run(x_test_run_id)
    _require_security_db()
    enabled = bool(body.get("enabled", False))
    forbidden_route = str(body.get("forbidden_route_name", "__test_abort_after_master_insert__"))
    existing_cols = await db.list_collection_names()
    if "process_sequence_sheets" not in existing_cols:
        await db.create_collection("process_sequence_sheets")
    if enabled:
        if _SAVED_PROCESS_SHEET_VALIDATOR is None:
            col_info = await db.command("listCollections", filter={"name": "process_sequence_sheets"})
            first_batch = col_info.get("cursor", {}).get("firstBatch", [])
            options = first_batch[0].get("options", {}) if first_batch else {}
            _SAVED_PROCESS_SHEET_VALIDATOR = {
                "validator": options.get("validator", {}),
                "validationLevel": options.get("validationLevel", "off"),
            }
        await db.command(
            "collMod",
            "process_sequence_sheets",
            validator={"route_name": {"$ne": forbidden_route}},
            validationLevel="strict",
        )
    else:
        await _restore_process_sheet_validator()
    return {"enabled": enabled, "forbidden_route_name": forbidden_route}


@app.post("/__test__/job-cards/raw-status-fixture", include_in_schema=False)
async def raw_job_card_status_fixture(body: dict[str, Any], x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    """Insert or delete a scoped raw job_cards document in factoryos_security_test to test fail-closed status guards."""
    _require_test_run(x_test_run_id)
    _require_security_db()
    company_id = str(body["company_id"]).strip()
    job_card_no = str(body["job_card_no"]).strip()
    if not job_card_no.startswith("JC-TSCHECK-"):
        raise HTTPException(status_code=422, detail="job_card_no must start with JC-TSCHECK-")

    action = str(body.get("action") or "upsert").strip()
    if action == "delete":
        deleted = (await db.job_cards.delete_many({"company_id": company_id, "job_card_no": job_card_no})).deleted_count
        return {"deleted": deleted}

    order_id = str(body["order_id"]).strip()
    order_no = str(body.get("order_no") or "SO-TSCHECK-RAW").strip()
    revision = int(body.get("revision") or 1)
    jc_id = str(body.get("id") or f"jc-raw-tscheck-{uuid.uuid4().hex[:10]}").strip()
    status_mode = str(body.get("status_mode") or "value").strip()
    now_str = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    raw_doc: dict[str, Any] = {
        "id": jc_id,
        "job_card_id": jc_id,
        "job_card_no": job_card_no,
        "company_id": company_id,
        "order_id": order_id,
        "order_no": order_no,
        "part_number": "PN-TSCHECK-RAW",
        "revision": revision,
        "revision_code": f"REV-{revision:02d}",
        "customer_id": str(body.get("customer_id") or "cust-tscheck-001"),
        "customer_name": "Apex Precision Test Customer",
        "version": 1,
        "created_at": now_str,
        "updated_at": now_str,
    }
    if status_mode == "missing":
        pass
    elif status_mode == "null":
        raw_doc["status"] = None
    elif status_mode == "value":
        raw_doc["status"] = body.get("status_value")
        if raw_doc["status"] == "superseded":
            raw_doc["superseded_by_revision_id"] = str(body.get("superseded_by_revision_id") or f"jc-sup-{uuid.uuid4().hex[:8]}")
            raw_doc["superseded_at"] = now_str
    else:
        raise HTTPException(status_code=422, detail=f"unsupported status_mode: {status_mode}")

    await db.job_cards.replace_one({"company_id": company_id, "job_card_no": job_card_no, "revision": revision}, raw_doc, upsert=True)
    return {"id": jc_id, "job_card_no": job_card_no, "order_id": order_id, "status_mode": status_mode}


@app.get("/__test__/job-cards/inspect/{company_id}/{job_card_no}", include_in_schema=False)
async def inspect_job_card_state(
    company_id: str,
    job_card_no: str,
    x_test_run_id: str = Header(..., alias="X-Test-Run-ID"),
):
    _require_test_run(x_test_run_id)
    _require_security_db()
    query = {"company_id": company_id, "job_card_no": job_card_no}
    jcs = await db.job_cards.find(query, {"_id": 0}).to_list(None)
    drills = await db.drill_detail_sheets.find(query, {"_id": 0}).to_list(None)
    procs = await db.process_sequence_sheets.find(query, {"_id": 0}).to_list(None)
    escaped_jc_no = re.escape(job_card_no)
    idem_count = await db.idempotency_records.count_documents({
        "company_id": company_id,
        "operation": {"$regex": f"^job_card\\.create:{escaped_jc_no}:"},
    })
    jc_ids = [d["id"] for d in jcs if "id" in d]
    update_audit_count = 0
    lifecycle_idem_count = 0
    audit_or: list[dict[str, Any]] = [
        {"after_state.job_card_no": job_card_no},
        {"resource_id": {"$regex": f"^job_card\\.create:{escaped_jc_no}:"}},
    ]
    if jc_ids:
        id_regex = "|".join(re.escape(jid) for jid in jc_ids)
        audit_or.append({"resource_id": {"$in": jc_ids}})
        audit_or.append({"resource_id": {"$regex": f"^job_card\\.(approve|release|revision):({id_regex}):"}})
        update_audit_count = await db.audit_logs.count_documents({
            "tenant_id": company_id,
            "action": "job_card.updated",
            "resource_type": "job_card",
            "resource_id": {"$in": jc_ids},
        })
        lifecycle_idem_count = await db.idempotency_records.count_documents({
            "company_id": company_id,
            "operation": {"$regex": f"^job_card\\.(approve|release|revision):({id_regex}):"},
        })
    audit_docs = await db.audit_logs.find(
        {
            "tenant_id": company_id,
            "resource_type": "job_card",
            "$or": audit_or,
        },
        {"action": 1, "_id": 0},
    ).to_list(None)
    audit_actions = [a["action"] for a in audit_docs if "action" in a]
    return {
        "job_cards_count": len(jcs),
        "drill_sheets_count": len(drills),
        "process_sheets_count": len(procs),
        "idempotency_count": idem_count,
        "lifecycle_idempotency_count": lifecycle_idem_count,
        "update_audit_count": update_audit_count,
        "audit_actions": audit_actions,
        "job_cards": jcs,
        "drill_sheets": drills,
        "process_sheets": procs,
    }


@app.post("/__test__/job-cards/cleanup", include_in_schema=False)
async def cleanup_job_card_test_data(x_test_run_id: str = Header(..., alias="X-Test-Run-ID")):
    _require_test_run(x_test_run_id)
    _require_security_db()
    await _restore_process_sheet_validator()

    prefix_filter = {"job_card_no": {"$regex": "^JC-TSCHECK-"}}
    jc_ids = [
        doc["id"]
        async for doc in db.job_cards.find(prefix_filter, {"id": 1, "_id": 0})
        if "id" in doc
    ]
    order_filter = {
        "$or": [
            {"_tscheck_job_card_order": True},
            {"id": {"$regex": "^ord-tscheck-"}},
            {"order_no": {"$regex": "^SO-TSCHECK-"}},
        ]
    }
    order_ids = [
        doc["id"]
        async for doc in db.orders.find(order_filter, {"id": 1, "_id": 0})
        if "id" in doc
    ]

    del_jc = (await db.job_cards.delete_many(prefix_filter)).deleted_count
    del_drill = (await db.drill_detail_sheets.delete_many(prefix_filter)).deleted_count
    del_proc = (await db.process_sequence_sheets.delete_many(prefix_filter)).deleted_count
    del_orders = (await db.orders.delete_many(order_filter)).deleted_count

    idem_or: list[dict[str, Any]] = [
        {"operation": {"$regex": "^job_card\\.create:JC-TSCHECK-"}},
    ]
    if jc_ids:
        id_regex = "|".join(re.escape(jid) for jid in jc_ids)
        idem_or.append({"operation": {"$regex": f"^job_card\\.(approve|release|revision):({id_regex}):"}})
    del_idem = (await db.idempotency_records.delete_many({"$or": idem_or})).deleted_count

    transactional_audit_or: list[dict[str, Any]] = [
        {"after_state.job_card_no": {"$regex": "^JC-TSCHECK-"}},
        {"resource_id": {"$regex": "^job_card\\.create:JC-TSCHECK-"}},
    ]
    if jc_ids:
        id_regex = "|".join(re.escape(jid) for jid in jc_ids)
        transactional_audit_or.append({"resource_id": {"$in": jc_ids}})
        transactional_audit_or.append({"resource_id": {"$regex": f"^job_card\\.(approve|release|revision):({id_regex}):"}})

    middleware_audit_or: list[dict[str, Any]] = [
        {"request_id": {"$regex": f"^tscheck-jc-{re.escape(TEST_RUN_ID[:12])}"}},
        {"tenant_id": {"$regex": f"^{re.escape(_SECONDARY_TENANT_PREFIX)}-co-"}},
    ]
    if jc_ids:
        id_regex = "|".join(re.escape(jid) for jid in jc_ids)
        middleware_audit_or.append({
            "action": {"$regex": f"^api\\.(patch|post|get):/api/job-cards/({id_regex})"}
        })

    audit_cleanup_or: list[dict[str, Any]] = [
        {"resource_type": "job_card", "$or": transactional_audit_or},
        {"resource_type": "job-cards", "$or": middleware_audit_or},
    ]
    if order_ids:
        ord_regex = "|".join(re.escape(oid) for oid in order_ids)
        audit_cleanup_or.append({"resource_type": "order", "resource_id": {"$in": order_ids}})
        audit_cleanup_or.append({
            "resource_type": "orders",
            "$or": [
                {"request_id": {"$regex": f"^tscheck-jc-{re.escape(TEST_RUN_ID[:12])}"}},
                {"action": {"$regex": f"^api\\.(post|put|patch|get):/api/orders/({ord_regex})"}},
            ],
        })

    del_audit = (await db.audit_logs.delete_many({"$or": audit_cleanup_or})).deleted_count

    # Clean up run-scoped secondary tenant users, memberships, and sessions
    sec_regex = {"$regex": f"^{re.escape(_SECONDARY_TENANT_PREFIX)}-"}
    del_sec_sessions = (await db.sessions.delete_many({"id": sec_regex})).deleted_count
    del_sec_members = (await db.company_members.delete_many({"id": sec_regex})).deleted_count
    del_sec_users = (await db.users.delete_many({"id": sec_regex})).deleted_count

    return {
        "deleted_job_cards": del_jc,
        "deleted_drill_sheets": del_drill,
        "deleted_process_sheets": del_proc,
        "deleted_test_orders": del_orders,
        "deleted_idempotency_records": del_idem,
        "deleted_audit_logs": del_audit,
        "deleted_secondary_sessions": del_sec_sessions,
        "deleted_secondary_members": del_sec_members,
        "deleted_secondary_users": del_sec_users,
    }


api_router = APIRouter(prefix="/api")


@api_router.get("/")
async def root():
    return {"message": "FactoryOS API", "demo_mode": False}


# The production company router contains an owner-authenticated demo reset route
# that opens a separate demo MongoDB client. It is deliberately omitted from the
# test app, while preserving the remaining company API routes and their original
# dependencies and response models.
_reset_routes = [route for route in company.router.routes if getattr(route, "path", None) == "/demo/reset"]
if len(_reset_routes) != 1:
    raise RuntimeError("Expected exactly one company demo-reset route; refusing unsafe test-router setup")
_safe_company_router = APIRouter()
_safe_company_router.routes.extend(route for route in company.router.routes if route not in _reset_routes)

api_router.include_router(_safe_company_router)
for router_module in (auth, session, orders, job_cards, attendance, production, machines, inventory, schedule, assistant, dashboard, reports):
    api_router.include_router(router_module.router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestAuditMiddleware)
app.include_router(api_router)


def main() -> None:
    fd = int(_SOCKET_FD)
    try:
        listener = socket.socket(fileno=fd)
    except OSError:
        raise RuntimeError("inherited test listener descriptor is invalid") from None
    try:
        if listener.family != socket.AF_INET:
            raise RuntimeError("test listener must be an IPv4 socket")
        if listener.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise RuntimeError("test listener must be a TCP stream socket")
        address = listener.getsockname()
        if not isinstance(address, tuple) or len(address) < 2 or address[0] != "127.0.0.1" or address[1] <= 0:
            raise RuntimeError("test listener must be bound to 127.0.0.1 on an assigned port")
        try:
            if listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1:
                raise RuntimeError("test listener must already be listening")
        except OSError as exc:
            if exc.errno != errno.ENOPROTOOPT or sys.platform != "darwin":
                raise RuntimeError("test listener must already be listening") from exc
    except BaseException:
        try:
            listener.close()
        except OSError:
            pass
        raise

    config = uvicorn.Config(app, host="127.0.0.1", workers=1, reload=False, log_level="warning")
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        try:
            listener.close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
