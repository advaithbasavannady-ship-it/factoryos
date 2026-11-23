"""Job Card HTTP API (Day 1.7B).

Thin HTTP layer over ``services.job_cards``. All persistence, identity verification,
audit, idempotency and optimistic-concurrency logic lives in the service layer and its
helpers (``lib.audit``, ``lib.idempotency``); this router only:

- resolves the tenant exclusively from the authenticated session membership,
- enforces the existing ``job_cards.read`` / ``job_cards.manage`` permissions,
- restricts client input to fields a client may legitimately supply,
- forwards the ``Idempotency-Key`` header and the expected version, and
- maps failures to safe HTTP responses without leaking internal details.

This router intentionally does not touch orders, inventory, production or scrap
workflows, and it never changes order stages.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from pymongo.errors import DuplicateKeyError, PyMongoError

from lib.authz import current_user, require_permission
from lib.tenant import get_company_id
from models.job_cards import (
    DrillDetailsSheet,
    DrillInspectionInfo,
    DrillSetupSpecs,
    DrillToolEntry,
    JobCard,
    MaterialSpecifications,
    PanelDimensions,
    PcbDimensions,
    ProcessExecutionEntry,
    ProcessSequenceSheet,
    ProcessSpecifications,
    RawMaterialBatchRef,
)
from services.job_cards import (
    JobCardPackage,
    JobCardServiceError,
    approve_job_card,
    create_job_card_package,
    create_job_card_revision,
    get_job_card_package,
    release_job_card,
    return_job_card_to_draft,
    submit_job_card_for_review,
    update_draft_drill_sheet,
    update_draft_job_card,
    update_draft_process_sequence_sheet,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Lifecycle, approval and audit fields that only the server may set. Accepting them on
# create would allow a client to create a package directly in an approved/released state,
# forge its creator, or pre-set its concurrency version.
MASTER_SERVER_MANAGED_FIELDS: frozenset[str] = frozenset({
    "status",
    "version",
    "created_by",
    "created_at",
    "updated_at",
    "approved_by",
    "approved_at",
    "superseded_by_revision_id",
    "superseded_at",
})
SHEET_SERVER_MANAGED_FIELDS: frozenset[str] = frozenset({
    "created_at",
    "updated_at",
    "is_locked",
    "version",
})


# ---------- Request schemas ----------

class JobCardPackageCreate(BaseModel):
    """Create request carrying the three linked documents.

    Each section is validated by the service through the existing ``JobCard``,
    ``DrillDetailsSheet`` and ``ProcessSequenceSheet`` models after the server has
    injected the authenticated ``company_id`` and the shared identity fields. A
    client-supplied ``company_id`` that differs from the session tenant is rejected.
    Omitting ``process_sequence_sheet`` assigns the standard 24-process route.
    """

    model_config = ConfigDict(extra="forbid")

    job_card: dict[str, Any]
    drill_sheet: Optional[dict[str, Any]] = None
    process_sequence_sheet: Optional[dict[str, Any]] = None


class JobCardDraftUpdate(BaseModel):
    """Draft update request. Only engineering/commercial draft fields are accepted.

    Identity fields (including customer_id), linked-document IDs, status, approval fields,
    timestamps and the stored version are not accepted (``extra="forbid"`` returns HTTP 422).
    ``expected_version`` is mandatory for optimistic concurrency control.
    """

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)

    customer_name: Optional[str] = None
    work_order_no: Optional[str] = None
    customer_po_no: Optional[str] = None
    order_quantity_pcb: Optional[int] = Field(default=None, ge=0)
    planned_panel_quantity: Optional[int] = Field(default=None, ge=0)
    target_delivery_date: Optional[str] = None
    target_completion_date: Optional[str] = None
    layers: Optional[int] = Field(default=None, ge=1)
    pcb_dimensions: Optional[PcbDimensions] = None
    panel_dimensions: Optional[PanelDimensions] = None
    material_specs: Optional[MaterialSpecifications] = None
    process_specs: Optional[ProcessSpecifications] = None
    raw_material_batches: Optional[list[RawMaterialBatchRef]] = None
    revision_reason: Optional[str] = None


class DrillSheetDraftUpdate(BaseModel):
    """Draft update request for the linked Drill Details Sheet."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)

    primary_drill: Optional[list[DrillToolEntry]] = None
    second_stage_drill: Optional[list[DrillToolEntry]] = None
    slot_drill: Optional[list[DrillToolEntry]] = None
    first_stage_route: Optional[list[DrillToolEntry]] = None
    slot_route: Optional[list[DrillToolEntry]] = None
    setup: Optional[DrillSetupSpecs] = None
    inspection: Optional[DrillInspectionInfo] = None


class ProcessSequenceDraftUpdate(BaseModel):
    """Draft update request for the linked Process Sequence Sheet."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)

    route_name: Optional[str] = None
    processes: Optional[list[ProcessExecutionEntry]] = None
    current_process_seq: Optional[int] = Field(default=None, ge=1)


class JobCardVersionInput(BaseModel):
    """Optimistic concurrency input for lifecycle transitions."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class JobCardReturnToDraftInput(BaseModel):
    """Request payload to return a Job Card from engineering_review to draft."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str


class JobCardRevisionCreateInput(BaseModel):
    """Request payload to create a controlled post-approval ECN revision."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    revision_reason: str


# ---------- Authorization dependencies ----------
# Permission checks run as dependencies so unauthenticated/unauthorized callers receive
# 401/403 before request-body validation can reveal schema details.

async def require_job_card_reader(user: dict = Depends(current_user)) -> dict:
    require_permission(user, "job_cards.read")
    return user


async def require_job_card_manager(user: dict = Depends(current_user)) -> dict:
    require_permission(user, "job_cards.manage")
    return user


# ---------- Error mapping ----------

async def _invoke_service(operation: str, awaitable: Any) -> Any:
    """Await a service call and map failures to safe HTTP responses.

    - ``JobCardServiceError`` (and all subclasses: not-found 404, duplicate 409, lock 409,
      identity-mismatch 422, validation 422) keeps its status and descriptive detail.
    - Other ``HTTPException`` instances raised by the audit/idempotency helpers
      (e.g. idempotency key misuse 409/422, audit persistence 503) pass through.
    - Database and unexpected errors are logged server-side and returned with a generic
      detail, so stack traces and driver messages never reach the client.
    """
    try:
        return await awaitable
    except JobCardServiceError:
        raise
    except HTTPException:
        raise
    except DuplicateKeyError:
        logger.warning("job card %s rejected by a database uniqueness constraint", operation)
        raise HTTPException(status_code=409, detail="job card package already exists") from None
    except PyMongoError:
        logger.exception("job card %s failed with a database error", operation)
        raise HTTPException(status_code=503, detail="job card storage is temporarily unavailable") from None
    except Exception:
        logger.exception("job card %s failed unexpectedly", operation)
        raise HTTPException(status_code=500, detail="job card request could not be completed") from None


def _reject_server_managed(section: str, values: Optional[dict[str, Any]], forbidden: frozenset[str]) -> None:
    if not values:
        return
    supplied = sorted(forbidden.intersection(values))
    if supplied:
        raise HTTPException(
            status_code=422,
            detail=f"{section} contains server-managed fields that cannot be supplied: {', '.join(supplied)}",
        )


# ---------- Endpoints ----------

@router.post("/job-cards", response_model=JobCardPackage, status_code=201)
async def create_job_card(
    body: JobCardPackageCreate,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Create a complete three-document Job Card package in ``draft`` status."""
    _reject_server_managed("job_card", body.job_card, MASTER_SERVER_MANAGED_FIELDS)
    _reject_server_managed("drill_sheet", body.drill_sheet, SHEET_SERVER_MANAGED_FIELDS)
    _reject_server_managed("process_sequence_sheet", body.process_sequence_sheet, SHEET_SERVER_MANAGED_FIELDS)

    return await _invoke_service(
        "create",
        create_job_card_package(
            company_id=cid,
            user=user,
            job_card=dict(body.job_card),
            drill_sheet=dict(body.drill_sheet) if body.drill_sheet is not None else None,
            process_sequence_sheet=(
                dict(body.process_sequence_sheet) if body.process_sequence_sheet is not None else None
            ),
            request=request,
            idempotency_key=idempotency_key,
        ),
    )


@router.get("/job-cards/{job_card_id}", response_model=JobCardPackage)
async def get_job_card(
    job_card_id: str,
    user: dict = Depends(require_job_card_reader),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Retrieve the identity-verified three-document package within the caller's tenant."""
    return await _invoke_service("retrieve", get_job_card_package(company_id=cid, job_card_id=job_card_id))


@router.patch("/job-cards/{job_card_id}", response_model=JobCard)
async def update_job_card(
    job_card_id: str,
    body: JobCardDraftUpdate,
    request: Request,
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCard:
    """Update permitted fields of a ``draft`` master Job Card with optimistic concurrency."""
    patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if not patch:
        raise HTTPException(status_code=422, detail="no updatable job card fields were supplied")

    return await _invoke_service(
        "update",
        update_draft_job_card(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            patch=patch,
            expected_version=body.expected_version,
            request=request,
        ),
    )


@router.patch("/job-cards/{job_card_id}/drill-sheet", response_model=DrillDetailsSheet)
async def update_drill_sheet(
    job_card_id: str,
    body: DrillSheetDraftUpdate,
    request: Request,
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> DrillDetailsSheet:
    """Update permitted fields of a ``draft`` Job Card's linked Drill Details Sheet."""
    patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if not patch:
        raise HTTPException(status_code=422, detail="no updatable drill sheet fields were supplied")

    return await _invoke_service(
        "update_drill_sheet",
        update_draft_drill_sheet(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            patch=patch,
            expected_version=body.expected_version,
            request=request,
        ),
    )


@router.patch("/job-cards/{job_card_id}/process-sequence", response_model=ProcessSequenceSheet)
async def update_process_sequence(
    job_card_id: str,
    body: ProcessSequenceDraftUpdate,
    request: Request,
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> ProcessSequenceSheet:
    """Update permitted fields of a ``draft`` Job Card's linked Process Sequence Sheet."""
    patch = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if not patch:
        raise HTTPException(status_code=422, detail="no updatable process sequence fields were supplied")

    return await _invoke_service(
        "update_process_sequence",
        update_draft_process_sequence_sheet(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            patch=patch,
            expected_version=body.expected_version,
            request=request,
        ),
    )


@router.post("/job-cards/{job_card_id}/submit-review", response_model=JobCardPackage)
async def submit_for_review(
    job_card_id: str,
    body: JobCardVersionInput,
    request: Request,
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Transition a ``draft`` Job Card package to ``engineering_review``."""
    return await _invoke_service(
        "submit_review",
        submit_job_card_for_review(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            expected_version=body.expected_version,
            request=request,
        ),
    )


@router.post("/job-cards/{job_card_id}/return-to-draft", response_model=JobCardPackage)
async def return_to_draft(
    job_card_id: str,
    body: JobCardReturnToDraftInput,
    request: Request,
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Return a Job Card from ``engineering_review`` to ``draft`` with a mandatory reason."""
    return await _invoke_service(
        "return_to_draft",
        return_job_card_to_draft(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            expected_version=body.expected_version,
            reason=body.reason,
            request=request,
        ),
    )


@router.post("/job-cards/{job_card_id}/approve", response_model=JobCardPackage)
async def approve(
    job_card_id: str,
    body: JobCardVersionInput,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Approve an ``engineering_review`` Job Card and lock its Drill Details Sheet."""
    return await _invoke_service(
        "approve",
        approve_job_card(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            request=request,
        ),
    )


@router.post("/job-cards/{job_card_id}/release", response_model=JobCardPackage)
async def release(
    job_card_id: str,
    body: JobCardVersionInput,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Release an ``approved`` Job Card package to production (``released_to_production``)."""
    return await _invoke_service(
        "release",
        release_job_card(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            expected_version=body.expected_version,
            idempotency_key=idempotency_key,
            request=request,
        ),
    )


@router.post("/job-cards/{job_card_id}/revisions", response_model=JobCardPackage, status_code=201)
async def create_revision(
    job_card_id: str,
    body: JobCardRevisionCreateInput,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    user: dict = Depends(require_job_card_manager),
    cid: str = Depends(get_company_id),
) -> JobCardPackage:
    """Create a controlled ECN revision (``revision + 1`` in ``draft``) from an ``approved`` Job Card."""
    return await _invoke_service(
        "create_revision",
        create_job_card_revision(
            company_id=cid,
            job_card_id=job_card_id,
            user=user,
            expected_version=body.expected_version,
            revision_reason=body.revision_reason,
            idempotency_key=idempotency_key,
            request=request,
        ),
    )
