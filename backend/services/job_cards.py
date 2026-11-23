"""Job Card persistence service layer.

Provides atomic tri-document package creation, identity-verified retrieval,
and draft-only updates for the FactoryOS Job Card subsystem.

Tri-document structure:
1. Master Job Card (db.job_cards)
2. Drill Details Sheet (db.drill_detail_sheets)
3. Process Sequence Sheet (db.process_sequence_sheets)

Shared identity keys enforced across all three documents:
- job_card_id, job_card_no, order_id, order_no, part_number, revision, company_id
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel
from pymongo.client_session import ClientSession

from models.job_cards import (
    DrillDetailsSheet,
    JobCard,
    ProcessExecutionEntry,
    ProcessSequenceSheet,
    build_default_process_route,
    new_id,
)

ACTIVE_JOB_CARD_STATUSES: tuple[str, ...] = (
    "draft",
    "engineering_review",
    "approved",
    "released_to_production",
)

# Shared identity fields that must match across all three package documents.
IDENTITY_FIELDS: tuple[str, ...] = (
    "job_card_id",
    "job_card_no",
    "order_id",
    "order_no",
    "part_number",
    "revision",
    "company_id",
)

# Immutable identity and sub-document linking fields that cannot be modified via patch.
PROTECTED_IDENTITY_FIELDS: frozenset[str] = frozenset(
    (
        *IDENTITY_FIELDS,
        "customer_id",
        "id",
        "_id",
        "drill_sheet_id",
        "process_sequence_sheet_id",
    )
)

PROTECTED_SHEET_IDENTITY_FIELDS: frozenset[str] = frozenset(
    (
        *IDENTITY_FIELDS,
        "id",
        "_id",
    )
)

SHEET_SERVER_MANAGED_FIELDS: frozenset[str] = frozenset(
    (
        "is_locked",
        "created_at",
        "updated_at",
    )
)


# ---------- Custom Service Exceptions ----------

class JobCardServiceError(HTTPException):
    """Base exception for Job Card service operations."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)


class JobCardNotFoundError(JobCardServiceError):
    """Raised when a Job Card or linked sheet cannot be found within tenant scope."""

    def __init__(self, detail: str = "job card package not found") -> None:
        super().__init__(status_code=404, detail=detail)


class DuplicateJobCardPackageError(JobCardServiceError):
    """Raised when a Job Card package with the same identity already exists."""

    def __init__(self, detail: str = "job card package already exists") -> None:
        super().__init__(status_code=409, detail=detail)


class JobCardLockError(JobCardServiceError):
    """Raised when attempting to modify a locked (non-draft) Job Card."""

    def __init__(self, detail: str = "cannot update locked job card") -> None:
        super().__init__(status_code=409, detail=detail)


class JobCardIdentityMismatchError(JobCardServiceError):
    """Raised when identity fields do not match across the tri-document package."""

    def __init__(self, detail: str = "job card package identity mismatch") -> None:
        super().__init__(status_code=422, detail=detail)


class JobCardValidationError(JobCardServiceError):
    """Raised when Job Card data fails domain or structural validation."""

    def __init__(self, detail: str = "invalid job card data") -> None:
        super().__init__(status_code=422, detail=detail)


# ---------- Package Container ----------

class JobCardPackage(BaseModel):
    """Container for the complete tri-document Job Card package."""

    job_card: JobCard
    drill_sheet: DrillDetailsSheet
    process_sequence_sheet: ProcessSequenceSheet

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


# ---------- Internal Helpers ----------

def now_iso() -> str:
    """Canonical UTC ISO-8601 timestamp formatted to seconds."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_db(db_override: Any = None) -> Any:
    """Resolve database instance, supporting test overrides and lazy imports."""
    if db_override is not None:
        return db_override
    from lib.db import db
    return db


def _default_request() -> Request:
    """Create a minimal request object when called outside HTTP scope."""
    return Request({"type": "http", "headers": [], "client": None})


def _coerce_package(raw_result: Any) -> JobCardPackage:
    """Coerce a raw idempotency replay dictionary or JobCardPackage into JobCardPackage."""
    if isinstance(raw_result, JobCardPackage):
        return raw_result
    return JobCardPackage(
        job_card=JobCard(**raw_result["job_card"]),
        drill_sheet=DrillDetailsSheet(**raw_result["drill_sheet"]),
        process_sequence_sheet=ProcessSequenceSheet(**raw_result["process_sequence_sheet"]),
    )


def _version_condition(stored_version: Any, current_version: int) -> dict[str, Any]:
    """Build an optimistic concurrency filter matching stored version or legacy missing version."""
    if stored_version is None:
        return {"$or": [{"version": 1}, {"version": {"$exists": False}}]}
    return {"version": current_version}


def _resolve_expected_version(
    expected_version: int | None,
    patch: dict[str, Any] | None = None,
    *,
    required: bool = False,
) -> int | None:
    """Resolve and validate expected_version from explicit parameter and optional patch dict."""
    target = expected_version
    if target is not None:
        try:
            target = int(target)
        except (ValueError, TypeError) as exc:
            raise JobCardValidationError(f"Invalid expected_version: {expected_version}") from exc
        if target < 1:
            raise JobCardValidationError(f"expected_version must be >= 1 (got {target})")

    if patch is not None and "version" in patch and patch["version"] is not None:
        try:
            patch_ver = int(patch["version"])
        except (ValueError, TypeError) as exc:
            raise JobCardValidationError(f"Invalid version specification: {patch['version']}") from exc
        if patch_ver < 1:
            raise JobCardValidationError(f"version must be >= 1 (got {patch_ver})")
        if target is not None and target != patch_ver:
            raise JobCardLockError(
                f"Contradictory version expectations: parameter={target} vs patch={patch_ver}"
            )
        target = patch_ver

    if required and target is None:
        raise JobCardValidationError("expected_version is required and must be >= 1")
    return target


async def _validate_and_fence_parent_order(
    database: Any,
    clean_company: str,
    order_id: str,
    expected_order_no: str,
    expected_customer_id: str,
    session: ClientSession | None,
) -> dict[str, Any]:
    """Atomically validate and fence the parent Order inside the active MongoDB transaction.

    Uses find_one_and_update with $inc on _job_card_lock_version so concurrent Order status
    transitions (such as legacy stage assignment) conflict at the WiredTiger storage layer
    and cannot commit concurrently with Job Card creation or lifecycle mutations.
    """
    order_doc = await database.orders.find_one_and_update(
        {"company_id": clean_company, "id": order_id, "status": "confirmed"},
        {"$inc": {"_job_card_lock_version": 1}},
        projection={"_id": 0},
        session=session,
    )
    if order_doc is None:
        existing_order = await database.orders.find_one(
            {"company_id": clean_company, "id": order_id},
            {"_id": 0, "status": 1},
            session=session,
        )
        if not existing_order:
            raise JobCardNotFoundError(f"Parent order '{order_id}' not found")
        status_val = existing_order.get("status")
        raise JobCardLockError(
            f"Parent order '{order_id}' is in '{status_val}' status; "
            "only 'confirmed' orders are eligible for Job Card operations"
        )

    actual_order_no = str(order_doc.get("order_no") or "").strip()
    if actual_order_no != expected_order_no:
        raise JobCardIdentityMismatchError(
            f"Job Card order_no '{expected_order_no}' does not match parent order order_no '{actual_order_no}'"
        )

    actual_customer_id = str(order_doc.get("customer_id") or "").strip()
    if actual_customer_id != expected_customer_id:
        raise JobCardIdentityMismatchError(
            f"Job Card customer_id '{expected_customer_id}' does not match parent order customer_id '{actual_customer_id}'"
        )

    return order_doc


def verify_package_identity(
    master: JobCard,
    drill: DrillDetailsSheet,
    proc: ProcessSequenceSheet,
) -> None:
    """Verify that all seven identity fields match exactly across all three documents.

    Also enforces strict bilateral pointer integrity: a complete Job Card package
    requires both master.drill_sheet_id and master.process_sequence_sheet_id to be
    non-empty after trimming whitespace and to match the corresponding sub-document IDs.
    """
    for field in IDENTITY_FIELDS:
        master_val = getattr(master, field)
        drill_val = getattr(drill, field)
        proc_val = getattr(proc, field)
        if not (master_val == drill_val == proc_val):
            raise JobCardIdentityMismatchError(
                f"Shared identity field '{field}' does not match across package: "
                f"job_card={master_val!r}, drill_sheet={drill_val!r}, process_sheet={proc_val!r}"
            )

    # Strict bilateral sub-document pointer verification (Correction A)
    master_drill_id = str(master.drill_sheet_id or "").strip()
    drill_id = str(drill.id or "").strip()
    if not master_drill_id or master_drill_id != drill_id:
        raise JobCardIdentityMismatchError(
            f"Linked drill_sheet_id mismatch: master specifies {master.drill_sheet_id!r}, "
            f"drill sheet has id {drill.id!r}"
        )

    master_proc_id = str(master.process_sequence_sheet_id or "").strip()
    proc_id = str(proc.id or "").strip()
    if not master_proc_id or master_proc_id != proc_id:
        raise JobCardIdentityMismatchError(
            f"Linked process_sequence_sheet_id mismatch: master specifies {master.process_sequence_sheet_id!r}, "
            f"process sequence sheet has id {proc.id!r}"
        )


# ---------- Service Functions ----------

async def create_job_card_package(
    company_id: str,
    user: dict,
    job_card: JobCard | dict[str, Any] | None = None,
    drill_sheet: DrillDetailsSheet | dict[str, Any] | None = None,
    process_sequence_sheet: ProcessSequenceSheet | dict[str, Any] | None = None,
    request: Request | None = None,
    idempotency_key: str | None = None,
    session: ClientSession | None = None,
    db: Any = None,
    **kwargs: Any,
) -> JobCardPackage:
    """Create and persist an atomic three-document Job Card package.

    Creates:
    - Master Job Card (db.job_cards)
    - Linked Drill Details Sheet (db.drill_detail_sheets)
    - Linked Process Sequence Sheet (db.process_sequence_sheets)

    Validates that:
    - All seven identity fields are present, valid, and identical across all three documents.
    - Tenant isolation is strictly enforced (company_id).
    - No duplicate package exists for (company_id, job_card_no, revision) or (company_id, id).
    - Writes are audited using run_audited_mutation or run_idempotent_mutation.
    """
    if not company_id or not str(company_id).strip():
        raise JobCardValidationError("company_id is required and cannot be empty")

    database = _get_db(db)
    clean_company = str(company_id).strip()

    # 1. Prepare master Job Card dictionary
    if isinstance(job_card, JobCard):
        jc_dict = job_card.model_dump()
    elif isinstance(job_card, dict):
        jc_dict = dict(job_card)
    else:
        jc_dict = dict(kwargs)

    # Validate company_id alignment
    if "company_id" in jc_dict and jc_dict["company_id"] is not None:
        if str(jc_dict["company_id"]).strip() != clean_company:
            raise JobCardIdentityMismatchError(
                f"Job Card company_id '{jc_dict['company_id']}' does not match authenticated company '{clean_company}'"
            )
    jc_dict["company_id"] = clean_company

    # Assign UUIDs with safe normalization (Correction B)
    raw_jc_id = str(jc_dict.get("id") or jc_dict.get("job_card_id") or "").strip()
    jc_id = raw_jc_id if raw_jc_id else new_id()

    raw_drill_id = str(jc_dict.get("drill_sheet_id") or "").strip()
    drill_sheet_id = raw_drill_id if raw_drill_id else new_id()

    raw_proc_id = str(jc_dict.get("process_sequence_sheet_id") or "").strip()
    proc_sheet_id = raw_proc_id if raw_proc_id else new_id()

    jc_dict["id"] = jc_id
    jc_dict["job_card_id"] = jc_id
    jc_dict["drill_sheet_id"] = drill_sheet_id
    jc_dict["process_sequence_sheet_id"] = proc_sheet_id

    created_timestamp = now_iso()
    jc_dict.setdefault("created_at", created_timestamp)
    jc_dict.setdefault("updated_at", created_timestamp)
    jc_dict.setdefault("created_by", user.get("id") or user.get("name") or "system")
    jc_dict.setdefault("status", "draft")

    try:
        master_model = JobCard(**jc_dict)
    except Exception as exc:
        raise JobCardValidationError(str(exc)) from exc

    if master_model.revision != 1:
        raise JobCardValidationError(
            f"Initial Job Card creation requires revision 1 (got revision {master_model.revision}); "
            "use the ECN revision flow to create revisions > 1"
        )
    if master_model.status != "draft":
        raise JobCardValidationError(
            f"Initial Job Card creation requires status 'draft' (got '{master_model.status}')"
        )

    # 2. Prepare Drill Details Sheet dictionary
    if isinstance(drill_sheet, DrillDetailsSheet):
        drill_dict = drill_sheet.model_dump()
    elif isinstance(drill_sheet, dict):
        drill_dict = dict(drill_sheet)
    else:
        drill_dict = {}

    raw_drill_sub_id = str(drill_dict.get("id") or "").strip()
    if raw_drill_sub_id:
        if raw_drill_sub_id != drill_sheet_id:
            raise JobCardIdentityMismatchError(
                f"drill_sheet.id '{drill_dict['id']}' does not match JobCard.drill_sheet_id '{drill_sheet_id}'"
            )
    drill_dict["id"] = drill_sheet_id

    # Align / verify identity fields in drill sheet
    for field in IDENTITY_FIELDS:
        master_val = getattr(master_model, field)
        if field in drill_dict and drill_dict[field] is not None:
            if drill_dict[field] != master_val:
                raise JobCardIdentityMismatchError(
                    f"Drill sheet identity mismatch on '{field}': expected {master_val!r}, got {drill_dict[field]!r}"
                )
        drill_dict[field] = master_val

    drill_dict.setdefault("created_at", master_model.created_at)
    drill_dict.setdefault("updated_at", master_model.updated_at)

    try:
        drill_model = DrillDetailsSheet(**drill_dict)
    except Exception as exc:
        raise JobCardValidationError(str(exc)) from exc

    # 3. Prepare Process Sequence Sheet dictionary
    if isinstance(process_sequence_sheet, ProcessSequenceSheet):
        proc_dict = process_sequence_sheet.model_dump()
    elif isinstance(process_sequence_sheet, dict):
        proc_dict = dict(process_sequence_sheet)
    else:
        proc_dict = {"processes": build_default_process_route()}

    raw_proc_sub_id = str(proc_dict.get("id") or "").strip()
    if raw_proc_sub_id:
        if raw_proc_sub_id != proc_sheet_id:
            raise JobCardIdentityMismatchError(
                f"process_sequence_sheet.id '{proc_dict['id']}' does not match JobCard.process_sequence_sheet_id '{proc_sheet_id}'"
            )
    proc_dict["id"] = proc_sheet_id

    # Align / verify identity fields in process sequence sheet
    for field in IDENTITY_FIELDS:
        master_val = getattr(master_model, field)
        if field in proc_dict and proc_dict[field] is not None:
            if proc_dict[field] != master_val:
                raise JobCardIdentityMismatchError(
                    f"Process sequence sheet identity mismatch on '{field}': expected {master_val!r}, got {proc_dict[field]!r}"
                )
        proc_dict[field] = master_val

    proc_dict.setdefault("created_at", master_model.created_at)
    proc_dict.setdefault("updated_at", master_model.updated_at)

    try:
        proc_model = ProcessSequenceSheet(**proc_dict)
    except Exception as exc:
        raise JobCardValidationError(str(exc)) from exc

    # 4. Strict identity verification across the tri-document package
    verify_package_identity(master_model, drill_model, proc_model)

    # 5. Persistence execution
    async def persist_package(s: ClientSession | None) -> JobCardPackage:
        # Validate and fence parent Order inside the transaction
        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master_model.order_id,
            expected_order_no=master_model.order_no,
            expected_customer_id=master_model.customer_id,
            session=s,
        )

        # Tenant-scoped duplicate identity checks
        dup_jc = await database.job_cards.find_one(
            {
                "company_id": clean_company,
                "$or": [
                    {"id": master_model.id},
                    {"job_card_no": master_model.job_card_no, "revision": master_model.revision},
                ],
            },
            session=s,
        )
        if dup_jc:
            raise DuplicateJobCardPackageError(
                f"Job Card '{master_model.job_card_no}' revision {master_model.revision} already exists"
            )

        # Single active Job Card per Order invariant within tenant scope
        active_for_order = await database.job_cards.find_one(
            {
                "company_id": clean_company,
                "order_id": master_model.order_id,
                "status": {"$in": list(ACTIVE_JOB_CARD_STATUSES)},
            },
            session=s,
        )
        if active_for_order:
            raise DuplicateJobCardPackageError(
                f"Order '{master_model.order_id}' already has an active Job Card "
                f"('{active_for_order.get('job_card_no')}' in '{active_for_order.get('status')}' status; "
                "active job card package already exists)"
            )

        dup_drill = await database.drill_detail_sheets.find_one(
            {
                "company_id": clean_company,
                "$or": [
                    {"id": drill_model.id},
                    {"job_card_id": master_model.id},
                ],
            },
            session=s,
        )
        if dup_drill:
            raise DuplicateJobCardPackageError(
                f"Drill sheet already exists for Job Card '{master_model.id}'"
            )

        dup_proc = await database.process_sequence_sheets.find_one(
            {
                "company_id": clean_company,
                "$or": [
                    {"id": proc_model.id},
                    {"job_card_id": master_model.id},
                ],
            },
            session=s,
        )
        if dup_proc:
            raise DuplicateJobCardPackageError(
                f"Process sequence sheet already exists for Job Card '{master_model.id}'"
            )

        # Insert all three linked documents
        await database.job_cards.insert_one(master_model.model_dump(), session=s)
        await database.drill_detail_sheets.insert_one(drill_model.model_dump(), session=s)
        await database.process_sequence_sheets.insert_one(proc_model.model_dump(), session=s)

        return JobCardPackage(
            job_card=master_model,
            drill_sheet=drill_model,
            process_sequence_sheet=proc_model,
        )

    # If caller already provided an active outer session, execute directly on it
    if session is not None:
        return await persist_package(session)

    req = request or _default_request()

    # If idempotency key is provided, use run_idempotent_mutation
    if idempotency_key is not None:
        from lib.idempotency import run_idempotent_mutation

        # Construct full substantive payload across all three documents,
        # excluding only generated/internal runtime IDs and timestamps.
        master_payload = master_model.model_dump(
            mode="json",
            exclude={"id", "job_card_id", "drill_sheet_id", "process_sequence_sheet_id", "created_at", "updated_at"},
        )
        drill_payload = drill_model.model_dump(
            mode="json",
            exclude={"id", "job_card_id", "created_at", "updated_at"},
        )
        proc_payload = proc_model.model_dump(
            mode="json",
            exclude={"id", "job_card_id", "created_at", "updated_at"},
        )
        payload = {
            "job_card": master_payload,
            "drill_sheet": drill_payload,
            "process_sequence_sheet": proc_payload,
        }
        raw_result = await run_idempotent_mutation(
            company_id=clean_company,
            actor_id=user.get("id") or "system",
            operation=f"job_card.create:{master_model.job_card_no}:{master_model.revision}",
            key=idempotency_key,
            payload=payload,
            mutate=persist_package,
            user=user,
            request=req,
            audit_action="job_card.created",
            audit_resource_type="job_card",
        )
        if isinstance(raw_result, JobCardPackage):
            return raw_result
        return JobCardPackage(
            job_card=JobCard(**raw_result["job_card"]),
            drill_sheet=DrillDetailsSheet(**raw_result["drill_sheet"]),
            process_sequence_sheet=ProcessSequenceSheet(**raw_result["process_sequence_sheet"]),
        )

    # Standard audited mutation
    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.created",
        resource_type="job_card",
        resource_id=master_model.id,
        mutate=persist_package,
        after_state={
            "job_card_id": master_model.id,
            "job_card_no": master_model.job_card_no,
            "revision": master_model.revision,
            "order_id": master_model.order_id,
        },
    )


async def get_job_card_package(
    company_id: str,
    job_card_id: str,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCardPackage:
    """Retrieve the complete tri-document Job Card package with identity verification.

    Enforces tenant isolation by scoping every query with company_id.
    Validates that:
    - Master Job Card exists in db.job_cards.
    - Linked Drill Details Sheet exists in db.drill_detail_sheets.
    - Linked Process Sequence Sheet exists in db.process_sequence_sheets.
    - All seven shared identity fields match exactly across all three documents.
    """
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()

    jc_doc = await database.job_cards.find_one(
        {"company_id": clean_company, "id": clean_id},
        {"_id": 0},
        session=session,
    )
    if not jc_doc:
        raise JobCardNotFoundError(f"Job Card '{clean_id}' not found")

    drill_doc = await database.drill_detail_sheets.find_one(
        {"company_id": clean_company, "job_card_id": clean_id},
        {"_id": 0},
        session=session,
    )
    if not drill_doc:
        raise JobCardNotFoundError(f"Linked Drill Details Sheet for Job Card '{clean_id}' not found")

    proc_doc = await database.process_sequence_sheets.find_one(
        {"company_id": clean_company, "job_card_id": clean_id},
        {"_id": 0},
        session=session,
    )
    if not proc_doc:
        raise JobCardNotFoundError(f"Linked Process Sequence Sheet for Job Card '{clean_id}' not found")

    try:
        jc = JobCard(**jc_doc)
        drill = DrillDetailsSheet(**drill_doc)
        proc = ProcessSequenceSheet(**proc_doc)
    except Exception as exc:
        raise JobCardValidationError(f"Persisted Job Card package document invalid: {exc}") from exc

    verify_package_identity(jc, drill, proc)

    return JobCardPackage(
        job_card=jc,
        drill_sheet=drill,
        process_sequence_sheet=proc,
    )


async def update_draft_job_card(
    company_id: str,
    job_card_id: str,
    user: dict,
    patch: dict[str, Any],
    expected_version: int | None = None,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCard:
    """Update a Job Card document while strictly enforcing draft status, identity immutability,
    and optimistic concurrency control.

    Rules:
    - Only records in 'draft' status may be modified.
    - Modifications to 'approved', 'released_to_production', and 'superseded' are rejected.
    - Modification of immutable identity fields (job_card_no, revision, order_id, company_id,
      drill_sheet_id, process_sequence_sheet_id, etc.) is rejected.
    - Concurrency control: draft updates verify against the stored document version and
      atomically increment version upon successful mutation.
    - Updated document is fully validated through the JobCard Pydantic model.
    - Mutations are audited via run_audited_mutation.
    """
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()

    existing = await database.job_cards.find_one(
        {"company_id": clean_company, "id": clean_id},
        {"_id": 0},
        session=session,
    )
    if not existing:
        raise JobCardNotFoundError(f"Job Card '{clean_id}' not found")

    current_status = existing.get("status")
    if current_status != "draft":
        raise JobCardLockError(
            f"Cannot update Job Card '{clean_id}' in '{current_status}' status; "
            f"only 'draft' records may be modified."
        )

    # Resolve expected version from argument or patch body
    target_expected_version = expected_version
    if "version" in patch and patch["version"] is not None:
        try:
            patch_ver = int(patch["version"])
        except (ValueError, TypeError) as exc:
            raise JobCardValidationError(f"Invalid version specification: {patch['version']}") from exc

        if target_expected_version is not None and target_expected_version != patch_ver:
            raise JobCardLockError(
                f"Contradictory version expectations: parameter={target_expected_version} vs patch={patch_ver}"
            )
        target_expected_version = patch_ver

    stored_version = existing.get("version")
    current_version = int(stored_version) if stored_version is not None else 1

    if target_expected_version is not None and target_expected_version != current_version:
        raise JobCardLockError(
            f"Job Card '{clean_id}' version conflict: expected version {target_expected_version}, "
            f"but current version is {current_version}"
        )

    # Next version increment
    new_version = current_version + 1

    # Check for attempted identity field tampering
    for key, val in patch.items():
        if key in PROTECTED_IDENTITY_FIELDS:
            existing_val = existing.get(key)
            if val is not None and str(val).strip() != str(existing_val).strip():
                raise JobCardIdentityMismatchError(
                    f"Modification of protected identity field '{key}' is forbidden. "
                    f"Existing: {existing_val!r}, Attempted: {val!r}"
                )

    sanitized_patch = {
        k: v for k, v in patch.items()
        if k not in PROTECTED_IDENTITY_FIELDS and k != "version"
    }
    merged = {
        **existing,
        **sanitized_patch,
        "version": new_version,
        "updated_at": now_iso(),
    }

    try:
        updated_card = JobCard(**merged)
    except Exception as exc:
        raise JobCardValidationError(str(exc)) from exc

    # Enforce linked document reference preservation
    if updated_card.drill_sheet_id != existing.get("drill_sheet_id"):
        raise JobCardIdentityMismatchError("drill_sheet_id reference cannot be changed")
    if updated_card.process_sequence_sheet_id != existing.get("process_sequence_sheet_id"):
        raise JobCardIdentityMismatchError("process_sequence_sheet_id reference cannot be changed")

    # Optimistic concurrency condition:
    # If legacy document lacked 'version', match version=1 or missing version field.
    version_condition: dict[str, Any]
    if stored_version is None:
        version_condition = {"$or": [{"version": 1}, {"version": {"$exists": False}}]}
    else:
        version_condition = {"version": current_version}

    update_filter = {
        "company_id": clean_company,
        "id": clean_id,
        "status": "draft",
        **version_condition,
    }

    async def do_update(s: ClientSession | None) -> JobCard:
        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=updated_card.order_id,
            expected_order_no=updated_card.order_no,
            expected_customer_id=updated_card.customer_id,
            session=s,
        )
        res = await database.job_cards.update_one(
            update_filter,
            {"$set": updated_card.model_dump()},
            session=s,
        )
        matched = getattr(res, "matched_count", 0) if res is not None else 0
        if not matched:
            raise JobCardLockError(
                f"Job Card '{clean_id}' was not updated (concurrent update, version conflict, or state change)"
            )
        return updated_card

    if session is not None:
        return await do_update(session)

    req = request or _default_request()
    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.updated",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_update,
        after_state={
            "job_card_id": clean_id,
            "version": updated_card.version,
            "status": updated_card.status,
            "updated_fields": list(sanitized_patch.keys()),
        },
    )


async def update_draft_drill_sheet(
    company_id: str,
    job_card_id: str,
    user: dict,
    patch: dict[str, Any],
    expected_version: int | None = None,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> DrillDetailsSheet:
    """Update a linked Drill Details Sheet while master Job Card is in draft and sheet is unlocked."""
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()

    target_expected_version = _resolve_expected_version(expected_version, patch, required=False)

    # Check server-managed field attempts before DB mutation
    for managed_field in SHEET_SERVER_MANAGED_FIELDS:
        if managed_field in patch:
            raise JobCardValidationError(
                f"Drill Details Sheet field '{managed_field}' is server-managed and cannot be modified"
            )

    pkg = await get_job_card_package(clean_company, clean_id, session=session, db=database)
    master = pkg.job_card
    if master.status != "draft":
        raise JobCardLockError(
            f"Cannot update Drill Details Sheet for Job Card '{clean_id}' in '{master.status}' status; "
            "only 'draft' Job Cards may be modified"
        )

    existing_drill_doc = await database.drill_detail_sheets.find_one(
        {"company_id": clean_company, "job_card_id": clean_id},
        {"_id": 0},
        session=session,
    )
    if not existing_drill_doc:
        raise JobCardNotFoundError(f"Linked Drill Details Sheet for Job Card '{clean_id}' not found")

    if existing_drill_doc.get("is_locked"):
        raise JobCardLockError(
            f"Cannot update Drill Details Sheet for Job Card '{clean_id}' because is_locked is True"
        )

    stored_version = existing_drill_doc.get("version")
    current_version = int(stored_version) if stored_version is not None else 1
    if target_expected_version is not None and target_expected_version != current_version:
        raise JobCardLockError(
            f"Drill Details Sheet for Job Card '{clean_id}' version conflict: "
            f"expected version {target_expected_version}, but current version is {current_version}"
        )

    for key, val in patch.items():
        if key in PROTECTED_SHEET_IDENTITY_FIELDS:
            existing_val = existing_drill_doc.get(key)
            if val is not None and str(val).strip() != str(existing_val).strip():
                raise JobCardIdentityMismatchError(
                    f"Modification of protected identity field '{key}' is forbidden. "
                    f"Existing: {existing_val!r}, Attempted: {val!r}"
                )

    sanitized_patch = {
        k: v
        for k, v in patch.items()
        if k not in PROTECTED_SHEET_IDENTITY_FIELDS and k not in SHEET_SERVER_MANAGED_FIELDS and k != "version"
    }
    if not sanitized_patch:
        raise JobCardValidationError("no updatable drill sheet fields were supplied")

    new_version = current_version + 1
    merged = {
        **existing_drill_doc,
        **sanitized_patch,
        "version": new_version,
        "updated_at": now_iso(),
    }

    try:
        updated_drill = DrillDetailsSheet(**merged)
    except Exception as exc:
        raise JobCardValidationError(str(exc)) from exc

    verify_package_identity(master, updated_drill, pkg.process_sequence_sheet)

    version_cond = _version_condition(stored_version, current_version)
    update_filter = {
        "company_id": clean_company,
        "id": updated_drill.id,
        "job_card_id": clean_id,
        "is_locked": False,
        **version_cond,
    }

    async def do_update(s: ClientSession | None) -> DrillDetailsSheet:
        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )
        master_in_tx = await database.job_cards.find_one(
            {"company_id": clean_company, "id": clean_id, "status": "draft"},
            {"_id": 1},
            session=s,
        )
        if not master_in_tx:
            raise JobCardLockError(
                f"Cannot update Drill Details Sheet for Job Card '{clean_id}'; master Job Card is not in 'draft' status"
            )
        res = await database.drill_detail_sheets.update_one(
            update_filter,
            {"$set": updated_drill.model_dump()},
            session=s,
        )
        matched = getattr(res, "matched_count", 0) if res is not None else 0
        if not matched:
            raise JobCardLockError(
                f"Drill Details Sheet for Job Card '{clean_id}' was not updated "
                "(concurrent update, version conflict, or lock change)"
            )
        return updated_drill

    if session is not None:
        return await do_update(session)

    req = request or _default_request()
    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.drill_sheet.updated",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_update,
        after_state={
            "job_card_id": clean_id,
            "drill_sheet_id": updated_drill.id,
            "version": updated_drill.version,
            "updated_fields": list(sanitized_patch.keys()),
        },
    )


async def update_draft_process_sequence_sheet(
    company_id: str,
    job_card_id: str,
    user: dict,
    patch: dict[str, Any],
    expected_version: int | None = None,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> ProcessSequenceSheet:
    """Update a linked Process Sequence Sheet while master Job Card is in draft status."""
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()

    target_expected_version = _resolve_expected_version(expected_version, patch, required=False)

    for managed_field in SHEET_SERVER_MANAGED_FIELDS:
        if managed_field in patch:
            raise JobCardValidationError(
                f"Process Sequence Sheet field '{managed_field}' is server-managed and cannot be modified"
            )

    pkg = await get_job_card_package(clean_company, clean_id, session=session, db=database)
    master = pkg.job_card
    if master.status != "draft":
        raise JobCardLockError(
            f"Cannot update Process Sequence Sheet for Job Card '{clean_id}' in '{master.status}' status; "
            "only 'draft' Job Cards may be modified"
        )

    existing_proc_doc = await database.process_sequence_sheets.find_one(
        {"company_id": clean_company, "job_card_id": clean_id},
        {"_id": 0},
        session=session,
    )
    if not existing_proc_doc:
        raise JobCardNotFoundError(f"Linked Process Sequence Sheet for Job Card '{clean_id}' not found")

    stored_version = existing_proc_doc.get("version")
    current_version = int(stored_version) if stored_version is not None else 1
    if target_expected_version is not None and target_expected_version != current_version:
        raise JobCardLockError(
            f"Process Sequence Sheet for Job Card '{clean_id}' version conflict: "
            f"expected version {target_expected_version}, but current version is {current_version}"
        )

    for key, val in patch.items():
        if key in PROTECTED_SHEET_IDENTITY_FIELDS:
            existing_val = existing_proc_doc.get(key)
            if val is not None and str(val).strip() != str(existing_val).strip():
                raise JobCardIdentityMismatchError(
                    f"Modification of protected identity field '{key}' is forbidden. "
                    f"Existing: {existing_val!r}, Attempted: {val!r}"
                )

    sanitized_patch = {
        k: v
        for k, v in patch.items()
        if k not in PROTECTED_SHEET_IDENTITY_FIELDS and k not in SHEET_SERVER_MANAGED_FIELDS and k != "version"
    }
    if not sanitized_patch:
        raise JobCardValidationError("no updatable process sequence fields were supplied")

    new_version = current_version + 1
    merged = {
        **existing_proc_doc,
        **sanitized_patch,
        "version": new_version,
        "updated_at": now_iso(),
    }

    try:
        updated_proc = ProcessSequenceSheet(**merged)
    except Exception as exc:
        raise JobCardValidationError(str(exc)) from exc

    if not any(p.is_active for p in updated_proc.processes):
        raise JobCardValidationError(
            "Process Sequence Sheet must contain at least one active process (is_active=True)"
        )

    verify_package_identity(master, pkg.drill_sheet, updated_proc)

    version_cond = _version_condition(stored_version, current_version)
    update_filter = {
        "company_id": clean_company,
        "id": updated_proc.id,
        "job_card_id": clean_id,
        **version_cond,
    }

    async def do_update(s: ClientSession | None) -> ProcessSequenceSheet:
        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )
        master_in_tx = await database.job_cards.find_one(
            {"company_id": clean_company, "id": clean_id, "status": "draft"},
            {"_id": 1},
            session=s,
        )
        if not master_in_tx:
            raise JobCardLockError(
                f"Cannot update Process Sequence Sheet for Job Card '{clean_id}'; master Job Card is not in 'draft' status"
            )
        res = await database.process_sequence_sheets.update_one(
            update_filter,
            {"$set": updated_proc.model_dump()},
            session=s,
        )
        matched = getattr(res, "matched_count", 0) if res is not None else 0
        if not matched:
            raise JobCardLockError(
                f"Process Sequence Sheet for Job Card '{clean_id}' was not updated "
                "(concurrent update or version conflict)"
            )
        return updated_proc

    if session is not None:
        return await do_update(session)

    req = request or _default_request()
    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.process_sequence_sheet.updated",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_update,
        after_state={
            "job_card_id": clean_id,
            "process_sequence_sheet_id": updated_proc.id,
            "version": updated_proc.version,
            "updated_fields": list(sanitized_patch.keys()),
        },
    )


async def submit_job_card_for_review(
    company_id: str,
    job_card_id: str,
    user: dict,
    expected_version: int,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCardPackage:
    """Transition a draft Job Card package to engineering_review status."""
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()
    target_expected = _resolve_expected_version(expected_version, required=True)
    after_state: dict[str, Any] = {"job_card_id": clean_id, "status": "engineering_review"}

    async def do_submit(s: ClientSession | None) -> JobCardPackage:
        pkg = await get_job_card_package(clean_company, clean_id, session=s, db=database)
        master = pkg.job_card
        if master.status != "draft":
            raise JobCardLockError(
                f"Cannot submit Job Card '{clean_id}' for review from '{master.status}' status; "
                "only 'draft' Job Cards can be submitted for engineering review"
            )
        if master.version != target_expected:
            raise JobCardLockError(
                f"Job Card '{clean_id}' version conflict: expected version {target_expected}, "
                f"but current version is {master.version}"
            )
        if not any(p.is_active for p in pkg.process_sequence_sheet.processes):
            raise JobCardValidationError(
                "Process Sequence Sheet must contain at least one active process before submitting for review"
            )

        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )

        new_version = master.version + 1
        ts = now_iso()
        updated_master = JobCard(
            **{
                **master.model_dump(),
                "status": "engineering_review",
                "version": new_version,
                "updated_at": ts,
            }
        )
        res = await database.job_cards.update_one(
            {
                "company_id": clean_company,
                "id": clean_id,
                "status": "draft",
                "version": master.version,
            },
            {"$set": updated_master.model_dump()},
            session=s,
        )
        matched = getattr(res, "matched_count", 0) if res is not None else 0
        if not matched:
            raise JobCardLockError(
                f"Job Card '{clean_id}' was not submitted (concurrent update, version conflict, or state change)"
            )
        after_state["version"] = updated_master.version
        return JobCardPackage(
            job_card=updated_master,
            drill_sheet=pkg.drill_sheet,
            process_sequence_sheet=pkg.process_sequence_sheet,
        )

    if session is not None:
        return await do_submit(session)

    req = request or _default_request()
    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.submitted_for_review",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_submit,
        after_state=after_state,
    )


async def return_job_card_to_draft(
    company_id: str,
    job_card_id: str,
    user: dict,
    expected_version: int,
    reason: str,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCardPackage:
    """Return a Job Card from engineering_review to draft status with a mandatory review reason.

    Strictly forbidden from approved, released_to_production, superseded, or draft.
    """
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()
    target_expected = _resolve_expected_version(expected_version, required=True)

    clean_reason = str(reason or "").strip()
    if len(clean_reason) < 3:
        raise JobCardValidationError(
            "A non-empty return reason (at least 3 characters) is required when returning a Job Card to draft"
        )

    after_state: dict[str, Any] = {
        "job_card_id": clean_id,
        "status": "draft",
        "reason": clean_reason,
    }

    async def do_return(s: ClientSession | None) -> JobCardPackage:
        pkg = await get_job_card_package(clean_company, clean_id, session=s, db=database)
        master = pkg.job_card
        if master.status != "engineering_review":
            raise JobCardLockError(
                f"Cannot return Job Card '{clean_id}' from '{master.status}' to 'draft'; "
                "only 'engineering_review' Job Cards may be returned to draft"
            )
        if master.version != target_expected:
            raise JobCardLockError(
                f"Job Card '{clean_id}' version conflict: expected version {target_expected}, "
                f"but current version is {master.version}"
            )

        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )

        new_version = master.version + 1
        ts = now_iso()
        updated_master = JobCard(
            **{
                **master.model_dump(),
                "status": "draft",
                "version": new_version,
                "updated_at": ts,
            }
        )
        res = await database.job_cards.update_one(
            {
                "company_id": clean_company,
                "id": clean_id,
                "status": "engineering_review",
                "version": master.version,
            },
            {"$set": updated_master.model_dump()},
            session=s,
        )
        matched = getattr(res, "matched_count", 0) if res is not None else 0
        if not matched:
            raise JobCardLockError(
                f"Job Card '{clean_id}' was not returned to draft (concurrent update, version conflict, or state change)"
            )
        after_state["version"] = updated_master.version
        return JobCardPackage(
            job_card=updated_master,
            drill_sheet=pkg.drill_sheet,
            process_sequence_sheet=pkg.process_sequence_sheet,
        )

    if session is not None:
        return await do_return(session)

    req = request or _default_request()
    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.returned_to_draft",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_return,
        after_state=after_state,
    )


async def approve_job_card(
    company_id: str,
    job_card_id: str,
    user: dict,
    expected_version: int,
    idempotency_key: str | None = None,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCardPackage:
    """Approve a Job Card from engineering_review, locking the linked Drill Details Sheet."""
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()
    target_expected = _resolve_expected_version(expected_version, required=True)
    approver_id = user.get("id") or user.get("name") or "system"
    after_state: dict[str, Any] = {
        "job_card_id": clean_id,
        "status": "approved",
        "approved_by": approver_id,
    }

    async def do_approve(s: ClientSession | None) -> JobCardPackage:
        pkg = await get_job_card_package(clean_company, clean_id, session=s, db=database)
        master = pkg.job_card
        drill = pkg.drill_sheet
        proc = pkg.process_sequence_sheet

        if master.status != "engineering_review":
            raise JobCardLockError(
                f"Cannot approve Job Card '{clean_id}' from '{master.status}' status; "
                "only 'engineering_review' Job Cards may be approved"
            )
        if master.version != target_expected:
            raise JobCardLockError(
                f"Job Card '{clean_id}' version conflict: expected version {target_expected}, "
                f"but current version is {master.version}"
            )
        if not any(p.is_active for p in proc.processes):
            raise JobCardValidationError(
                "Process Sequence Sheet must contain at least one active process before approval"
            )

        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )

        ts = now_iso()
        updated_master = JobCard(
            **{
                **master.model_dump(),
                "status": "approved",
                "approved_by": approver_id,
                "approved_at": ts,
                "version": master.version + 1,
                "updated_at": ts,
            }
        )
        updated_drill = DrillDetailsSheet(
            **{
                **drill.model_dump(),
                "is_locked": True,
                "version": drill.version + 1,
                "updated_at": ts,
            }
        )

        res_master = await database.job_cards.update_one(
            {
                "company_id": clean_company,
                "id": clean_id,
                "status": "engineering_review",
                "version": master.version,
            },
            {"$set": updated_master.model_dump()},
            session=s,
        )
        if not (getattr(res_master, "matched_count", 0) if res_master is not None else 0):
            raise JobCardLockError(
                f"Job Card '{clean_id}' was not approved (concurrent update, version conflict, or state change)"
            )

        res_drill = await database.drill_detail_sheets.update_one(
            {
                "company_id": clean_company,
                "id": drill.id,
                "job_card_id": clean_id,
            },
            {"$set": updated_drill.model_dump()},
            session=s,
        )
        if not (getattr(res_drill, "matched_count", 0) if res_drill is not None else 0):
            raise JobCardLockError(
                f"Linked Drill Details Sheet for Job Card '{clean_id}' could not be locked during approval"
            )

        after_state["version"] = updated_master.version
        after_state["drill_sheet_version"] = updated_drill.version
        return JobCardPackage(
            job_card=updated_master,
            drill_sheet=updated_drill,
            process_sequence_sheet=proc,
        )

    if session is not None:
        return await do_approve(session)

    req = request or _default_request()
    if idempotency_key is not None:
        from lib.idempotency import run_idempotent_mutation

        raw_result = await run_idempotent_mutation(
            company_id=clean_company,
            actor_id=user.get("id") or "system",
            operation=f"job_card.approve:{clean_id}:{target_expected}",
            key=idempotency_key,
            payload={"job_card_id": clean_id, "expected_version": target_expected},
            mutate=do_approve,
            user=user,
            request=req,
            audit_action="job_card.approved",
            audit_resource_type="job_card",
        )
        return _coerce_package(raw_result)

    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.approved",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_approve,
        after_state=after_state,
    )


async def release_job_card(
    company_id: str,
    job_card_id: str,
    user: dict,
    expected_version: int,
    idempotency_key: str | None = None,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCardPackage:
    """Release an approved Job Card package to production (released_to_production)."""
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()
    target_expected = _resolve_expected_version(expected_version, required=True)
    after_state: dict[str, Any] = {
        "job_card_id": clean_id,
        "status": "released_to_production",
    }

    async def do_release(s: ClientSession | None) -> JobCardPackage:
        pkg = await get_job_card_package(clean_company, clean_id, session=s, db=database)
        master = pkg.job_card
        drill = pkg.drill_sheet
        proc = pkg.process_sequence_sheet

        if master.status != "approved":
            raise JobCardLockError(
                f"Cannot release Job Card '{clean_id}' from '{master.status}' status; "
                "only 'approved' Job Cards may be released to production"
            )
        if master.version != target_expected:
            raise JobCardLockError(
                f"Job Card '{clean_id}' version conflict: expected version {target_expected}, "
                f"but current version is {master.version}"
            )
        if not drill.is_locked:
            raise JobCardValidationError(
                f"Cannot release Job Card '{clean_id}' while Drill Details Sheet is unlocked"
            )

        active_seqs = [p.process_seq for p in proc.processes if p.is_active]
        if not active_seqs:
            raise JobCardValidationError(
                "Process Sequence Sheet must contain at least one active process before releasing to production"
            )

        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )

        first_active_seq = min(active_seqs)
        updated_processes: list[dict[str, Any]] = []
        for entry in proc.processes:
            entry_dict = entry.model_dump()
            entry_dict["status"] = "waiting" if entry.is_active else "skipped"
            updated_processes.append(entry_dict)

        ts = now_iso()
        updated_master = JobCard(
            **{
                **master.model_dump(),
                "status": "released_to_production",
                "version": master.version + 1,
                "updated_at": ts,
            }
        )
        updated_proc = ProcessSequenceSheet(
            **{
                **proc.model_dump(),
                "processes": updated_processes,
                "current_process_seq": first_active_seq,
                "version": proc.version + 1,
                "updated_at": ts,
            }
        )

        res_master = await database.job_cards.update_one(
            {
                "company_id": clean_company,
                "id": clean_id,
                "status": "approved",
                "version": master.version,
            },
            {"$set": updated_master.model_dump()},
            session=s,
        )
        if not (getattr(res_master, "matched_count", 0) if res_master is not None else 0):
            raise JobCardLockError(
                f"Job Card '{clean_id}' was not released (concurrent update, version conflict, or state change)"
            )

        res_proc = await database.process_sequence_sheets.update_one(
            {
                "company_id": clean_company,
                "id": proc.id,
                "job_card_id": clean_id,
            },
            {"$set": updated_proc.model_dump()},
            session=s,
        )
        if not (getattr(res_proc, "matched_count", 0) if res_proc is not None else 0):
            raise JobCardLockError(
                f"Linked Process Sequence Sheet for Job Card '{clean_id}' could not be updated during release"
            )

        after_state["version"] = updated_master.version
        after_state["current_process_seq"] = first_active_seq
        return JobCardPackage(
            job_card=updated_master,
            drill_sheet=drill,
            process_sequence_sheet=updated_proc,
        )

    if session is not None:
        return await do_release(session)

    req = request or _default_request()
    if idempotency_key is not None:
        from lib.idempotency import run_idempotent_mutation

        raw_result = await run_idempotent_mutation(
            company_id=clean_company,
            actor_id=user.get("id") or "system",
            operation=f"job_card.release:{clean_id}:{target_expected}",
            key=idempotency_key,
            payload={"job_card_id": clean_id, "expected_version": target_expected},
            mutate=do_release,
            user=user,
            request=req,
            audit_action="job_card.released_to_production",
            audit_resource_type="job_card",
        )
        return _coerce_package(raw_result)

    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.released_to_production",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_release,
        after_state=after_state,
    )


async def create_job_card_revision(
    company_id: str,
    job_card_id: str,
    user: dict,
    expected_version: int,
    revision_reason: str,
    idempotency_key: str | None = None,
    request: Request | None = None,
    session: ClientSession | None = None,
    db: Any = None,
) -> JobCardPackage:
    """Create a controlled post-approval ECN revision (revision N+1 in draft) and supersede revision N.

    In Day 1.9, ECN revisions are permitted ONLY from JobCard.status == 'approved'.
    Attempting revision from draft, engineering_review, released_to_production, or superseded
    is rejected with HTTP 409 (JobCardLockError).
    """
    if not company_id or not str(company_id).strip():
        raise JobCardNotFoundError("Invalid company ID")
    if not job_card_id or not str(job_card_id).strip():
        raise JobCardNotFoundError("Invalid job card ID")

    database = _get_db(db)
    clean_company = str(company_id).strip()
    clean_id = str(job_card_id).strip()
    target_expected = _resolve_expected_version(expected_version, required=True)

    clean_reason = str(revision_reason or "").strip()
    if not clean_reason:
        raise JobCardValidationError("revision_reason is required and cannot be empty for ECN revisions")

    creator_id = user.get("id") or user.get("name") or "system"
    after_state: dict[str, Any] = {
        "previous_revision_id": clean_id,
        "revision_reason": clean_reason,
    }

    async def do_revision(s: ClientSession | None) -> JobCardPackage:
        pkg = await get_job_card_package(clean_company, clean_id, session=s, db=database)
        master = pkg.job_card
        drill = pkg.drill_sheet
        proc = pkg.process_sequence_sheet

        if master.status != "approved":
            raise JobCardLockError(
                f"Cannot create ECN revision from Job Card '{clean_id}' in '{master.status}' status; "
                "only 'approved' Job Cards are eligible for ECN revision in Day 1.9"
            )
        if master.version != target_expected:
            raise JobCardLockError(
                f"Job Card '{clean_id}' version conflict: expected version {target_expected}, "
                f"but current version is {master.version}"
            )

        await _validate_and_fence_parent_order(
            database=database,
            clean_company=clean_company,
            order_id=master.order_id,
            expected_order_no=master.order_no,
            expected_customer_id=master.customer_id,
            session=s,
        )

        new_revision_num = master.revision + 1
        dup_rev = await database.job_cards.find_one(
            {
                "company_id": clean_company,
                "job_card_no": master.job_card_no,
                "revision": new_revision_num,
            },
            session=s,
        )
        if dup_rev:
            raise DuplicateJobCardPackageError(
                f"Job Card '{master.job_card_no}' revision {new_revision_num} already exists"
            )

        new_jc_id = new_id()
        new_drill_id = new_id()
        new_proc_id = new_id()
        ts = now_iso()

        # 1. Supersede revision N first so partial unique index on active (company_id, order_id) is satisfied
        superseded_master = JobCard(
            **{
                **master.model_dump(),
                "status": "superseded",
                "superseded_by_revision_id": new_jc_id,
                "superseded_at": ts,
                "version": master.version + 1,
                "updated_at": ts,
            }
        )
        res_supersede = await database.job_cards.update_one(
            {
                "company_id": clean_company,
                "id": clean_id,
                "status": "approved",
                "version": master.version,
            },
            {"$set": superseded_master.model_dump()},
            session=s,
        )
        if not (getattr(res_supersede, "matched_count", 0) if res_supersede is not None else 0):
            raise JobCardLockError(
                f"Job Card '{clean_id}' could not be superseded (concurrent update, version conflict, or state change)"
            )

        # 2. Construct new revision N+1 tri-document package in draft status
        master_base = master.model_dump()
        master_base.update(
            {
                "id": new_jc_id,
                "job_card_id": new_jc_id,
                "revision": new_revision_num,
                "revision_code": f"REV-{new_revision_num:02d}",
                "revision_reason": clean_reason,
                "previous_revision_id": master.id,
                "superseded_by_revision_id": None,
                "superseded_at": None,
                "status": "draft",
                "version": 1,
                "approved_by": None,
                "approved_at": None,
                "qa_completion": None,
                "raw_material_batches": [],
                "drill_sheet_id": new_drill_id,
                "process_sequence_sheet_id": new_proc_id,
                "created_at": ts,
                "updated_at": ts,
                "created_by": creator_id,
            }
        )

        drill_base = drill.model_dump()
        drill_base.update(
            {
                "id": new_drill_id,
                "job_card_id": new_jc_id,
                "revision": new_revision_num,
                "version": 1,
                "is_locked": False,
                "inspection": None,
                "created_at": ts,
                "updated_at": ts,
            }
        )

        reset_processes: list[dict[str, Any]] = []
        for p in proc.processes:
            reset_processes.append(
                ProcessExecutionEntry(
                    process_seq=p.process_seq,
                    process_name=p.process_name,
                    process_code=p.process_code,
                    department=p.department,
                    assigned_supervisor=p.assigned_supervisor,
                    assigned_machine_id=p.assigned_machine_id,
                    is_active=p.is_active,
                    status="waiting",
                ).model_dump()
            )

        proc_base = proc.model_dump()
        proc_base.update(
            {
                "id": new_proc_id,
                "job_card_id": new_jc_id,
                "revision": new_revision_num,
                "version": 1,
                "processes": reset_processes,
                "current_process_seq": None,
                "created_at": ts,
                "updated_at": ts,
            }
        )

        try:
            new_master = JobCard(**master_base)
            new_drill = DrillDetailsSheet(**drill_base)
            new_proc = ProcessSequenceSheet(**proc_base)
        except Exception as exc:
            raise JobCardValidationError(str(exc)) from exc

        verify_package_identity(new_master, new_drill, new_proc)

        await database.job_cards.insert_one(new_master.model_dump(), session=s)
        await database.drill_detail_sheets.insert_one(new_drill.model_dump(), session=s)
        await database.process_sequence_sheets.insert_one(new_proc.model_dump(), session=s)

        after_state["new_job_card_id"] = new_master.id
        after_state["job_card_no"] = new_master.job_card_no
        after_state["revision"] = new_master.revision
        return JobCardPackage(
            job_card=new_master,
            drill_sheet=new_drill,
            process_sequence_sheet=new_proc,
        )

    if session is not None:
        return await do_revision(session)

    req = request or _default_request()
    if idempotency_key is not None:
        from lib.idempotency import run_idempotent_mutation

        raw_result = await run_idempotent_mutation(
            company_id=clean_company,
            actor_id=user.get("id") or "system",
            operation=f"job_card.revision:{clean_id}:{target_expected}",
            key=idempotency_key,
            payload={
                "job_card_id": clean_id,
                "expected_version": target_expected,
                "revision_reason": clean_reason,
            },
            mutate=do_revision,
            user=user,
            request=req,
            audit_action="job_card.revision_created",
            audit_resource_type="job_card",
        )
        return _coerce_package(raw_result)

    from lib.audit import run_audited_mutation

    return await run_audited_mutation(
        company_id=clean_company,
        user=user,
        request=req,
        action="job_card.revision_created",
        resource_type="job_card",
        resource_id=clean_id,
        mutate=do_revision,
        after_state=after_state,
    )
