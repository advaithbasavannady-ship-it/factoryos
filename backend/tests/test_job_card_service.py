"""Unit and service integration tests for Job Card persistence layer.

Executes under the isolated mock lane (FACTORYOS_TEST_LANE=mock).
Tests verify:
1. Successful three-document package creation.
2. Shared identity consistency across all three documents.
3. Missing or mismatched identity rejection.
4. Duplicate package rejection.
5. Tenant isolation (scoping and cross-tenant isolation).
6. Retrieval of a complete package.
7. Missing linked drill sheet detection.
8. Missing linked process sequence sheet detection.
9. Successful draft update with audit.
10. Rejection of approved Job Card updates.
11. Rejection of released Job Card updates.
12. Rejection of superseded Job Card updates.
13. Prevention of identity-field modification.
14. Database write failure handling.
15. Audit failure handling where supported by the real helper contract.
16. Idempotency replay behaviour where supported by the real helper contract.
17. Prevention of partial writes in the service logic.

NOTE: These tests run in the isolated mock test lane verifying service logic,
callback sequencing, payload validation, and exception propagation. They do not
claim that in-memory mock testing proves physical MongoDB replica-set engine durability.
"""

from __future__ import annotations

import copy
import sys
from types import ModuleType
from typing import Any

import pytest
from fastapi import HTTPException

# Ensure lib.db is stubbed before importing audit / idempotency / service modules
# so that lack of real MONGO_URL in mock lane does not cause KeyError on import.


class MockSession:
    """Mock MongoDB ClientSession simulating transactional staging and rollback."""

    def __init__(self, client: MockClient) -> None:
        self.client = client
        self.staged_inserts: list[tuple[MockCollection, dict[str, Any]]] = []
        self.staged_updates: list[tuple[MockCollection, dict[str, Any], dict[str, Any]]] = []
        self.is_active = True

    async def __aenter__(self) -> MockSession:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if exc_type is not None:
            self.abort_transaction()
        self.is_active = False
        return False

    def abort_transaction(self) -> None:
        self.staged_inserts.clear()
        self.staged_updates.clear()

    async def with_transaction(self, callback: Any) -> Any:
        try:
            result = await callback(self)
            # Commit staged mutations
            for col, doc in self.staged_inserts:
                col._commit_insert(doc)
            for col, query, update in self.staged_updates:
                col._commit_update(query, update)
            self.staged_inserts.clear()
            self.staged_updates.clear()
            return result
        except Exception:
            self.abort_transaction()
            raise


class MockUpdateResult:
    def __init__(self, matched_count: int, modified_count: int) -> None:
        self.matched_count = matched_count
        self.modified_count = modified_count


class MockCollection:
    """In-memory collection with transactional staging, matching, and query filters."""

    def __init__(self, name: str, db: MockDatabase) -> None:
        self.name = name
        self.db = db
        self.docs: list[dict[str, Any]] = []
        self.fail_writes = False
        self.fail_write_message = "Simulated write failure"

    def _matches(self, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or":
                if not any(self._matches(doc, branch) for branch in v):
                    return False
            elif k == "$ne":
                pass
            elif isinstance(v, dict):
                # Simple sub-operator support (e.g. {"$ne": val}, {"$exists": bool}, {"$in": [...]})
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
                if "$exists" in v:
                    exists = (k in doc)
                    if v["$exists"] != exists:
                        return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    def _commit_insert(self, doc: dict[str, Any]) -> None:
        self.docs.append(copy.deepcopy(doc))

    def _commit_update(self, query: dict[str, Any], update: dict[str, Any]) -> None:
        set_vals = update.get("$set", {})
        inc_vals = update.get("$inc", {})
        for d in self.docs:
            if self._matches(d, query):
                d.update(copy.deepcopy(set_vals))
                for inc_k, inc_v in inc_vals.items():
                    d[inc_k] = int(d.get(inc_k) or 0) + inc_v

    async def insert_one(self, doc: dict[str, Any], session: MockSession | None = None) -> Any:
        if self.fail_writes:
            raise RuntimeError(self.fail_write_message)

        cloned = copy.deepcopy(doc)
        if session is not None and getattr(session, "is_active", False):
            session.staged_inserts.append((self, cloned))
        else:
            self.docs.append(cloned)
        return type("InsertResult", (), {"inserted_id": cloned.get("_id") or cloned.get("id")})()

    async def find_one(
        self,
        query: dict[str, Any],
        projection: dict[str, Any] | None = None,
        session: MockSession | None = None,
    ) -> dict[str, Any] | None:
        # Check staged uncommitted inserts in active session first (read-your-own-writes)
        if session is not None and getattr(session, "is_active", False):
            for col, staged_doc in reversed(session.staged_inserts):
                if col is self and self._matches(staged_doc, query):
                    out = copy.deepcopy(staged_doc)
                    if projection and "_id" in projection and projection["_id"] == 0:
                        out.pop("_id", None)
                    return out

        # Check committed documents
        for d in self.docs:
            if self._matches(d, query):
                out = copy.deepcopy(d)
                if projection and "_id" in projection and projection["_id"] == 0:
                    out.pop("_id", None)
                return out
        return None

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        session: MockSession | None = None,
        upsert: bool = False,
    ) -> MockUpdateResult:
        if self.fail_writes:
            raise RuntimeError(self.fail_write_message)

        matched = 0
        for d in self.docs:
            if self._matches(d, query):
                matched += 1
                break

        if session is not None and getattr(session, "is_active", False):
            session.staged_updates.append((self, query, update))
        else:
            if matched:
                self._commit_update(query, update)

        return MockUpdateResult(matched_count=matched, modified_count=matched)

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        projection: dict[str, Any] | None = None,
        session: MockSession | None = None,
        return_document: Any = None,
    ) -> dict[str, Any] | None:
        if self.fail_writes:
            raise RuntimeError(self.fail_write_message)
        found = await self.find_one(query, projection=projection, session=session)
        if found is None:
            return None
        if session is not None and getattr(session, "is_active", False):
            session.staged_updates.append((self, query, update))
        else:
            self._commit_update(query, update)
        return found

    async def delete_one(self, query: dict[str, Any], session: MockSession | None = None) -> Any:
        before = len(self.docs)
        self.docs = [d for d in self.docs if not self._matches(d, query)]
        deleted = before - len(self.docs)
        return type("DeleteResult", (), {"deleted_count": deleted})()


class MockDatabase:
    """In-memory MongoDB database mocking FactoryOS collections."""

    def __init__(self) -> None:
        self.orders = MockCollection("orders", self)
        self.job_cards = MockCollection("job_cards", self)
        self.drill_detail_sheets = MockCollection("drill_detail_sheets", self)
        self.process_sequence_sheets = MockCollection("process_sequence_sheets", self)
        self.audit_logs = MockCollection("audit_logs", self)
        self.idempotency_records = MockCollection("idempotency_records", self)

    def __getitem__(self, name: str) -> MockCollection:
        return getattr(self, name)


class MockClient:
    """Mock Motor client providing transaction sessions."""

    def __init__(self, db: MockDatabase) -> None:
        self.db = db

    async def start_session(self) -> MockSession:
        return MockSession(self)


# Inject stub lib.db into sys.modules so audit and idempotency helpers use our mock
fake_db_module = ModuleType("lib.db")
mock_db_instance = MockDatabase()
mock_client_instance = MockClient(mock_db_instance)
fake_db_module.db = mock_db_instance
fake_db_module.client = mock_client_instance
sys.modules["lib.db"] = fake_db_module

# Now import the service module and exceptions under test
from models.job_cards import DrillDetailsSheet, JobCard, ProcessSequenceSheet
from services.job_cards import (
    DuplicateJobCardPackageError,
    JobCardIdentityMismatchError,
    JobCardLockError,
    JobCardNotFoundError,
    JobCardPackage,
    JobCardServiceError,
    JobCardValidationError,
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
    verify_package_identity,
)


def _seed_confirmed_orders(db: MockDatabase) -> None:
    """Seed confirmed parent Order fixtures used across the service test suite."""
    default_orders = [
        ("company-alpha", "ORD-001", "SO-2026-100", "CUST-99"),
        ("company-alpha", "ORD-CONSIST-1", "SO-CONSIST-1", "CUST-1"),
        ("company-alpha", "ORD-1", "SO-1", "C-1"),
        ("company-alpha", "ORD-2", "SO-2", "C-2"),
        ("company-alpha", "ORD-ALPHA", "SO-ALPHA", "C-1"),
        ("company-beta", "ORD-BETA", "SO-BETA", "C-2"),
        ("company-alpha", "ORD-FETCH", "SO-FETCH", "C-1"),
        ("company-alpha", "ORD-100", "SO-100", "C-1"),
        ("company-alpha", "ORD-10", "SO-10", "C-1"),
        ("company-alpha", "ORD-999", "SO-999", "C-1"),
    ]
    for company_id, order_id, order_no, customer_id in default_orders:
        db.orders.docs.append(
            {
                "company_id": company_id,
                "id": order_id,
                "order_no": order_no,
                "customer_id": customer_id,
                "status": "confirmed",
            }
        )


@pytest.fixture
def mock_db() -> MockDatabase:
    """Fresh in-memory mock database for each test."""
    db = MockDatabase()
    _seed_confirmed_orders(db)
    client = MockClient(db)
    fake_db_module.db = db
    fake_db_module.client = client
    return db


@pytest.fixture
def standard_user() -> dict[str, Any]:
    return {
        "id": "usr-test-101",
        "name": "Alex Engineer",
        "role": "production_manager",
        "company_id": "company-alpha",
    }


# ==============================================================================
# 1. Successful three-document package creation
# ==============================================================================

@pytest.mark.asyncio
async def test_successful_three_document_package_creation(mock_db: MockDatabase, standard_user: dict[str, Any]) -> None:
    pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-2026-0001",
        order_id="ORD-001",
        order_no="SO-2026-100",
        part_number="PCB-REV-A",
        customer_id="CUST-99",
        customer_name="Nordic Dynamics",
        order_quantity_pcb=500,
        layers=4,
        db=mock_db,
    )

    assert isinstance(pkg, JobCardPackage)
    assert pkg.job_card.status == "draft"
    assert pkg.job_card.job_card_no == "JC-2026-0001"
    assert pkg.job_card.revision == 1
    assert pkg.job_card.revision_code == "REV-01"
    assert pkg.job_card.order_quantity_pcb == 500
    assert pkg.job_card.layers == 4

    # Verify links
    assert pkg.job_card.drill_sheet_id == pkg.drill_sheet.id
    assert pkg.job_card.process_sequence_sheet_id == pkg.process_sequence_sheet.id

    # Verify persistence in mock database
    assert len(mock_db.job_cards.docs) == 1
    assert len(mock_db.drill_detail_sheets.docs) == 1
    assert len(mock_db.process_sequence_sheets.docs) == 1

    # Verify audit event
    assert len(mock_db.audit_logs.docs) == 1
    audit = mock_db.audit_logs.docs[0]
    assert audit["action"] == "job_card.created"
    assert audit["tenant_id"] == "company-alpha"
    assert audit["user_id"] == standard_user["id"]


# ==============================================================================
# 2. Shared identity consistency across all three documents
# ==============================================================================

@pytest.mark.asyncio
async def test_shared_identity_consistency_across_all_documents(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-CONSIST-01",
        order_id="ORD-CONSIST-1",
        order_no="SO-CONSIST-1",
        part_number="PART-CONSIST",
        customer_id="CUST-1",
        customer_name="Acme Corp",
        db=mock_db,
    )

    identity_keys = (
        "job_card_id",
        "job_card_no",
        "order_id",
        "order_no",
        "part_number",
        "revision",
        "company_id",
    )
    for key in identity_keys:
        jc_val = getattr(pkg.job_card, key)
        drill_val = getattr(pkg.drill_sheet, key)
        proc_val = getattr(pkg.process_sequence_sheet, key)
        assert jc_val == drill_val == proc_val, f"Mismatch on key {key}: {jc_val} != {drill_val} != {proc_val}"


# ==============================================================================
# 3. Missing or mismatched identity rejection
# ==============================================================================

@pytest.mark.asyncio
async def test_mismatched_identity_drill_sheet_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    custom_drill = {
        "part_number": "DIFFERENT-PART-NO",  # Mismatch with master part_number
    }
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-MISMATCH-1",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PART-REAL",
            customer_id="C-1",
            customer_name="Test",
            drill_sheet=custom_drill,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "part_number" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_mismatched_identity_process_sheet_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    custom_proc = {
        "order_no": "DIFFERENT-ORDER-NO",  # Mismatch with master order_no
    }
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-MISMATCH-2",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PART-REAL",
            customer_id="C-1",
            customer_name="Test",
            process_sequence_sheet=custom_proc,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "order_no" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_client_company_id_mismatch_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card={
                "company_id": "company-attacker",  # Attempted override of tenant
                "job_card_no": "JC-SPOOF",
                "order_id": "ORD-1",
                "order_no": "SO-1",
                "part_number": "PART-1",
                "customer_id": "C-1",
                "customer_name": "Test",
            },
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "company_id" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_empty_or_whitespace_identity_field_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    with pytest.raises(JobCardValidationError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="    ",  # Whitespace-only string
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PART-1",
            customer_id="C-1",
            customer_name="Test",
            db=mock_db,
        )
    assert exc_info.value.status_code == 422


# ==============================================================================
# 4. Duplicate package rejection
# ==============================================================================

@pytest.mark.asyncio
async def test_duplicate_package_rejection(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    # First creation succeeds
    await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-DUP-CHECK",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Acme",
        db=mock_db,
    )

    # Second creation with identical (company_id, job_card_no, revision) is rejected
    with pytest.raises(DuplicateJobCardPackageError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-DUP-CHECK",
            order_id="ORD-2",
            order_no="SO-2",
            part_number="PN-2",
            customer_id="C-2",
            customer_name="Acme 2",
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "already exists" in str(exc_info.value.detail)


# ==============================================================================
# 5. Tenant isolation
# ==============================================================================

@pytest.mark.asyncio
async def test_tenant_isolation(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    # Create package under company-alpha
    alpha_pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-SHARED-NO",
        order_id="ORD-ALPHA",
        order_no="SO-ALPHA",
        part_number="PN-ALPHA",
        customer_id="C-1",
        customer_name="Client Alpha",
        db=mock_db,
    )

    # Attempt to fetch company-alpha's job card from company-beta -> 404
    with pytest.raises(JobCardNotFoundError) as exc_info:
        await get_job_card_package(
            company_id="company-beta",
            job_card_id=alpha_pkg.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 404

    # Company beta can create its own package with the same job_card_no independently
    beta_user = {**standard_user, "company_id": "company-beta"}
    beta_pkg = await create_job_card_package(
        company_id="company-beta",
        user=beta_user,
        job_card_no="JC-SHARED-NO",
        order_id="ORD-BETA",
        order_no="SO-BETA",
        part_number="PN-BETA",
        customer_id="C-2",
        customer_name="Client Beta",
        db=mock_db,
    )
    assert beta_pkg.job_card.company_id == "company-beta"
    assert beta_pkg.job_card.id != alpha_pkg.job_card.id


# ==============================================================================
# 6. Retrieval of a complete package
# ==============================================================================

@pytest.mark.asyncio
async def test_retrieval_of_complete_package(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-FETCH-01",
        order_id="ORD-FETCH",
        order_no="SO-FETCH",
        part_number="PN-FETCH",
        customer_id="C-1",
        customer_name="Fetch Customer",
        db=mock_db,
    )

    fetched = await get_job_card_package(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        db=mock_db,
    )
    assert fetched.job_card.id == created.job_card.id
    assert fetched.drill_sheet.id == created.drill_sheet.id
    assert fetched.process_sequence_sheet.id == created.process_sequence_sheet.id
    assert fetched.job_card.job_card_no == "JC-FETCH-01"


# ==============================================================================
# 7. Missing linked drill sheet detection
# ==============================================================================

@pytest.mark.asyncio
async def test_missing_linked_drill_sheet_raises_not_found(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-DRILL-MISSING",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Corrupt database state by removing drill sheet
    await mock_db.drill_detail_sheets.delete_one({"job_card_id": created.job_card.id})

    with pytest.raises(JobCardNotFoundError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 404
    assert "Drill Details Sheet" in str(exc_info.value.detail)


# ==============================================================================
# 8. Missing linked process sequence sheet detection
# ==============================================================================

@pytest.mark.asyncio
async def test_missing_linked_process_sequence_sheet_raises_not_found(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-PROC-MISSING",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Corrupt database state by removing process sequence sheet
    await mock_db.process_sequence_sheets.delete_one({"job_card_id": created.job_card.id})

    with pytest.raises(JobCardNotFoundError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 404
    assert "Process Sequence Sheet" in str(exc_info.value.detail)


# ==============================================================================
# 9. Successful draft update
# ==============================================================================

@pytest.mark.asyncio
async def test_successful_draft_update(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-UPDATE-DRAFT",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        layers=2,
        db=mock_db,
    )

    patch = {
        "layers": 6,
        "order_quantity_pcb": 1200,
        "pcb_dimensions": {
            "length_mm": 150.0,
            "width_mm": 80.0,
            "thickness_mm": 1.6,
        },
    }

    updated = await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch=patch,
        db=mock_db,
    )

    assert updated.layers == 6
    assert updated.order_quantity_pcb == 1200
    assert updated.pcb_dimensions.length_mm == 150.0

    # Verify persisted in database
    persisted = await mock_db.job_cards.find_one({"id": created.job_card.id})
    assert persisted["layers"] == 6

    # Verify audit log recorded update
    audits = [a for a in mock_db.audit_logs.docs if a["action"] == "job_card.updated"]
    assert len(audits) == 1
    assert audits[0]["resource_id"] == created.job_card.id


# ==============================================================================
# 10. Rejection of approved Job Card updates
# ==============================================================================

@pytest.mark.asyncio
async def test_rejection_of_approved_job_card_updates(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-APPROVED-LOCK",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Transition to approved
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"status": "approved"}},
    )

    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 4},
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "approved" in str(exc_info.value.detail)


# ==============================================================================
# 11. Rejection of released Job Card updates
# ==============================================================================

@pytest.mark.asyncio
async def test_rejection_of_released_job_card_updates(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-RELEASED-LOCK",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Transition to released_to_production
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"status": "released_to_production"}},
    )

    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 4},
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "released_to_production" in str(exc_info.value.detail)


# ==============================================================================
# 12. Rejection of superseded Job Card updates
# ==============================================================================

@pytest.mark.asyncio
async def test_rejection_of_superseded_job_card_updates(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-SUPERSEDED-LOCK",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Transition to superseded
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"status": "superseded"}},
    )

    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 4},
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "superseded" in str(exc_info.value.detail)


# ==============================================================================
# 13. Prevention of identity-field modification
# ==============================================================================

@pytest.mark.asyncio
async def test_prevention_of_identity_field_modification(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IMMUTABLE-01",
        order_id="ORD-100",
        order_no="SO-100",
        part_number="PN-ORIGINAL",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Attempt to tamper with various protected identity fields
    tamper_attempts = [
        {"job_card_no": "JC-TAMPERED"},
        {"revision": 2},
        {"order_id": "ORD-TAMPERED"},
        {"order_no": "SO-TAMPERED"},
        {"part_number": "PN-TAMPERED"},
        {"company_id": "company-tampered"},
        {"drill_sheet_id": "drill-tampered"},
        {"process_sequence_sheet_id": "proc-tampered"},
    ]

    for patch in tamper_attempts:
        with pytest.raises(JobCardIdentityMismatchError) as exc_info:
            await update_draft_job_card(
                company_id="company-alpha",
                job_card_id=created.job_card.id,
                user=standard_user,
                patch=patch,
                db=mock_db,
            )
        assert exc_info.value.status_code == 422

    # Verify original fields remain unchanged in database
    persisted = await mock_db.job_cards.find_one({"id": created.job_card.id})
    assert persisted["job_card_no"] == "JC-IMMUTABLE-01"
    assert persisted["part_number"] == "PN-ORIGINAL"
    assert persisted["revision"] == 1


# ==============================================================================
# 14. Database write failure handling
# ==============================================================================

@pytest.mark.asyncio
async def test_database_write_failure_handling(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    mock_db.job_cards.fail_writes = True
    mock_db.job_cards.fail_write_message = "I/O write failure on primary node"

    with pytest.raises(RuntimeError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-WRITE-FAIL",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    assert "I/O write failure on primary node" in str(exc_info.value)
    # Ensure no documents persisted
    assert len(mock_db.job_cards.docs) == 0
    assert len(mock_db.drill_detail_sheets.docs) == 0
    assert len(mock_db.process_sequence_sheets.docs) == 0


# ==============================================================================
# 15. Audit failure handling where supported by the real helper contract
# ==============================================================================

@pytest.mark.asyncio
async def test_audit_failure_handling(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    # Cause audit insert failure to simulate audit logging unavailability
    mock_db.audit_logs.fail_writes = True
    mock_db.audit_logs.fail_write_message = "Audit journal disk quota exceeded"

    with pytest.raises(HTTPException) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-AUDIT-FAIL",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    # Real run_audited_mutation contract translates AuditPersistenceError to HTTP 503
    assert exc_info.value.status_code == 503
    assert "required audit persistence failed" in exc_info.value.detail

    # Verify transaction aborts: no Job Card documents remain committed
    assert len(mock_db.job_cards.docs) == 0
    assert len(mock_db.drill_detail_sheets.docs) == 0
    assert len(mock_db.process_sequence_sheets.docs) == 0


# ==============================================================================
# 16. Idempotency replay behaviour where supported by the real helper contract
# ==============================================================================

@pytest.mark.asyncio
async def test_idempotency_replay_behaviour(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    key = "idem-jc-create-123456789012345"

    # First call: executes mutation and creates package
    pkg1 = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEMPOTENT-01",
        order_id="ORD-10",
        order_no="SO-10",
        part_number="PN-10",
        customer_id="C-1",
        customer_name="Customer",
        idempotency_key=key,
        db=mock_db,
    )

    assert pkg1.job_card.job_card_no == "JC-IDEMPOTENT-01"
    assert len(mock_db.job_cards.docs) == 1
    assert len(mock_db.idempotency_records.docs) == 1

    # Second call with the identical key & payload: replays cached result without re-executing
    pkg2 = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEMPOTENT-01",
        order_id="ORD-10",
        order_no="SO-10",
        part_number="PN-10",
        customer_id="C-1",
        customer_name="Customer",
        idempotency_key=key,
        db=mock_db,
    )

    assert pkg2.job_card.id == pkg1.job_card.id
    assert len(mock_db.job_cards.docs) == 1  # No duplicate created

    # Calling with the same key but conflicting payload -> raises HTTP 409 Conflict
    with pytest.raises(HTTPException) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-IDEMPOTENT-DIFFERENT",
            order_id="ORD-999",
            order_no="SO-999",
            part_number="PN-999",
            customer_id="C-1",
            customer_name="Customer",
            idempotency_key=key,
            db=mock_db,
        )
    assert exc_info.value.status_code == 409


# ==============================================================================
# 17. Prevention of partial writes in the service logic
# ==============================================================================

@pytest.mark.asyncio
async def test_prevention_of_partial_writes_on_process_sequence_sheet_failure(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    # Fail only the 3rd document insertion
    mock_db.process_sequence_sheets.fail_writes = True
    mock_db.process_sequence_sheets.fail_write_message = "ProcessSequenceSheet write error"

    with pytest.raises(RuntimeError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-PARTIAL-ABORT",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    assert "ProcessSequenceSheet write error" in str(exc_info.value)

    # Crucial assertion: transaction aborted, meaning 0 documents committed across all collections
    assert len(mock_db.job_cards.docs) == 0, "Partial write leak: job_cards was not rolled back!"
    assert len(mock_db.drill_detail_sheets.docs) == 0, "Partial write leak: drill_detail_sheets was not rolled back!"
    assert len(mock_db.process_sequence_sheets.docs) == 0


# ==============================================================================
# 18. Sub-document ID mismatch verification
# ==============================================================================

@pytest.mark.asyncio
async def test_drill_sheet_id_mismatch_in_package_identity_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    custom_drill = {
        "id": "drill-custom-id-999",  # Does not match master.drill_sheet_id
    }
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-DRILL-ID-MISMATCH",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            drill_sheet=custom_drill,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "drill_sheet" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_process_sequence_sheet_id_mismatch_in_package_identity_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    custom_proc = {
        "id": "proc-custom-id-999",  # Does not match master.process_sequence_sheet_id
    }
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-PROC-ID-MISMATCH",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            process_sequence_sheet=custom_proc,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_job_card_package_detects_mismatched_drill_sheet_id(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-DRILL-CORRUPT-ID",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Corrupt drill sheet ID in database while keeping job_card_id matching
    await mock_db.drill_detail_sheets.update_one(
        {"job_card_id": created.job_card.id},
        {"$set": {"id": "corrupted-drill-id-777"}},
    )

    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "drill_sheet_id mismatch" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_get_job_card_package_detects_mismatched_process_sheet_id(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-PROC-CORRUPT-ID",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Corrupt process sequence sheet ID in database while keeping job_card_id matching
    await mock_db.process_sequence_sheets.update_one(
        {"job_card_id": created.job_card.id},
        {"$set": {"id": "corrupted-proc-id-888"}},
    )

    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet_id mismatch" in str(exc_info.value.detail)


# ==============================================================================
# 19. Expanded idempotency payload tests (Three-document specifications)
# ==============================================================================

@pytest.mark.asyncio
async def test_idempotency_conflict_on_master_specification_change(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    key = "idem-master-change-123456789012"

    # Initial creation: 2 layers, 500 units
    await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEM-SPEC-1",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        layers=2,
        order_quantity_pcb=500,
        idempotency_key=key,
        db=mock_db,
    )

    # Replay with same key but changed master specification (layers=4)
    with pytest.raises(HTTPException) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-IDEM-SPEC-1",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            layers=4,  # Changed specification!
            order_quantity_pcb=500,
            idempotency_key=key,
            db=mock_db,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_conflict_on_drill_specification_change(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    key = "idem-drill-change-123456789012"

    # Initial creation with empty drill tools
    await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEM-SPEC-2",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        idempotency_key=key,
        db=mock_db,
    )

    # Replay with same key but changed drill sheet tool specification
    with pytest.raises(HTTPException) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-IDEM-SPEC-2",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            drill_sheet={
                "primary_drill": [{"tool_no": "T01", "drill_size_mm": 0.8, "hole_type": "PTH"}]
            },
            idempotency_key=key,
            db=mock_db,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_conflict_on_process_sequence_specification_change(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    key = "idem-proc-change-1234567890123"

    # Initial creation with standard route
    await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEM-SPEC-3",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        idempotency_key=key,
        db=mock_db,
    )

    # Replay with same key but changed route name
    with pytest.raises(HTTPException) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-IDEM-SPEC-3",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            process_sequence_sheet={"route_name": "Special Rapid-Turn PCB Route"},
            idempotency_key=key,
            db=mock_db,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_idempotency_replay_preserves_identical_full_specifications(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    key = "idem-replay-identical-1234567"

    pkg1 = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEM-IDENTICAL",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        layers=4,
        order_quantity_pcb=750,
        idempotency_key=key,
        db=mock_db,
    )

    pkg2 = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-IDEM-IDENTICAL",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        layers=4,
        order_quantity_pcb=750,
        idempotency_key=key,
        db=mock_db,
    )

    assert pkg1.job_card.id == pkg2.job_card.id
    assert pkg2.job_card.layers == 4
    assert pkg2.job_card.order_quantity_pcb == 750
    assert len(mock_db.job_cards.docs) == 1


# ==============================================================================
# 20. Optimistic concurrency control tests
# ==============================================================================

@pytest.mark.asyncio
async def test_optimistic_concurrency_version_increments_on_update(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-VERSION-INCR",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Initial version defaults to 1
    assert created.job_card.version == 1

    # First update: version increments to 2
    upd1 = await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch={"layers": 4},
        db=mock_db,
    )
    assert upd1.version == 2

    # Second update: version increments to 3
    upd2 = await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch={"order_quantity_pcb": 500},
        db=mock_db,
    )
    assert upd2.version == 3

    # Check database persistence
    in_db = await mock_db.job_cards.find_one({"id": created.job_card.id})
    assert in_db["version"] == 3


@pytest.mark.asyncio
async def test_optimistic_concurrency_stale_expected_version_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-STALE-VER-1",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # First update moves document to version 2
    await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch={"layers": 4},
        db=mock_db,
    )

    # Attempt update passing stale expected_version=1
    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 6},
            expected_version=1,
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "version conflict" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_optimistic_concurrency_stale_patch_version_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-STALE-VER-2",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Advance to version 2
    await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch={"layers": 4},
        db=mock_db,
    )

    # Attempt update with stale version in patch body
    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 6, "version": 1},
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "version conflict" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_optimistic_concurrency_concurrent_writer_simulation(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-CONCUR-SIM",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # User A and User B both read at version 1
    # User A completes update first
    await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch={"layers": 4, "version": 1},
        db=mock_db,
    )

    # User B now attempts to update based on their stale read (version 1)
    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"order_quantity_pcb": 1000, "version": 1},
            db=mock_db,
        )
    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_optimistic_concurrency_legacy_document_without_version(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-LEGACY-VER",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Remove 'version' field from database document to simulate legacy record
    for doc in mock_db.job_cards.docs:
        if doc["id"] == created.job_card.id:
            doc.pop("version", None)

    # Updating legacy document succeeds and sets version to 2
    updated = await update_draft_job_card(
        company_id="company-alpha",
        job_card_id=created.job_card.id,
        user=standard_user,
        patch={"layers": 8},
        db=mock_db,
    )
    assert updated.version == 2

    # Verify persisted in database as version 2
    in_db = await mock_db.job_cards.find_one({"id": created.job_card.id})
    assert in_db["version"] == 2

    # A subsequent update with stale version 1 must now be rejected
    with pytest.raises(JobCardLockError):
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 10, "version": 1},
            db=mock_db,
        )


# ==============================================================================
# 21. Fail-closed on unexpected database responses
# ==============================================================================

@pytest.mark.asyncio
async def test_update_draft_job_card_fails_closed_when_matched_count_missing(
    mock_db: MockDatabase, standard_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-FAIL-CLOSED",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Simulate an unexpected database response that does not expose matched_count
    async def unexpected_update_one(*args: Any, **kwargs: Any) -> Any:
        return object()  # Lacks matched_count attribute entirely!

    monkeypatch.setattr(mock_db.job_cards, "update_one", unexpected_update_one)

    with pytest.raises(JobCardLockError) as exc_info:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            user=standard_user,
            patch={"layers": 4},
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert "was not updated" in str(exc_info.value.detail)


# ==============================================================================
# 22. Strict bilateral pointer verification on retrieval and creation (Day 1.7A.2.3)
# ==============================================================================

@pytest.mark.asyncio
async def test_get_job_card_package_rejects_missing_or_empty_drill_sheet_id(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    """Missing or empty drill_sheet_id on persisted master is rejected during retrieval."""
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-DRILL-PTR-CHECK",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Case A: Missing (None) drill_sheet_id rejected via verify_package_identity
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"drill_sheet_id": None}},
    )
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "drill_sheet_id mismatch" in str(exc_info.value.detail)

    # Case B: Empty string ("") drill_sheet_id rejected during retrieval
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"drill_sheet_id": ""}},
    )
    with pytest.raises(JobCardServiceError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "drill_sheet_id" in str(exc_info.value.detail).lower()


@pytest.mark.asyncio
async def test_get_job_card_package_rejects_missing_or_empty_process_sequence_sheet_id(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    """Missing or empty process_sequence_sheet_id on persisted master is rejected during retrieval."""
    created = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-PROC-PTR-CHECK",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Case A: Missing (None) process_sequence_sheet_id rejected via verify_package_identity
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"process_sequence_sheet_id": None}},
    )
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet_id mismatch" in str(exc_info.value.detail)

    # Case B: Empty string ("") process_sequence_sheet_id rejected during retrieval
    await mock_db.job_cards.update_one(
        {"id": created.job_card.id},
        {"$set": {"process_sequence_sheet_id": ""}},
    )
    with pytest.raises(JobCardServiceError) as exc_info:
        await get_job_card_package(
            company_id="company-alpha",
            job_card_id=created.job_card.id,
            db=mock_db,
        )
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet_id" in str(exc_info.value.detail).lower()


def test_verify_package_identity_raises_identity_mismatch_on_missing_empty_or_whitespace_pointers() -> None:
    """verify_package_identity raises JobCardIdentityMismatchError on None, empty, whitespace or mismatched pointers."""
    from models.job_cards import DrillDetailsSheet, JobCard, ProcessSequenceSheet

    shared = {
        "job_card_id": "jc-shared-uuid",
        "job_card_no": "JC-SHARED-01",
        "order_id": "ord-01",
        "order_no": "SO-01",
        "part_number": "PN-01",
        "company_id": "comp-alpha",
        "customer_id": "cust-01",
        "customer_name": "Customer",
    }
    drill = DrillDetailsSheet(id="drill-valid-id", **shared)
    proc = ProcessSequenceSheet(id="proc-valid-id", **shared)

    # 1. Missing (None) drill pointer
    jc_drill_none = JobCard(**shared, drill_sheet_id=None, process_sequence_sheet_id="proc-valid-id")
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        verify_package_identity(jc_drill_none, drill, proc)
    assert exc_info.value.status_code == 422
    assert "drill_sheet_id mismatch" in str(exc_info.value.detail)

    # 2. Empty string ("") drill pointer
    jc_drill_empty = JobCard(**shared, drill_sheet_id="drill-valid-id", process_sequence_sheet_id="proc-valid-id")
    object.__setattr__(jc_drill_empty, "drill_sheet_id", "")
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        verify_package_identity(jc_drill_empty, drill, proc)
    assert exc_info.value.status_code == 422
    assert "drill_sheet_id mismatch" in str(exc_info.value.detail)

    # 3. Whitespace-only ("   ") drill pointer
    jc_drill_ws = JobCard(**shared, drill_sheet_id="drill-valid-id", process_sequence_sheet_id="proc-valid-id")
    object.__setattr__(jc_drill_ws, "drill_sheet_id", "   ")
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        verify_package_identity(jc_drill_ws, drill, proc)
    assert exc_info.value.status_code == 422
    assert "drill_sheet_id mismatch" in str(exc_info.value.detail)

    # 4. Missing (None) process sequence pointer
    jc_proc_none = JobCard(**shared, drill_sheet_id="drill-valid-id", process_sequence_sheet_id=None)
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        verify_package_identity(jc_proc_none, drill, proc)
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet_id mismatch" in str(exc_info.value.detail)

    # 5. Empty string ("") process sequence pointer
    jc_proc_empty = JobCard(**shared, drill_sheet_id="drill-valid-id", process_sequence_sheet_id="proc-valid-id")
    object.__setattr__(jc_proc_empty, "process_sequence_sheet_id", "")
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        verify_package_identity(jc_proc_empty, drill, proc)
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet_id mismatch" in str(exc_info.value.detail)

    # 6. Whitespace-only ("   ") process sequence pointer
    jc_proc_ws = JobCard(**shared, drill_sheet_id="drill-valid-id", process_sequence_sheet_id="proc-valid-id")
    object.__setattr__(jc_proc_ws, "process_sequence_sheet_id", "   ")
    with pytest.raises(JobCardIdentityMismatchError) as exc_info:
        verify_package_identity(jc_proc_ws, drill, proc)
    assert exc_info.value.status_code == 422
    assert "process_sequence_sheet_id mismatch" in str(exc_info.value.detail)

    # 7. Valid matching pointers succeed
    jc_valid = JobCard(**shared, drill_sheet_id="drill-valid-id", process_sequence_sheet_id="proc-valid-id")
    verify_package_identity(jc_valid, drill, proc)  # No exception raised


@pytest.mark.asyncio
async def test_create_package_whitespace_pointers_safely_replaced_with_generated_ids(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    """Whitespace-only pointers during package creation are safely replaced with fresh UUIDs."""
    pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-WS-PTR-SAFE",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        drill_sheet_id="   ",
        process_sequence_sheet_id="   ",
        db=mock_db,
    )

    # Verify drill pointer is valid non-empty UUID and matches sub-document
    assert pkg.job_card.drill_sheet_id
    assert pkg.job_card.drill_sheet_id.strip() != ""
    assert pkg.job_card.drill_sheet_id == pkg.drill_sheet.id

    # Verify process pointer is valid non-empty UUID and matches sub-document
    assert pkg.job_card.process_sequence_sheet_id
    assert pkg.job_card.process_sequence_sheet_id.strip() != ""
    assert pkg.job_card.process_sequence_sheet_id == pkg.process_sequence_sheet.id

    # Verify document retrieval succeeds and validates identity
    retrieved = await get_job_card_package(
        company_id="company-alpha",
        job_card_id=pkg.job_card.id,
        db=mock_db,
    )
    assert retrieved.job_card.drill_sheet_id == pkg.drill_sheet.id
    assert retrieved.job_card.process_sequence_sheet_id == pkg.process_sequence_sheet.id


# ==============================================================================
# 23. Day 1.9 — Parent Order validation, fencing & single active Job Card rule
# ==============================================================================


@pytest.mark.asyncio
async def test_parent_order_missing_or_cross_tenant_rejected_with_404(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    # Non-existent Order -> 404
    with pytest.raises(JobCardNotFoundError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-MISSING-ORD",
            order_id="ORD-DOES-NOT-EXIST",
            order_no="SO-MISSING",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    assert exc_info.value.status_code == 404

    # Cross-tenant Order (ORD-BETA belongs to company-beta) -> 404 when accessed from company-alpha
    with pytest.raises(JobCardNotFoundError) as exc_info_cross:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-CROSS-ORD",
            order_id="ORD-BETA",
            order_no="SO-BETA",
            part_number="PN-1",
            customer_id="C-2",
            customer_name="Customer Beta",
            db=mock_db,
        )
    assert exc_info_cross.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ineligible_status",
    [
        "awaiting_approval",
        "pending_review",
        "correction_requested",
        "rejected",
        "in_production",
        "quality_check",
        "ready_for_dispatch",
        "dispatched",
    ],
)
async def test_parent_order_non_confirmed_status_rejected_with_409(
    mock_db: MockDatabase, standard_user: dict[str, Any], ineligible_status: str
) -> None:
    order_id = f"ORD-STATUS-{ineligible_status}"
    mock_db.orders.docs.append(
        {
            "company_id": "company-alpha",
            "id": order_id,
            "order_no": "SO-STATUS",
            "customer_id": "C-1",
            "status": ineligible_status,
        }
    )

    with pytest.raises(JobCardLockError) as exc_info:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no=f"JC-STAT-{ineligible_status}",
            order_id=order_id,
            order_no="SO-STATUS",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    assert exc_info.value.status_code == 409
    assert ineligible_status in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_parent_order_identity_mismatch_and_initial_revision_gt_1_rejected(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    # Mismatched order_no -> 422
    with pytest.raises(JobCardIdentityMismatchError) as exc_order_no:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-ORD-NO-MISMATCH",
            order_id="ORD-1",
            order_no="SO-WRONG-NO",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    assert exc_order_no.value.status_code == 422
    assert "order_no" in str(exc_order_no.value.detail)

    # Mismatched customer_id -> 422
    with pytest.raises(JobCardIdentityMismatchError) as exc_cust:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-CUST-ID-MISMATCH",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="CUST-WRONG",
            customer_name="Customer",
            db=mock_db,
        )
    assert exc_cust.value.status_code == 422
    assert "customer_id" in str(exc_cust.value.detail)

    # Initial creation with revision > 1 -> 422
    with pytest.raises(JobCardValidationError) as exc_rev:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-REV2-DIRECT",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-1",
            customer_id="C-1",
            customer_name="Customer",
            revision=2,
            revision_reason="Direct revision 2 attempt",
            db=mock_db,
        )
    assert exc_rev.value.status_code == 422
    assert "revision 1" in str(exc_rev.value.detail)


@pytest.mark.asyncio
async def test_single_active_job_card_per_order_and_customer_id_immutability(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-ACTIVE-1",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )

    # Verify parent order fence version was incremented
    order_doc = await mock_db.orders.find_one({"company_id": "company-alpha", "id": "ORD-1"})
    assert order_doc["_job_card_lock_version"] == 1

    # Creating a second active Job Card for the same Order (even with different job_card_no) -> 409
    with pytest.raises(DuplicateJobCardPackageError) as exc_dup:
        await create_job_card_package(
            company_id="company-alpha",
            user=standard_user,
            job_card_no="JC-ACTIVE-2",
            order_id="ORD-1",
            order_no="SO-1",
            part_number="PN-2",
            customer_id="C-1",
            customer_name="Customer",
            db=mock_db,
        )
    assert exc_dup.value.status_code == 409
    assert "already has an active Job Card" in str(exc_dup.value.detail)

    # Attempting to modify customer_id via draft update -> 422
    with pytest.raises(JobCardIdentityMismatchError) as exc_tamper:
        await update_draft_job_card(
            company_id="company-alpha",
            job_card_id=pkg.job_card.id,
            user=standard_user,
            patch={"customer_id": "C-TAMPERED"},
            expected_version=1,
            db=mock_db,
        )
    assert exc_tamper.value.status_code == 422


# ==============================================================================
# 24. Day 1.9 — Draft sub-sheet updates, lifecycle transitions & ECN revisions
# ==============================================================================


@pytest.mark.asyncio
async def test_draft_sub_sheet_updates_versioning_and_locks(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-SUBSHEET-01",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-1",
        customer_id="C-1",
        customer_name="Customer",
        db=mock_db,
    )
    jc_id = pkg.job_card.id
    assert pkg.drill_sheet.version == 1
    assert pkg.process_sequence_sheet.version == 1

    # Update Drill Details Sheet in draft
    updated_drill = await update_draft_drill_sheet(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        patch={
            "primary_drill": [{"tool_no": "T01", "drill_size_mm": 0.9, "hole_type": "PTH"}],
            "setup": {"stack_height": 2, "drill_program_name": "PROG_A.DRL"},
        },
        expected_version=1,
        db=mock_db,
    )
    assert updated_drill.version == 2
    assert updated_drill.primary_drill[0].drill_size_mm == 0.9
    assert updated_drill.setup.stack_height == 2

    # Stale drill sheet version -> 409
    with pytest.raises(JobCardLockError) as exc_stale_drill:
        await update_draft_drill_sheet(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            patch={"setup": {"stack_height": 3}},
            expected_version=1,
            db=mock_db,
        )
    assert exc_stale_drill.value.status_code == 409

    # Server-managed field is_locked in drill sheet patch -> 422
    with pytest.raises(JobCardValidationError) as exc_managed:
        await update_draft_drill_sheet(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            patch={"is_locked": True},
            expected_version=2,
            db=mock_db,
        )
    assert exc_managed.value.status_code == 422

    # Update Process Sequence Sheet in draft (stage 1 inactive, stage 2 and 3 active)
    updated_proc = await update_draft_process_sequence_sheet(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        patch={
            "route_name": "Custom 3-Stage Route",
            "processes": [
                {"process_seq": 1, "process_name": "PPC", "is_active": False},
                {"process_seq": 2, "process_name": "CNC Drilling", "is_active": True},
                {"process_seq": 3, "process_name": "Packing", "is_active": True},
            ],
        },
        expected_version=1,
        db=mock_db,
    )
    assert updated_proc.version == 2
    assert updated_proc.route_name == "Custom 3-Stage Route"

    # Invalid process route (duplicate process_seq or 0 active processes) -> 422
    with pytest.raises(JobCardValidationError):
        await update_draft_process_sequence_sheet(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            patch={
                "processes": [
                    {"process_seq": 1, "process_name": "PPC", "is_active": True},
                    {"process_seq": 1, "process_name": "Packing", "is_active": True},
                ]
            },
            expected_version=2,
            db=mock_db,
        )

    with pytest.raises(JobCardValidationError):
        await update_draft_process_sequence_sheet(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            patch={
                "processes": [
                    {"process_seq": 1, "process_name": "PPC", "is_active": False},
                ]
            },
            expected_version=2,
            db=mock_db,
        )


@pytest.mark.asyncio
async def test_full_job_card_lifecycle_and_ecn_revisions(
    mock_db: MockDatabase, standard_user: dict[str, Any]
) -> None:
    pkg = await create_job_card_package(
        company_id="company-alpha",
        user=standard_user,
        job_card_no="JC-LIFECYCLE-01",
        order_id="ORD-1",
        order_no="SO-1",
        part_number="PN-LIFE-1",
        customer_id="C-1",
        customer_name="Customer",
        process_sequence_sheet={
            "route_name": "Selective Route",
            "processes": [
                {"process_seq": 1, "process_name": "PPC", "is_active": False},
                {"process_seq": 2, "process_name": "Sheet Cutting", "is_active": True},
                {"process_seq": 3, "process_name": "Packing", "is_active": True},
            ],
        },
        db=mock_db,
    )
    jc_id = pkg.job_card.id

    # 1. Submit for engineering review (draft -> engineering_review)
    in_review = await submit_job_card_for_review(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        expected_version=1,
        db=mock_db,
    )
    assert in_review.job_card.status == "engineering_review"
    assert in_review.job_card.version == 2

    # Sub-sheet edits are blocked while in engineering_review -> 409
    with pytest.raises(JobCardLockError):
        await update_draft_drill_sheet(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            patch={"setup": {"stack_height": 2}},
            expected_version=1,
            db=mock_db,
        )

    # 2. Return to draft requires a reason >= 3 chars
    with pytest.raises(JobCardValidationError):
        await return_job_card_to_draft(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            expected_version=2,
            reason="  ",
            db=mock_db,
        )

    returned = await return_job_card_to_draft(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        expected_version=2,
        reason="Update drill tolerance on T01 before approval",
        db=mock_db,
    )
    assert returned.job_card.status == "draft"
    assert returned.job_card.version == 3

    # Re-submit for engineering review
    in_review_2 = await submit_job_card_for_review(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        expected_version=3,
        db=mock_db,
    )
    assert in_review_2.job_card.status == "engineering_review"
    assert in_review_2.job_card.version == 4

    # 3. Approve Job Card (engineering_review -> approved, locks DrillDetailsSheet)
    approved = await approve_job_card(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        expected_version=4,
        idempotency_key="idem-approve-lifecycle-0001",
        db=mock_db,
    )
    assert approved.job_card.status == "approved"
    assert approved.job_card.version == 5
    assert approved.job_card.approved_by == standard_user["id"]
    assert approved.job_card.approved_at is not None
    assert approved.drill_sheet.is_locked is True
    assert approved.drill_sheet.version == 2

    # Reopening an approved Job Card via return_job_card_to_draft is strictly forbidden -> 409
    with pytest.raises(JobCardLockError):
        await return_job_card_to_draft(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            expected_version=5,
            reason="Attempt illegal reopen of approved card",
            db=mock_db,
        )

    # 4. Controlled ECN Revision from approved (rev 1 -> superseded, rev 2 -> draft)
    rev2_pkg = await create_job_card_revision(
        company_id="company-alpha",
        job_card_id=jc_id,
        user=standard_user,
        expected_version=5,
        revision_reason="ECN-2026-01: Customer changed finish hole tolerance",
        idempotency_key="idem-revision-lifecycle-0001",
        db=mock_db,
    )
    rev2_id = rev2_pkg.job_card.id
    assert rev2_id != jc_id
    assert rev2_pkg.job_card.revision == 2
    assert rev2_pkg.job_card.revision_code == "REV-02"
    assert rev2_pkg.job_card.status == "draft"
    assert rev2_pkg.job_card.version == 1
    assert rev2_pkg.job_card.previous_revision_id == jc_id
    assert rev2_pkg.job_card.approved_by is None
    assert rev2_pkg.job_card.approved_at is None
    assert rev2_pkg.drill_sheet.is_locked is False
    assert rev2_pkg.drill_sheet.version == 1
    assert rev2_pkg.process_sequence_sheet.current_process_seq is None
    assert rev2_pkg.process_sequence_sheet.version == 1

    # Verify rev 1 is now superseded and points to rev 2
    rev1_after = await get_job_card_package("company-alpha", jc_id, db=mock_db)
    assert rev1_after.job_card.status == "superseded"
    assert rev1_after.job_card.superseded_by_revision_id == rev2_id
    assert rev1_after.job_card.superseded_at is not None

    # ECN revision from draft (rev 2) or superseded (rev 1) is rejected -> 409
    with pytest.raises(JobCardLockError):
        await create_job_card_revision(
            company_id="company-alpha",
            job_card_id=rev2_id,
            user=standard_user,
            expected_version=1,
            revision_reason="Cannot revise draft",
            db=mock_db,
        )
    with pytest.raises(JobCardLockError):
        await create_job_card_revision(
            company_id="company-alpha",
            job_card_id=jc_id,
            user=standard_user,
            expected_version=6,
            revision_reason="Cannot revise superseded",
            db=mock_db,
        )

    # 5. Advance rev 2 through submit -> approve -> release
    rev2_review = await submit_job_card_for_review(
        company_id="company-alpha",
        job_card_id=rev2_id,
        user=standard_user,
        expected_version=1,
        db=mock_db,
    )
    rev2_approved = await approve_job_card(
        company_id="company-alpha",
        job_card_id=rev2_id,
        user=standard_user,
        expected_version=rev2_review.job_card.version,
        idempotency_key="idem-approve-rev2-0001",
        db=mock_db,
    )
    released = await release_job_card(
        company_id="company-alpha",
        job_card_id=rev2_id,
        user=standard_user,
        expected_version=rev2_approved.job_card.version,
        idempotency_key="idem-release-rev2-0001",
        db=mock_db,
    )
    assert released.job_card.status == "released_to_production"
    # Stage 1 was inactive -> status='skipped'; Stage 2 is lowest active -> current_process_seq=2, status='waiting'
    assert released.process_sequence_sheet.current_process_seq == 2
    assert released.process_sequence_sheet.processes[0].status == "skipped"
    assert released.process_sequence_sheet.processes[1].status == "waiting"
    assert released.process_sequence_sheet.processes[2].status == "waiting"

    # Parent Order remains in 'confirmed' status
    order_doc = await mock_db.orders.find_one({"company_id": "company-alpha", "id": "ORD-1"})
    assert order_doc["status"] == "confirmed"

    # ECN revision from released_to_production is rejected in Day 1.9 -> 409
    with pytest.raises(JobCardLockError):
        await create_job_card_revision(
            company_id="company-alpha",
            job_card_id=rev2_id,
            user=standard_user,
            expected_version=released.job_card.version,
            revision_reason="Cannot revise released card in Day 1.9",
            db=mock_db,
        )


