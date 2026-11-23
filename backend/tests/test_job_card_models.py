"""Focused model validation tests for Job Card domain models.

Executed in the isolated mock test lane (FACTORYOS_TEST_LANE=mock) without database connection.
"""

import pytest
from pydantic import ValidationError

from models.job_cards import (
    STANDARD_PROCESS_CATALOGUE,
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
    ScrapBoundaryPlaceholder,
    build_default_process_route,
)


def _shared_identity():
    return {
        "job_card_id": "jc-uuid-001",
        "job_card_no": "JC-2026-0001",
        "order_id": "ord-uuid-001",
        "order_no": "SO-2026-0042",
        "part_number": "PCB-INV-REV1",
        "revision": 1,
        "company_id": "test-company",
    }


# 1. Main Job Card creation
def test_main_job_card_creation_minimal_and_full():
    shared = _shared_identity()
    jc = JobCard(
        **shared,
        customer_id="cust-001",
        customer_name="Apex Precision Customer",
    )
    assert jc.job_card_id == "jc-uuid-001"
    assert jc.job_card_no == "JC-2026-0001"
    assert jc.status == "draft"
    assert jc.revision == 1
    assert jc.revision_code == "REV-01"

    # Full Main Job Card with nested specs
    full_jc = JobCard(
        **shared,
        customer_id="cust-001",
        customer_name="Apex Precision Customer",
        work_order_no="WO-2026-0089",
        customer_po_no="PO-CUST-992",
        order_quantity_pcb=500,
        planned_panel_quantity=100,
        target_delivery_date="2026-12-15",
        layers=4,
        pcb_dimensions=PcbDimensions(length_mm=100.5, width_mm=80.0, thickness_mm=1.6),
        panel_dimensions=PanelDimensions(length_mm=400.0, width_mm=300.0, ups_per_panel=5),
        material_specs=MaterialSpecifications(
            base_material="FR4",
            material_grade="TG-170",
            copper_outer_oz="1 oz",
            copper_inner_oz="0.5 oz",
        ),
        process_specs=ProcessSpecifications(
            solder_mask_color="Green",
            legend_color_cs="White",
            legend_color_ss="None",
            surface_finish="HAL Lead-Free",
            min_track_width_mm=0.15,
            min_track_spacing_mm=0.15,
            min_hole_size_mm=0.3,
        ),
        raw_material_batches=[
            RawMaterialBatchRef(
                material_name="FR4 Laminate Sheet",
                batch_lot_no="LOT-FR4-2026-09",
                quantity_allocated=20,
            )
        ],
    )
    assert full_jc.order_quantity_pcb == 500
    assert full_jc.planned_panel_quantity == 100
    assert full_jc.layers == 4
    assert full_jc.pcb_dimensions.length_mm == 100.5
    assert full_jc.panel_dimensions.ups_per_panel == 5
    assert full_jc.material_specs.base_material == "FR4"
    assert full_jc.process_specs.surface_finish == "HAL Lead-Free"
    assert len(full_jc.raw_material_batches) == 1


# 2. Drill Details Sheet creation
def test_drill_details_sheet_creation_and_sections():
    shared = _shared_identity()
    tool_primary = DrillToolEntry(
        tool_no="T01",
        drill_size_mm=0.8,
        finish_size_mm=0.7,
        hole_type="PTH",
        hole_count_per_board=42,
        hole_count_per_panel=210,
        hit_count=70,  # CAM-supplied hit count, not auto-calculated
    )
    tool_slot = DrillToolEntry(
        tool_no="T02",
        drill_size_mm=2.0,
        finish_size_mm=2.0,
        hole_type="NPTH",
        hole_count_per_board=4,
    )
    drill_sheet = DrillDetailsSheet(
        **shared,
        primary_drill=[tool_primary],
        slot_drill=[tool_slot],
        setup=DrillSetupSpecs(
            stack_height=3,
            entry_material="Aluminum Foil 0.15mm",
            backup_material="Phenolic Board 1.5mm",
            drill_program_name="DRILL_REV1.DRL",
        ),
        inspection=DrillInspectionInfo(
            first_article_inspected=True,
            inspector="Drill Lead",
            burr_check_passed=True,
        ),
    )
    assert len(drill_sheet.primary_drill) == 1
    assert drill_sheet.primary_drill[0].tool_no == "T01"
    assert drill_sheet.primary_drill[0].hole_type == "PTH"
    assert len(drill_sheet.slot_drill) == 1
    assert len(drill_sheet.second_stage_drill) == 0
    assert len(drill_sheet.first_stage_route) == 0
    assert len(drill_sheet.slot_route) == 0
    assert drill_sheet.setup.stack_height == 3
    assert drill_sheet.inspection.first_article_inspected is True


# 3. Process Sequence Sheet creation
def test_process_sequence_sheet_creation():
    shared = _shared_identity()
    default_route = build_default_process_route()
    sheet = ProcessSequenceSheet(
        **shared,
        processes=default_route,
        current_process_seq=1,  # explicitly set; default is None for empty-route sheets
    )
    assert len(sheet.processes) == 24
    assert sheet.processes[0].process_name == "PPC"
    assert sheet.processes[23].process_name == "Packing"
    assert sheet.current_process_seq == 1


# 4. Shared identity consistency
def test_shared_identity_consistency():
    shared = _shared_identity()
    jc = JobCard(**shared, customer_id="c1", customer_name="Customer 1")
    drill = DrillDetailsSheet(**shared)
    proc = ProcessSequenceSheet(**shared)

    for doc in (jc, drill, proc):
        assert doc.job_card_id == shared["job_card_id"]
        assert doc.job_card_no == shared["job_card_no"]
        assert doc.order_id == shared["order_id"]
        assert doc.order_no == shared["order_no"]
        assert doc.part_number == shared["part_number"]
        assert doc.revision == shared["revision"]
        assert doc.company_id == shared["company_id"]


# 5. Exact 24-process catalogue
def test_exact_24_process_catalogue():
    expected_24 = (
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
    assert STANDARD_PROCESS_CATALOGUE == expected_24
    route = build_default_process_route()
    assert len(route) == 24
    for idx, (expected_name, entry) in enumerate(zip(expected_24, route)):
        assert entry.process_seq == idx + 1
        assert entry.process_name == expected_name
        assert entry.is_active is True
        assert entry.status == "waiting"


# 6. Configurable process routes
def test_configurable_process_routes():
    shared = _shared_identity()
    # Product skips Black Hole and Electro Plating (single-sided board), reorders Inspection
    custom_processes = [
        ProcessExecutionEntry(process_seq=1, process_name="PPC", is_active=True),
        ProcessExecutionEntry(process_seq=2, process_name="Sheet Cutting", is_active=True),
        ProcessExecutionEntry(process_seq=3, process_name="Black Hole", is_active=False),  # skipped
        ProcessExecutionEntry(process_seq=4, process_name="Electro Plating", is_active=False),  # skipped
        ProcessExecutionEntry(process_seq=5, process_name="Final Inspection", is_active=True),
        ProcessExecutionEntry(process_seq=6, process_name="Packing", is_active=True),
    ]
    sheet = ProcessSequenceSheet(
        **shared,
        route_name="Single-Sided Short Route",
        processes=custom_processes,
    )
    assert len(sheet.processes) == 6
    assert [p.process_name for p in sheet.processes if p.is_active] == [
        "PPC", "Sheet Cutting", "Final Inspection", "Packing"
    ]


# 7. Optional department assignment
def test_optional_department_assignment():
    entry_without_dept = ProcessExecutionEntry(
        process_seq=1,
        process_name="PPC",
        department=None,
        assigned_supervisor=None,
    )
    assert entry_without_dept.department is None
    assert entry_without_dept.assigned_supervisor is None

    entry_with_dept = ProcessExecutionEntry(
        process_seq=5,
        process_name="CNC Drilling",
        department="Mechanical",
        assigned_supervisor="Sarah Drill",
        assigned_machine_id="DRL-01",
    )
    assert entry_with_dept.department == "Mechanical"
    assert entry_with_dept.assigned_supervisor == "Sarah Drill"
    assert entry_with_dept.assigned_machine_id == "DRL-01"


# 8. Quantity validation
def test_quantity_validation():
    # Non-negative quantities valid
    entry = ProcessExecutionEntry(
        process_seq=5,
        process_name="CNC Drilling",
        input_qty=100,
        ok_qty=95,
        rej_qty=5,
        transferred_to_next=95,
        rework_qty=0,
        scrap_qty=0,
    )
    assert entry.ok_qty == 95
    assert entry.rej_qty == 5

    # Negative quantities rejected
    with pytest.raises(ValidationError):
        ProcessExecutionEntry(process_seq=1, process_name="PPC", ok_qty=-1)

    with pytest.raises(ValidationError):
        ProcessExecutionEntry(process_seq=1, process_name="PPC", rej_qty=-5)

    with pytest.raises(ValidationError):
        DrillToolEntry(tool_no="T01", drill_size_mm=-0.5)

    with pytest.raises(ValidationError):
        JobCard(
            **_shared_identity(),
            customer_id="c1",
            customer_name="C1",
            order_quantity_pcb=-10,
        )


# 9. Null, blank and N/A preservation
def test_null_blank_and_na_preservation():
    shared = _shared_identity()
    jc = JobCard(
        **shared,
        customer_id="cust-001",
        customer_name="Cust 1",
        work_order_no=None,  # Unknown
        customer_po_no="",   # Blank
        material_specs=MaterialSpecifications(
            base_material="FR4",
            copper_inner_oz="N/A",  # Not Applicable for 2-layer board
            raw_material_notes="",
        ),
        process_specs=ProcessSpecifications(
            legend_color_ss="N/A",  # No bottom legend
            special_instructions=None,
        ),
    )
    assert jc.work_order_no is None
    assert jc.customer_po_no == ""
    assert jc.material_specs.copper_inner_oz == "N/A"
    assert jc.material_specs.raw_material_notes == ""
    assert jc.process_specs.legend_color_ss == "N/A"
    assert jc.process_specs.special_instructions is None


# 10. Mutable defaults do not leak between instances
def test_mutable_defaults_do_not_leak():
    shared = _shared_identity()
    drill_1 = DrillDetailsSheet(**shared)
    drill_2 = DrillDetailsSheet(**shared)

    drill_1.primary_drill.append(DrillToolEntry(tool_no="T01", drill_size_mm=1.0))
    assert len(drill_1.primary_drill) == 1
    assert len(drill_2.primary_drill) == 0

    proc_1 = ProcessSequenceSheet(**shared)
    proc_2 = ProcessSequenceSheet(**shared)
    proc_1.processes.append(ProcessExecutionEntry(process_seq=1, process_name="PPC"))
    assert len(proc_1.processes) == 1
    assert len(proc_2.processes) == 0


# 11. Historical revision references
def test_historical_revision_references():
    shared_v1 = _shared_identity()
    shared_v1["revision"] = 1
    jc_v1 = JobCard(
        **shared_v1,
        customer_id="c1",
        customer_name="C1",
        status="superseded",
        revision_code="REV-01",
        superseded_by_revision_id="jc-uuid-v2",
        superseded_at="2026-11-20T10:00:00Z",
    )

    shared_v2 = _shared_identity()
    shared_v2["job_card_id"] = "jc-uuid-v2"
    shared_v2["revision"] = 2
    jc_v2 = JobCard(
        **shared_v2,
        customer_id="c1",
        customer_name="C1",
        status="approved",
        revision_code="REV-02",
        previous_revision_id=jc_v1.id,
        revision_reason="CAM drill hole diameter tolerance updated per customer ECN-102",
        approved_by="Lead CAM Engineer",
        approved_at="2026-11-20T10:05:00Z",
    )
    assert jc_v1.status == "superseded"
    assert jc_v1.superseded_by_revision_id == "jc-uuid-v2"
    assert jc_v2.revision == 2
    assert jc_v2.revision_code == "REV-02"
    assert jc_v2.previous_revision_id == jc_v1.id
    assert "ECN-102" in jc_v2.revision_reason
    assert jc_v2.approved_by == "Lead CAM Engineer"


# 12. Invalid input rejection
def test_invalid_input_rejection():
    # Missing required shared identifiers
    with pytest.raises(ValidationError):
        JobCard(job_card_no="JC-01")  # missing order_id, part_number, customer_id, etc.

    with pytest.raises(ValidationError):
        DrillDetailsSheet(job_card_no="JC-01")

    with pytest.raises(ValidationError):
        ProcessSequenceSheet(job_card_no="JC-01")

    # Invalid status string
    shared = _shared_identity()
    with pytest.raises(ValidationError):
        JobCard(**shared, customer_id="c1", customer_name="C1", status="invalid_status")

    # Invalid hole type
    with pytest.raises(ValidationError):
        DrillToolEntry(tool_no="T01", hole_type="INVALID_TYPE")

    # Invalid revision (< 1)
    shared_invalid_rev = _shared_identity()
    shared_invalid_rev["revision"] = 0
    with pytest.raises(ValidationError):
        JobCard(**shared_invalid_rev, customer_id="c1", customer_name="C1")


# Scrap boundary placeholder verification
def test_scrap_boundary_placeholder():
    placeholder = ScrapBoundaryPlaceholder(
        scrap_record_id="scrap-001",
        scrap_reason_code="OVER_ETCH",
        condemned_by="QC Inspector",
        disposition_status="scrap_confirmed",
    )
    assert placeholder.disposition_status == "scrap_confirmed"
    assert placeholder.scrap_reason_code == "OVER_ETCH"


# ============================================================
# DAY 1.6 — Validation Corrections Test Suite
# Tests added per Day 1.5.1 architecture audit recommendations.
# ============================================================


# 14. Revision > 1 requires a non-empty revision_reason (ISO 9001 traceability)
def test_revision_requires_reason_when_greater_than_one():
    """JobCard with revision > 1 must have a non-empty revision_reason."""
    shared = _shared_identity()
    shared["revision"] = 2

    # Must fail: revision = 2, no reason provided
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared, customer_id="c1", customer_name="C1", revision_code="REV-02")
    assert "revision_reason is required" in str(exc_info.value)

    # Must fail: revision = 2, whitespace-only reason
    with pytest.raises(ValidationError):
        JobCard(**shared, customer_id="c1", customer_name="C1",
                revision_code="REV-02", revision_reason="   ")

    # Must succeed: revision = 2 with a valid reason
    jc = JobCard(**shared, customer_id="c1", customer_name="C1",
                 revision_code="REV-02",
                 revision_reason="ECN-200: Drill tolerance updated per customer specification")
    assert jc.revision == 2
    assert jc.revision_code == "REV-02"

    # Revision 1 must succeed without any reason (initial release)
    shared_v1 = _shared_identity()
    shared_v1["revision"] = 1
    jc_v1 = JobCard(**shared_v1, customer_id="c1", customer_name="C1")
    assert jc_v1.revision == 1
    assert jc_v1.revision_reason is None


# 15. revision_code is auto-formatted when left at default "REV-01" on revision > 1
def test_revision_code_auto_formats_when_left_at_default():
    """When revision_code is not explicitly set, it syncs to REV-{revision:02d}."""
    shared = _shared_identity()

    # Revision 2 without explicit revision_code — auto-formatted to REV-02
    shared["revision"] = 2
    jc2 = JobCard(**shared, customer_id="c1", customer_name="C1",
                  revision_reason="ECN-200: Updated drill specification")
    assert jc2.revision_code == "REV-02"

    # Revision 5 without explicit revision_code — auto-formatted to REV-05
    shared5 = _shared_identity()
    shared5["revision"] = 5
    jc5 = JobCard(**shared5, customer_id="c1", customer_name="C1",
                  revision_reason="ECN-500: Fifth revision after CAM re-route")
    assert jc5.revision_code == "REV-05"

    # Explicitly provided revision_code is preserved as-is (custom format)
    shared_custom = _shared_identity()
    shared_custom["revision"] = 3
    jc_custom = JobCard(**shared_custom, customer_id="c1", customer_name="C1",
                        revision_code="REV-03-HOTFIX",
                        revision_reason="ECN-301: Emergency HAL thickness correction")
    assert jc_custom.revision_code == "REV-03-HOTFIX"


# 16. Identity string fields reject empty strings (min_length=1)
def test_identity_fields_reject_empty_strings():
    """All six non-revision identity string fields must reject empty strings."""
    base = {
        "job_card_id": "jc-001",
        "job_card_no": "JC-2026-0001",
        "order_id": "ord-001",
        "order_no": "SO-001",
        "part_number": "PCB-001",
        "revision": 1,
        "company_id": "co-001",
    }

    empty_field_cases = [
        "job_card_id",
        "job_card_no",
        "order_id",
        "order_no",
        "part_number",
        "company_id",
    ]

    for field_name in empty_field_cases:
        invalid = dict(base)
        invalid[field_name] = ""
        with pytest.raises(ValidationError, match="String should have at least 1 character"):
            DrillDetailsSheet(**invalid)
        with pytest.raises(ValidationError, match="String should have at least 1 character"):
            ProcessSequenceSheet(**invalid)


# 17. ipc_class defaults to None (not "IPC-A-600 Class 2")
def test_ipc_class_defaults_to_none():
    """ProcessSpecifications.ipc_class must default to None, not a hardcoded assumption."""
    specs_default = ProcessSpecifications()
    assert specs_default.ipc_class is None, (
        "ipc_class must default to None so the CAM team explicitly sets the class. "
        f"Got: {specs_default.ipc_class!r}"
    )

    # Confirm it can be set explicitly
    specs_set = ProcessSpecifications(ipc_class="IPC-A-600 Class 3")
    assert specs_set.ipc_class == "IPC-A-600 Class 3"

    # Confirm it can be cleared to None explicitly
    specs_none = ProcessSpecifications(ipc_class=None)
    assert specs_none.ipc_class is None


# 18. ProcessSequenceSheet rejects duplicate process_seq values
def test_process_sequence_sheet_rejects_duplicate_seq_numbers():
    """process_seq values must be unique across the route."""
    shared = _shared_identity()

    # Must fail: two entries share process_seq=1
    with pytest.raises(ValidationError) as exc_info:
        ProcessSequenceSheet(
            **shared,
            processes=[
                ProcessExecutionEntry(process_seq=1, process_name="PPC"),
                ProcessExecutionEntry(process_seq=1, process_name="CNC Drilling"),
            ]
        )
    assert "Duplicate process_seq" in str(exc_info.value)

    # Must fail: three entries, two share seq=5
    with pytest.raises(ValidationError):
        ProcessSequenceSheet(
            **shared,
            processes=[
                ProcessExecutionEntry(process_seq=1, process_name="PPC"),
                ProcessExecutionEntry(process_seq=5, process_name="CNC Drilling"),
                ProcessExecutionEntry(process_seq=5, process_name="Deburring"),
                ProcessExecutionEntry(process_seq=10, process_name="Packing"),
            ]
        )

    # Must succeed: all seq numbers unique (non-contiguous is allowed for sparse routes)
    sheet = ProcessSequenceSheet(
        **shared,
        processes=[
            ProcessExecutionEntry(process_seq=1, process_name="PPC"),
            ProcessExecutionEntry(process_seq=3, process_name="CNC Drilling"),
            ProcessExecutionEntry(process_seq=10, process_name="Final Inspection"),
            ProcessExecutionEntry(process_seq=11, process_name="Packing"),
        ],
        current_process_seq=1,
    )
    assert len(sheet.processes) == 4


# 19. ProcessSequenceSheet rejects current_process_seq that references a non-existent seq
def test_process_sequence_sheet_rejects_invalid_current_seq():
    """current_process_seq must reference a declared process_seq in the route."""
    shared = _shared_identity()

    # Must fail: current_process_seq=99 is not in [1, 2]
    with pytest.raises(ValidationError) as exc_info:
        ProcessSequenceSheet(
            **shared,
            processes=[
                ProcessExecutionEntry(process_seq=1, process_name="PPC"),
                ProcessExecutionEntry(process_seq=2, process_name="Sheet Cutting"),
            ],
            current_process_seq=99,
        )
    assert "does not reference" in str(exc_info.value)

    # Must fail: current_process_seq points to an inactive stage (is_active=False)
    with pytest.raises(ValidationError) as exc_info:
        ProcessSequenceSheet(
            **shared,
            processes=[
                ProcessExecutionEntry(process_seq=1, process_name="PPC", is_active=True),
                ProcessExecutionEntry(process_seq=2, process_name="Black Hole", is_active=False),
                ProcessExecutionEntry(process_seq=3, process_name="Packing", is_active=True),
            ],
            current_process_seq=2,  # points to an inactive stage — must be rejected
        )
    assert "references an inactive process sequence" in str(exc_info.value)

    # Must succeed: current_process_seq points to an active stage
    sheet = ProcessSequenceSheet(
        **shared,
        processes=[
            ProcessExecutionEntry(process_seq=1, process_name="PPC", is_active=True),
            ProcessExecutionEntry(process_seq=2, process_name="Black Hole", is_active=False),
            ProcessExecutionEntry(process_seq=3, process_name="Packing", is_active=True),
        ],
        current_process_seq=1,  # points to an active stage
    )
    assert sheet.current_process_seq == 1

    # Must succeed: empty processes list (no route yet) — current_process_seq must be None
    empty_sheet = ProcessSequenceSheet(**shared)
    assert empty_sheet.current_process_seq is None


# ============================================================
# DAY 1.6 FOLLOW-UP — Validation Gap Regression Tests
# ============================================================


# 20. Identity fields reject whitespace-only strings
def test_identity_fields_reject_whitespace_only_strings():
    """All six identity string fields must reject whitespace-only values.

    Field(min_length=1) alone does not reject '   ' because len('   ') > 1.
    strip_whitespace=True collapses it to '' before the min_length check fires.
    This test ensures that protection is in place and works for every field.
    """
    base = {
        "job_card_id": "jc-001",
        "job_card_no": "JC-2026-0001",
        "order_id": "ord-001",
        "order_no": "SO-001",
        "part_number": "PCB-001",
        "revision": 1,
        "company_id": "co-001",
    }

    whitespace_variants = ["   ", "\t", "\n", "  \t  ", "\r\n"]
    identity_fields = [
        "job_card_id",
        "job_card_no",
        "order_id",
        "order_no",
        "part_number",
        "company_id",
    ]

    for field_name in identity_fields:
        for ws in whitespace_variants:
            invalid = dict(base)
            invalid[field_name] = ws
            with pytest.raises(ValidationError, match="String should have at least 1 character"):
                DrillDetailsSheet(**invalid)
            with pytest.raises(ValidationError, match="String should have at least 1 character"):
                ProcessSequenceSheet(**invalid)
            if field_name == "job_card_id":
                with pytest.raises(ValidationError, match="job_card_id cannot be whitespace-only"):
                    JobCard(**invalid, customer_id="c1", customer_name="C1")
            else:
                with pytest.raises(ValidationError, match="String should have at least 1 character"):
                    JobCard(**invalid, customer_id="c1", customer_name="C1")

    # Confirm that leading/trailing whitespace on valid values is stripped cleanly
    # (strip_whitespace=True normalises the stored value)
    stripped = dict(base)
    stripped["job_card_no"] = "  JC-2026-0001  "
    drill = DrillDetailsSheet(**stripped)
    assert drill.job_card_no == "JC-2026-0001"
    jc = JobCard(**stripped, customer_id="c1", customer_name="C1")
    assert jc.job_card_no == "JC-2026-0001"


# 21. ProcessSequenceSheet empty-process / current_process_seq matrix
def test_process_sequence_sheet_empty_process_current_seq_matrix():
    """Exhaustive matrix for the four required behaviours.

    State                                      | Expected
    -------------------------------------------|----------
    Empty processes + current_process_seq=None  | VALID
    Empty processes + current_process_seq=1     | INVALID
    Non-empty + valid current seq               | VALID
    Non-empty + nonexistent current seq         | INVALID
    Non-empty + duplicate process seqs          | INVALID
    Non-empty + unique non-contiguous seqs      | VALID
    """
    shared = _shared_identity()

    # 1. Empty processes + current_process_seq=None → VALID
    sheet = ProcessSequenceSheet(**shared, processes=[], current_process_seq=None)
    assert sheet.current_process_seq is None
    assert sheet.processes == []

    # 2. Empty processes + current_process_seq=1 → INVALID
    with pytest.raises(ValidationError) as exc_info:
        ProcessSequenceSheet(**shared, processes=[], current_process_seq=1)
    assert "current_process_seq cannot be set" in str(exc_info.value)

    # Also invalid for any non-None value when no processes declared
    with pytest.raises(ValidationError):
        ProcessSequenceSheet(**shared, processes=[], current_process_seq=5)

    # 3. Non-empty + valid current seq → VALID
    valid_procs = [
        ProcessExecutionEntry(process_seq=1, process_name="PPC"),
        ProcessExecutionEntry(process_seq=2, process_name="Sheet Cutting"),
        ProcessExecutionEntry(process_seq=3, process_name="Packing"),
    ]
    sheet = ProcessSequenceSheet(**shared, processes=valid_procs, current_process_seq=2)
    assert sheet.current_process_seq == 2

    # Also valid when current_process_seq is None on a non-empty sheet
    sheet_no_ptr = ProcessSequenceSheet(**shared, processes=valid_procs, current_process_seq=None)
    assert sheet_no_ptr.current_process_seq is None

    # 4. Non-empty + nonexistent current seq → INVALID
    with pytest.raises(ValidationError) as exc_info:
        ProcessSequenceSheet(**shared, processes=valid_procs, current_process_seq=99)
    assert "does not reference" in str(exc_info.value)

    # 5. Duplicate process sequences → INVALID
    with pytest.raises(ValidationError) as exc_info:
        ProcessSequenceSheet(
            **shared,
            processes=[
                ProcessExecutionEntry(process_seq=1, process_name="PPC"),
                ProcessExecutionEntry(process_seq=1, process_name="Deburring"),
            ],
        )
    assert "Duplicate process_seq" in str(exc_info.value)

    # 6. Unique non-contiguous seqs → VALID (sparse routes are permitted)
    sparse = ProcessSequenceSheet(
        **shared,
        processes=[
            ProcessExecutionEntry(process_seq=1, process_name="PPC"),
            ProcessExecutionEntry(process_seq=5, process_name="CNC Drilling"),
            ProcessExecutionEntry(process_seq=22, process_name="Electrical Testing"),
            ProcessExecutionEntry(process_seq=24, process_name="Packing"),
        ],
        current_process_seq=5,
    )
    assert len(sparse.processes) == 4
    assert sparse.current_process_seq == 5


# ============================================================
# DAY 1.6.1 — Targeted Corrections Test Suite
# ============================================================


# 22. JobCard.job_card_id validation and synchronization
def test_job_card_id_validation_and_sync():
    """Verify JobCard.job_card_id handling across all input variants:

    1. Whitespace-only values -> rejected with ValidationError (cannot pass validation).
    2. Empty string "" -> synchronizes with self.id.
    3. Omitted/default -> synchronizes with self.id.
    4. Explicit None -> synchronizes with self.id.
    5. Valid explicit ID -> preserved as supplied.
    6. Valid explicit ID with surrounding whitespace -> normalized (stripped).
    """
    shared = _shared_identity()

    # 1. Whitespace-only job_card_id rejected
    for ws in ["   ", "\t", "\n", "  \t  "]:
        invalid = dict(shared)
        invalid["job_card_id"] = ws
        with pytest.raises(ValidationError, match="job_card_id cannot be whitespace-only"):
            JobCard(**invalid, customer_id="c1", customer_name="C1")

    # 2. Explicit empty string "" -> synchronizes with self.id
    empty_id_data = dict(shared)
    empty_id_data["job_card_id"] = ""
    jc_empty = JobCard(**empty_id_data, customer_id="c1", customer_name="C1")
    assert jc_empty.job_card_id == jc_empty.id
    assert len(jc_empty.job_card_id) > 0

    # 3. Omitted / default job_card_id -> synchronizes with self.id
    omitted_data = {k: v for k, v in shared.items() if k != "job_card_id"}
    jc_omitted = JobCard(**omitted_data, customer_id="c1", customer_name="C1")
    assert jc_omitted.job_card_id == jc_omitted.id
    assert len(jc_omitted.job_card_id) > 0

    # 4. Explicit None -> synchronizes with self.id
    none_data = dict(shared)
    none_data["job_card_id"] = None
    jc_none = JobCard(**none_data, customer_id="c1", customer_name="C1")
    assert jc_none.job_card_id == jc_none.id

    # 5. Valid explicit ID -> preserved
    explicit_data = dict(shared)
    explicit_data["job_card_id"] = "JC-CUSTOM-2026-999"
    jc_explicit = JobCard(**explicit_data, customer_id="c1", customer_name="C1")
    assert jc_explicit.job_card_id == "JC-CUSTOM-2026-999"
    assert jc_explicit.job_card_id != jc_explicit.id

    # 6. Leading/trailing whitespace normalization
    padded_data = dict(shared)
    padded_data["job_card_id"] = "   JC-PADDED-ID-01   "
    jc_padded = JobCard(**padded_data, customer_id="c1", customer_name="C1")
    assert jc_padded.job_card_id == "JC-PADDED-ID-01"


# 23. Revision code rejection of explicitly supplied inconsistent values
def test_revision_code_inconsistent_explicit_values_rejected():
    """Verify revision_code validation rules:

    1. Generated defaults: auto-synchronize to REV-{revision:02d}.
    2. Explicit consistent values: preserved as supplied.
    3. Explicit consistent custom suffix: preserved as supplied.
    4. Explicit inconsistent values: rejected with ValidationError.
    5. Explicit whitespace-only or empty values: rejected with ValidationError.
    6. Never silently overwrite user data.
    """
    shared = _shared_identity()

    # 1. Generated defaults auto-synchronize
    shared_v1 = dict(shared)
    shared_v1["revision"] = 1
    jc_def1 = JobCard(**shared_v1, customer_id="c1", customer_name="C1")
    assert jc_def1.revision_code == "REV-01"

    shared_v2 = dict(shared)
    shared_v2["revision"] = 2
    jc_def2 = JobCard(**shared_v2, customer_id="c1", customer_name="C1",
                      revision_reason="ECN-101: Track width widened")
    assert jc_def2.revision_code == "REV-02"

    shared_v10 = dict(shared)
    shared_v10["revision"] = 10
    jc_def10 = JobCard(**shared_v10, customer_id="c1", customer_name="C1",
                       revision_reason="ECN-110: Tenth iteration")
    assert jc_def10.revision_code == "REV-10"

    # 2. Explicit consistent values preserved
    shared_exp = dict(shared)
    shared_exp["revision"] = 2
    jc_exp = JobCard(**shared_exp, customer_id="c1", customer_name="C1",
                     revision_code="REV-02",
                     revision_reason="ECN-102: Consistent explicit code")
    assert jc_exp.revision_code == "REV-02"

    # 3. Explicit consistent custom format preserved
    shared_custom = dict(shared)
    shared_custom["revision"] = 4
    jc_custom = JobCard(**shared_custom, customer_id="c1", customer_name="C1",
                        revision_code="REV-04-SPECIAL-BUILD",
                        revision_reason="ECN-104: Special build for prototype run")
    assert jc_custom.revision_code == "REV-04-SPECIAL-BUILD"

    # 4. Explicit inconsistent values REJECTED (never silently overwritten)
    # revision=2 but explicit revision_code="REV-01"
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared_v2, customer_id="c1", customer_name="C1",
                revision_code="REV-01",
                revision_reason="ECN-105: Inconsistent code attempt")
    assert "is inconsistent with revision number 2" in str(exc_info.value)

    # revision=1 but explicit revision_code="REV-02"
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared_v1, customer_id="c1", customer_name="C1",
                revision_code="REV-02")
    assert "is inconsistent with revision number 1" in str(exc_info.value)

    # revision=5 but explicit revision_code="REV-03"
    shared_v5 = dict(shared)
    shared_v5["revision"] = 5
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared_v5, customer_id="c1", customer_name="C1",
                revision_code="REV-03",
                revision_reason="ECN-105: Inconsistent code attempt")
    assert "is inconsistent with revision number 5" in str(exc_info.value)

    # 5. Explicit whitespace-only or empty revision_code REJECTED
    with pytest.raises(ValidationError, match="revision_code cannot be empty or whitespace-only"):
        JobCard(**shared_v1, customer_id="c1", customer_name="C1", revision_code="   ")

    with pytest.raises(ValidationError, match="revision_code cannot be empty or whitespace-only"):
        JobCard(**shared_v1, customer_id="c1", customer_name="C1", revision_code="")

    # 6. Leading/trailing whitespace on consistent code normalized
    jc_trim = JobCard(**shared_v2, customer_id="c1", customer_name="C1",
                      revision_code="  REV-02  ",
                      revision_reason="ECN-106: Trim test")
    assert jc_trim.revision_code == "REV-02"


# 24. Master Job Card sub-document linking pointer validation (Correction C)
def test_master_job_card_pointer_validation():
    """Verify master Job Card sub-document linking pointer validation rules:

    1. None pointers accepted for standalone/unlinked drafts (omitted and explicit None).
    2. Empty string drill pointer rejected with ValidationError.
    3. Whitespace-only drill pointer rejected with ValidationError.
    4. Empty string process pointer rejected with ValidationError.
    5. Whitespace-only process pointer rejected with ValidationError.
    6. Valid pointer strings accepted and surrounding whitespace normalized.
    """
    shared = _shared_identity()

    # 1. Standalone drafts with omitted pointers default to None
    jc_omitted = JobCard(**shared, customer_id="c1", customer_name="C1")
    assert jc_omitted.drill_sheet_id is None
    assert jc_omitted.process_sequence_sheet_id is None

    # Explicit None pointers accepted for unlinked drafts
    jc_none = JobCard(
        **shared,
        customer_id="c1",
        customer_name="C1",
        drill_sheet_id=None,
        process_sequence_sheet_id=None,
    )
    assert jc_none.drill_sheet_id is None
    assert jc_none.process_sequence_sheet_id is None

    # 2. Empty string drill_sheet_id rejected
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared, customer_id="c1", customer_name="C1", drill_sheet_id="")
    assert "drill_sheet_id" in str(exc_info.value)

    # 3. Whitespace-only drill_sheet_id rejected
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared, customer_id="c1", customer_name="C1", drill_sheet_id="   ")
    assert "drill_sheet_id" in str(exc_info.value)

    # 4. Empty string process_sequence_sheet_id rejected
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared, customer_id="c1", customer_name="C1", process_sequence_sheet_id="")
    assert "process_sequence_sheet_id" in str(exc_info.value)

    # 5. Whitespace-only process_sequence_sheet_id rejected
    with pytest.raises(ValidationError) as exc_info:
        JobCard(**shared, customer_id="c1", customer_name="C1", process_sequence_sheet_id="   ")
    assert "process_sequence_sheet_id" in str(exc_info.value)

    # 6. Valid pointer strings accepted and whitespace normalized
    jc_valid = JobCard(
        **shared,
        customer_id="c1",
        customer_name="C1",
        drill_sheet_id="   drill-sheet-uuid-001   ",
        process_sequence_sheet_id="   proc-sheet-uuid-002   ",
    )
    assert jc_valid.drill_sheet_id == "drill-sheet-uuid-001"
    assert jc_valid.process_sequence_sheet_id == "proc-sheet-uuid-002"


# ============================================================
# DAY 1.9 — Sub-Sheet Version & Customer Field Validation
# ============================================================


def test_subsheet_optimistic_concurrency_versions_and_validation():
    """DrillDetailsSheet and ProcessSequenceSheet track version >= 1 defaulting to 1."""
    shared = _shared_identity()
    drill_default = DrillDetailsSheet(**shared)
    proc_default = ProcessSequenceSheet(**shared)
    assert drill_default.version == 1
    assert proc_default.version == 1

    drill_v4 = DrillDetailsSheet(**shared, version=4)
    proc_v7 = ProcessSequenceSheet(**shared, version=7)
    assert drill_v4.version == 4
    assert proc_v7.version == 7

    for invalid_ver in (0, -1, -99):
        with pytest.raises(ValidationError):
            DrillDetailsSheet(**shared, version=invalid_ver)
        with pytest.raises(ValidationError):
            ProcessSequenceSheet(**shared, version=invalid_ver)


def test_customer_id_and_customer_name_reject_empty_and_whitespace_strings():
    """customer_id and customer_name must be non-empty stripped strings (_NonEmptyStr)."""
    shared = _shared_identity()
    for bad in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            JobCard(**shared, customer_id=bad, customer_name="Valid Name")
        with pytest.raises(ValidationError):
            JobCard(**shared, customer_id="cust-001", customer_name=bad)

    jc = JobCard(**shared, customer_id="  cust-001  ", customer_name="  Apex Customer  ")
    assert jc.customer_id == "cust-001"
    assert jc.customer_name == "Apex Customer"
