"""Job Card domain models for FactoryOS.

This module implements the tri-document Job Card architecture:
1. Main Job Card (JobCard)
2. Drill Details Sheet (DrillDetailsSheet)
3. Process Sequence Sheet (ProcessSequenceSheet)

All three share required identity fields:
- job_card_id, job_card_no, order_id, order_no, part_number, revision, company_id.

Day 1.6 corrections applied (from Day 1.5.1 audit):
- Identity string fields enforce min_length=1 to prevent empty-string orphaned documents.
- JobCard revision > 1 requires a non-empty revision_reason (ISO 9001 traceability).
- revision_code is auto-synchronised to REV-{revision:02d} when left at "REV-01" on revision > 1.
- ipc_class defaults to None (was incorrectly hardcoded to "IPC-A-600 Class 2").
- ProcessSequenceSheet validates that process_seq values are unique across the route.
- ProcessSequenceSheet validates that current_process_seq references an existing sequence.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Annotated, Literal, Optional
from typing_extensions import Self

from pydantic import BaseModel, Field, StringConstraints, model_validator


def new_id() -> str:
    """Generate a string UUIDv4 for document identity."""
    return str(uuid.uuid4())


# ---------- Status and Type Definitions ----------

JobCardStatus = Literal[
    "draft",
    "engineering_review",
    "approved",
    "released_to_production",
    "superseded",
]

HoleType = Literal["PTH", "NPTH", "VIA"]

ProcessExecutionStatus = Literal[
    "waiting",
    "in_progress",
    "completed",
    "skipped",
    "on_hold",
]

STANDARD_PROCESS_CATALOGUE: tuple[str, ...] = (
    "PPC",
    "Sheet Cutting",
    "Edge Beveling & Corner Rounding",
    "Stacking & Pinning",
    "CNC Drilling",
    "Drilling Inspection",
    "Deburring",
    "Black Hole",
    "Lamination",
    "Exposing",
    "Developing & Inspection",
    "Electro Plating",
    "SES",
    "AOI / Inspection",
    "Legend CS",
    "Legend SS",
    "HAL",
    "Surface Finish Inspection",
    "V Groove",
    "CNC Routing",
    "Water Wash",
    "Electrical Testing",
    "Final Inspection",
    "Packing",
)


# ---------- Shared Identity Base ----------


# Type alias: a string that is stripped of surrounding whitespace and must not
# be empty after stripping. Used for all six shared Job Card identity fields to
# prevent whitespace-only or empty orphaned documents reaching MongoDB.
_NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class JobCardIdentityBase(BaseModel):
    """Shared identifiers required across all three linked Job Card documents.

    All string identity fields use _NonEmptyStr (strip_whitespace=True, min_length=1)
    to prevent empty strings AND whitespace-only strings from being accepted.
    Pydantic v2 requires Annotated[str, StringConstraints(...)] for string-level
    coercion; Field(strip_whitespace=...) is a Pydantic v1 pattern and is ignored
    in v2.
    """
    job_card_id: _NonEmptyStr
    job_card_no: _NonEmptyStr
    order_id: _NonEmptyStr
    order_no: _NonEmptyStr
    part_number: _NonEmptyStr
    revision: int = Field(default=1, ge=1)
    company_id: _NonEmptyStr


# ---------- Main Job Card Nested Models ----------

class PcbDimensions(BaseModel):
    """Single circuit board physical dimensions."""
    length_mm: Optional[float] = Field(default=None, ge=0)
    width_mm: Optional[float] = Field(default=None, ge=0)
    thickness_mm: Optional[float] = Field(default=None, ge=0)
    unit: str = "mm"
    raw_dimensions_text: Optional[str] = None


class PanelDimensions(BaseModel):
    """Production panel layout and dimensions."""
    length_mm: Optional[float] = Field(default=None, ge=0)
    width_mm: Optional[float] = Field(default=None, ge=0)
    ups_per_panel: Optional[int] = Field(default=None, ge=1)
    unit: str = "mm"
    raw_panel_text: Optional[str] = None


class MaterialSpecifications(BaseModel):
    """Base laminate, copper weight, and material specifications."""
    base_material: Optional[str] = None
    material_grade: Optional[str] = None
    copper_outer_oz: Optional[str] = None
    copper_inner_oz: Optional[str] = None
    laminate_manufacturer: Optional[str] = None
    dielectric_constant: Optional[str] = None
    raw_material_notes: Optional[str] = None


class ProcessSpecifications(BaseModel):
    """Finishing, solder mask, legend and IPC specifications."""
    solder_mask_color: Optional[str] = None
    solder_mask_type: Optional[str] = None
    legend_color_cs: Optional[str] = None
    legend_color_ss: Optional[str] = None
    surface_finish: Optional[str] = None
    min_track_width_mm: Optional[float] = Field(default=None, ge=0)
    min_track_spacing_mm: Optional[float] = Field(default=None, ge=0)
    min_hole_size_mm: Optional[float] = Field(default=None, ge=0)
    impedance_controlled: Optional[str] = None
    ipc_class: Optional[str] = None  # Must be set explicitly; no default assumed (see Day 1.5.1 audit)
    special_instructions: Optional[str] = None


class RawMaterialBatchRef(BaseModel):
    """Traceable raw material batch reference allocated to this Job Card."""
    item_id: Optional[str] = None
    item_sku: Optional[str] = None
    material_name: str
    batch_lot_no: str
    quantity_allocated: Optional[float] = Field(default=None, ge=0)
    unit: Optional[str] = None


class QaCompletionInfo(BaseModel):
    """Quality assurance sign-off and completion audit details."""
    final_inspector: Optional[str] = None
    inspection_date: Optional[str] = None
    passed_qty: Optional[int] = Field(default=None, ge=0)
    rejected_qty: Optional[int] = Field(default=None, ge=0)
    test_certificate_no: Optional[str] = None
    qa_status: Optional[str] = None
    qa_notes: Optional[str] = None
    signoff_by: Optional[str] = None
    signoff_at: Optional[str] = None


# ---------- Document 1: Main Job Card ----------

class JobCard(JobCardIdentityBase):
    """Main Job Card document defining technical, commercial, and physical specs."""
    id: str = Field(default_factory=new_id)
    job_card_id: Optional[str] = ""
    status: JobCardStatus = "draft"
    version: int = Field(default=1, ge=1)
    revision_code: str = "REV-01"
    revision_reason: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None
    superseded_by_revision_id: Optional[str] = None
    superseded_at: Optional[str] = None
    previous_revision_id: Optional[str] = None

    # Customer & Commercial References
    customer_id: _NonEmptyStr
    customer_name: _NonEmptyStr
    work_order_no: Optional[str] = None
    customer_po_no: Optional[str] = None

    # Quantities (PCB units vs Panel count clearly separated)
    order_quantity_pcb: Optional[int] = Field(default=None, ge=0)
    planned_panel_quantity: Optional[int] = Field(default=None, ge=0)

    # Milestones & Target Dates
    target_delivery_date: Optional[str] = None
    target_completion_date: Optional[str] = None

    # Physical, Material & Process Specs
    layers: Optional[int] = Field(default=None, ge=1)
    pcb_dimensions: Optional[PcbDimensions] = None
    panel_dimensions: Optional[PanelDimensions] = None
    material_specs: Optional[MaterialSpecifications] = None
    process_specs: Optional[ProcessSpecifications] = None
    raw_material_batches: list[RawMaterialBatchRef] = Field(default_factory=list)
    qa_completion: Optional[QaCompletionInfo] = None

    # Sub-Document Linking References
    drill_sheet_id: Optional[_NonEmptyStr] = None
    process_sequence_sheet_id: Optional[_NonEmptyStr] = None

    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    created_by: Optional[str] = None

    @model_validator(mode="after")
    def _validate_revision_and_sync(self) -> Self:
        """Synchronise job_card_id and enforce revision traceability rules.

        1. If job_card_id was omitted or explicitly set to empty string/None, default it to self.id.
           If explicitly supplied as whitespace-only, reject with ValueError (cannot be whitespace-only).
           If explicitly supplied as valid non-empty string, strip and preserve it.
        2. For revision > 1, a non-empty revision_reason is mandatory (ISO 9001 traceability).
        3. If revision_code was left as default (not explicitly provided in constructor),
           auto-synchronise it to "REV-{revision:02d}".
           If revision_code was explicitly supplied, validate that it is not whitespace-only
           and that its sequence number is consistent with revision. An explicitly supplied
           inconsistent revision code must be rejected (never silently overwritten).
        """
        # 1. Synchronise or validate job_card_id.
        if "job_card_id" in self.model_fields_set:
            raw_id = self.job_card_id
            if raw_id is None or raw_id == "":
                # Explicit empty/None: synchronize with self.id
                self.job_card_id = self.id
            else:
                stripped_id = raw_id.strip()
                if not stripped_id:
                    # Explicit non-empty whitespace-only input: invalid
                    raise ValueError(
                        "job_card_id cannot be whitespace-only; omit the field, "
                        "pass empty string, or supply a valid non-empty identifier."
                    )
                self.job_card_id = stripped_id
        else:
            # Missing / default: synchronize with self.id
            self.job_card_id = self.id

        # 2. Revision reason is required when revision > 1.
        if self.revision > 1:
            reason = (self.revision_reason or "").strip()
            if not reason:
                raise ValueError(
                    f"revision_reason is required when revision > 1 "
                    f"(current revision: {self.revision}). "
                    "Provide an Engineering Change Note (ECN) or change description."
                )

        # 3. Validate and synchronise revision_code.
        if "revision_code" not in self.model_fields_set:
            # Generated default: auto-synchronize to match revision number
            self.revision_code = f"REV-{self.revision:02d}"
        else:
            code = (self.revision_code or "").strip()
            if not code:
                raise ValueError("revision_code cannot be empty or whitespace-only")
            match = re.search(r"(\d+)", code)
            if match and int(match.group(1)) != self.revision:
                raise ValueError(
                    f"Explicitly supplied revision_code '{self.revision_code}' is inconsistent "
                    f"with revision number {self.revision}. Expected sequence matching revision {self.revision}."
                )
            self.revision_code = code

        return self


# ---------- Document 2: Drill Details Sheet ----------

class DrillToolEntry(BaseModel):
    """Single drill tool specification row."""
    tool_no: str
    drill_size_mm: Optional[float] = Field(default=None, ge=0)
    finish_size_mm: Optional[float] = Field(default=None, ge=0)
    hole_type: Optional[HoleType] = None
    hole_count_per_board: Optional[int] = Field(default=None, ge=0)
    hole_count_per_panel: Optional[int] = Field(default=None, ge=0)
    hit_count: Optional[int] = Field(default=None, ge=0)
    tolerance_mm: Optional[str] = None
    remarks: Optional[str] = None


class DrillSetupSpecs(BaseModel):
    """Optional CNC drilling machine setup parameters."""
    stack_height: Optional[int] = Field(default=None, ge=1)
    entry_material: Optional[str] = None
    backup_material: Optional[str] = None
    drill_program_name: Optional[str] = None
    spindle_speed_rpm: Optional[int] = Field(default=None, ge=0)
    feed_rate: Optional[float] = Field(default=None, ge=0)


class DrillInspectionInfo(BaseModel):
    """First-article hole and drill inspection sign-off."""
    first_article_inspected: Optional[bool] = None
    inspector: Optional[str] = None
    inspected_at: Optional[str] = None
    hole_registration_passed: Optional[bool] = None
    burr_check_passed: Optional[bool] = None
    remarks: Optional[str] = None


class DrillDetailsSheet(JobCardIdentityBase):
    """Drill Details Sheet preserving the 5 authoritative factory sections."""
    id: str = Field(default_factory=new_id)
    version: int = Field(default=1, ge=1)

    # The 5 Original Authoritative Sections
    primary_drill: list[DrillToolEntry] = Field(default_factory=list)
    second_stage_drill: list[DrillToolEntry] = Field(default_factory=list)
    slot_drill: list[DrillToolEntry] = Field(default_factory=list)
    first_stage_route: list[DrillToolEntry] = Field(default_factory=list)
    slot_route: list[DrillToolEntry] = Field(default_factory=list)

    # Optional extensions
    setup: Optional[DrillSetupSpecs] = None
    inspection: Optional[DrillInspectionInfo] = None
    is_locked: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# ---------- Document 3: Process Sequence Sheet ----------

class ProcessExecutionEntry(BaseModel):
    """Single process execution stage on the active Job Card traveler."""
    process_seq: int = Field(ge=1)
    process_name: str
    process_code: Optional[str] = None
    department: Optional[str] = None
    assigned_supervisor: Optional[str] = None
    assigned_machine_id: Optional[str] = None
    is_active: bool = True
    status: ProcessExecutionStatus = "waiting"

    # Confirmed factory tracking fields
    date_in: Optional[str] = None
    date_out: Optional[str] = None
    ok_qty: Optional[int] = Field(default=None, ge=0)
    rej_qty: Optional[int] = Field(default=None, ge=0)
    rejection_code: Optional[str] = None
    operator: Optional[str] = None

    # Optional execution & flow tracking
    input_qty: Optional[int] = Field(default=None, ge=0)
    transferred_to_next: Optional[int] = Field(default=None, ge=0)
    rework_qty: Optional[int] = Field(default=None, ge=0)
    scrap_qty: Optional[int] = Field(default=None, ge=0)
    notes: Optional[str] = None
    supervisor_signoff: Optional[str] = None
    signoff_at: Optional[str] = None
    correction_history: list[dict[str, Any]] = Field(default_factory=list)


class ProcessSequenceSheet(JobCardIdentityBase):
    """Process Sequence Sheet carrying the approved route traveler."""
    id: str = Field(default_factory=new_id)
    version: int = Field(default=1, ge=1)
    route_name: str = "Standard 24-Process PCB Route"
    processes: list[ProcessExecutionEntry] = Field(default_factory=list)
    current_process_seq: Optional[int] = Field(default=None, ge=1)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def _validate_process_route(self) -> Self:
        """Enforce structural integrity of the process route.

        Empty process list:
          - current_process_seq must be None. A sheet with no route has no
            current position to point to; a non-null pointer would be a dangling
            reference with no declared sequence to validate it against.

        Non-empty process list:
          1. process_seq values must be unique across all entries.
          2. current_process_seq, when set, must reference an ACTIVE sequence
             that exists in the route. Inactive or non-existent stages are rejected.
        """
        if not self.processes:
            # No route declared yet. current_process_seq must be None.
            if self.current_process_seq is not None:
                raise ValueError(
                    f"current_process_seq cannot be set to {self.current_process_seq} "
                    "when processes is empty. Set current_process_seq=None when no "
                    "process route has been assigned."
                )
        else:
            # Route is present — enforce uniqueness and pointer validity.
            seqs = [p.process_seq for p in self.processes]
            if len(seqs) != len(set(seqs)):
                duplicates = sorted({s for s in seqs if seqs.count(s) > 1})
                raise ValueError(
                    f"Duplicate process_seq numbers found in process route: {duplicates}. "
                    "Each stage must have a unique sequence number."
                )

            if self.current_process_seq is not None:
                declared_seqs = {p.process_seq for p in self.processes}
                if self.current_process_seq not in declared_seqs:
                    raise ValueError(
                        f"current_process_seq {self.current_process_seq} does not reference "
                        f"any declared process sequence. Declared sequences: {sorted(declared_seqs)}"
                    )
                active_seqs = {p.process_seq for p in self.processes if p.is_active}
                if self.current_process_seq not in active_seqs:
                    raise ValueError(
                        f"current_process_seq {self.current_process_seq} references an inactive "
                        f"process sequence. current_process_seq must refer to an active process in the route."
                    )

        return self


def build_default_process_route() -> list[ProcessExecutionEntry]:
    """Generate the standard 24-process sequence route with default sequence numbers."""
    return [
        ProcessExecutionEntry(
            process_seq=idx + 1,
            process_name=name,
            is_active=True,
            status="waiting",
        )
        for idx, name in enumerate(STANDARD_PROCESS_CATALOGUE)
    ]


# ---------- Scrap Boundary Placeholder ----------

class ScrapBoundaryPlaceholder(BaseModel):
    """Placeholder type for future Phase 2G / Scrap Process #25 integration.

    Not an active operational workflow. Rejections logged in ProcessExecutionEntry
    are NOT automatically scrap; they must undergo disposition review before
    generating scrap records.
    """
    scrap_record_id: Optional[str] = None
    scrap_reason_code: Optional[str] = None
    condemned_by: Optional[str] = None
    condemned_at: Optional[str] = None
    disposition_status: Literal["pending_review", "rework", "scrap_confirmed"] = "pending_review"
