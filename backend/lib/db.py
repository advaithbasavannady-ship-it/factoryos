"""Shared Mongo handle — import `client`/`db` from here (server.py, routers, seed.py).

Phase 1.1 intentionally does not create production indexes or migrations. This module
contains the shared DB handle and the security-ready index definitions for the auth
session foundation, but it does not run any index creation automatically.
"""

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING, DESCENDING, IndexModel

load_dotenv(Path(__file__).parent.parent / ".env")

mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]

logger = logging.getLogger(__name__)


def _idx(*keys: str, unique: bool = False, name: str | None = None) -> IndexModel:
    return IndexModel([(k, ASCENDING) for k in keys], name=name or "_".join(keys), unique=unique)


# Phase 1.1 security indexes: not created automatically yet; these are the structures
# to be validated in the isolated test database before any production-safe creation.
AUTH_INDEXES: dict[str, list[IndexModel]] = {
    "users": [
        _idx("email", unique=True, name="email_unique"),
        IndexModel([("email_normalized", ASCENDING)], name="email_normalized_unique", unique=True,
                   partialFilterExpression={"email_normalized": {"$type": "string"}}),
        _idx("status", name="status_idx"),
        IndexModel([("password_reset_token_hash", ASCENDING)], name="password_reset_token_unique", unique=True,
                   sparse=True),
    ],
    "company_members": [
        _idx("company_id", "user_id", unique=True, name="company_user_unique"),
        _idx("company_id", "role", name="company_role_idx"),
        _idx("company_id", "status", name="company_status_idx"),
    ],
    "sessions": [
        _idx("session_id", unique=True, name="session_unique"),
        _idx("user_id", "company_id", name="user_company_idx"),
        _idx("expires_at", name="session_expiry_idx"),
        IndexModel([("refresh_token_hash", ASCENDING)], name="session_refresh_hash_unique", unique=True,
                   sparse=True),
        _idx("user_id", "status", name="user_session_status_idx"),
    ],
    "refresh_requests": [
        _idx("request_id_hash", unique=True, name="refresh_request_id_unique"),
        _idx("session_id", "created_at", name="refresh_request_session_time_idx"),
        IndexModel([("cleanup_at", ASCENDING)], name="refresh_request_cleanup_ttl", expireAfterSeconds=0),
    ],
    "refresh_tokens": [
        _idx("token_hash", unique=True, name="refresh_hash_unique"),
        _idx("user_id", "company_id", name="refresh_user_company_idx"),
        _idx("status", name="refresh_status_idx"),
        _idx("expires_at", name="refresh_expiry_idx"),
    ],
    "password_reset_tokens": [
        _idx("token_hash", unique=True, name="reset_hash_unique"),
        _idx("user_id", name="reset_user_idx"),
        _idx("expires_at", name="reset_expiry_idx"),
    ],
    "audit_logs": [
        _idx("tenant_id", "timestamp", name="audit_tenant_time_idx"),
        _idx("user_id", "timestamp", name="audit_user_time_idx"),
        _idx("tenant_id", "resource_type", "resource_id", "timestamp", name="audit_resource_time_idx"),
    ],
    "rate_limits": [
        IndexModel([("expires_at", ASCENDING)], name="auth_rate_limit_expiry", expireAfterSeconds=0),
    ],
}

# Existing business collections remain untouched in this Phase 1.1 scope.
LEGACY_INDEXES: dict[str, list[IndexModel]] = {
    "companies": [_idx("id", unique=True)],
    "customers": [_idx("company_id", "id", unique=True), _idx("company_id", "name")],
    "orders": [_idx("company_id", "id", unique=True), _idx("company_id", "order_date"), _idx("company_id", "status")],
    "employees": [_idx("company_id", "id", unique=True), _idx("company_id", "emp_code")],
    "attendance": [_idx("company_id", "date", "employee_id"), _idx("company_id", "employee_id", "date")],
    "production": [_idx("company_id", "date", "machine_id")],
    "machines": [_idx("company_id", "id", unique=True), _idx("company_id", "code")],
    "maintenance": [_idx("company_id", "machine_id", "date"), _idx("company_id", "date")],
    "inventory": [_idx("company_id", "id", unique=True), _idx("company_id", "sku")],
    "suppliers": [_idx("company_id", "id", unique=True)],
    "purchases": [_idx("company_id", "id", unique=True), _idx("company_id", "order_date")],
    "consumption": [_idx("company_id", "item_id", "date"), _idx("company_id", "date")],
    "events": [_idx("company_id", "id", unique=True), _idx("company_id", "date", "start_time")],
    "conversations": [_idx("company_id", "id", unique=True), IndexModel([("company_id", ASCENDING), ("updated_at", DESCENDING)], name="company_updated")],
    "meta": [_idx("company_id", unique=True)],
    "notifications": [IndexModel([("company_id", ASCENDING), ("recipient_roles", ASCENDING), ("created_at", DESCENDING)], name="company_roles_created")],
    "idempotency_records": [_idx("company_id", "key_hash", unique=True, name="tenant_idempotency_key_unique")],
}

# Day 1.7D / Day 1.9 Job Card tri-document unique indexes: applied explicitly after duplicate
# preflight validation in the isolated target database, never automatically at startup.
ACTIVE_JOB_CARD_STATUSES: tuple[str, ...] = (
    "draft",
    "engineering_review",
    "approved",
    "released_to_production",
)
VALID_JOB_CARD_STATUSES: tuple[str, ...] = (*ACTIVE_JOB_CARD_STATUSES, "superseded")

JOB_CARD_INDEXES: dict[str, list[IndexModel]] = {
    "job_cards": [
        _idx("company_id", "job_card_no", "revision", unique=True, name="company_job_card_no_revision_unique"),
        _idx("company_id", "id", unique=True, name="company_job_card_id_unique"),
        IndexModel(
            [("company_id", ASCENDING), ("order_id", ASCENDING)],
            unique=True,
            name="company_active_order_job_card_unique",
            partialFilterExpression={"status": {"$in": list(ACTIVE_JOB_CARD_STATUSES)}},
        ),
    ],
    "drill_detail_sheets": [
        _idx("company_id", "job_card_id", unique=True, name="company_drill_job_card_unique"),
        _idx("company_id", "id", unique=True, name="company_drill_sheet_id_unique"),
    ],
    "process_sequence_sheets": [
        _idx("company_id", "job_card_id", unique=True, name="company_process_job_card_unique"),
        _idx("company_id", "id", unique=True, name="company_process_sheet_id_unique"),
    ],
}

SECURITY_TEST_DB_NAME = "factoryos_security_test"


def _require_security_test_db(target_db=None):
    """Fail closed unless both configured DB_NAME and target database are factoryos_security_test."""
    database = db if target_db is None else target_db
    env_db_name = os.environ.get("DB_NAME", "").strip()
    actual_db_name = getattr(database, "name", None)
    if env_db_name != SECURITY_TEST_DB_NAME or actual_db_name != SECURITY_TEST_DB_NAME:
        raise RuntimeError(
            f"Refusing index operation outside '{SECURITY_TEST_DB_NAME}' "
            f"(DB_NAME={env_db_name!r}, db.name={actual_db_name!r})"
        )
    return database


async def ensure_indexes() -> None:
    # Phase 1.1 must not create production indexes automatically. This function is left
    # as a placeholder for later explicit, controlled index creation after validation.
    return None


async def apply_auth_indexes_after_preflight() -> dict[str, list[str]]:
    """Explicit index migration helper. Never called at app startup.

    Operators must first run the duplicate preflight in the isolated target DB,
    review results, and select the intended database explicitly.
    """
    duplicates = await auth_index_duplicate_preflight()
    if duplicates:
        raise RuntimeError(f"auth index preflight found duplicate keys: {duplicates}")
    created: dict[str, list[str]] = {}
    for collection_name, indexes in AUTH_INDEXES.items():
        created[collection_name] = await db[collection_name].create_indexes(indexes)
    return created


async def auth_index_duplicate_preflight() -> dict[str, list[dict]]:
    """Read-only duplicate check for unique auth indexes; explicit operator use only."""
    duplicates: dict[str, list[dict]] = {}
    for collection_name, indexes in AUTH_INDEXES.items():
        for index in indexes:
            spec = index.document
            if not spec.get("unique"):
                continue
            keys = list(spec["key"].keys())
            match: dict = {}
            if spec.get("partialFilterExpression"):
                match.update(spec["partialFilterExpression"])
            elif spec.get("sparse") and len(keys) == 1:
                match[keys[0]] = {"$exists": True}
            group_id = {key: f"${key}" for key in keys}
            pipeline = []
            if match:
                pipeline.append({"$match": match})
            pipeline.extend([
                {"$group": {"_id": group_id, "count": {"$sum": 1}}},
                {"$match": {"count": {"$gt": 1}}},
                {"$limit": 10},
            ])
            rows = await db[collection_name].aggregate(pipeline).to_list(10)
            if rows:
                duplicates.setdefault(collection_name, []).append({"index": spec["name"], "samples": rows})
    return duplicates


def _build_key_anomaly_expression(key: str) -> dict:
    """Build a read-only aggregation expression returning an anomaly category or None."""
    field_ref = f"${key}"
    type_expr = {"$type": field_ref}
    if key == "revision":
        return {
            "$switch": {
                "branches": [
                    {"case": {"$eq": [type_expr, "missing"]}, "then": "invalid_revision:missing_field"},
                    {"case": {"$eq": [type_expr, "null"]}, "then": "invalid_revision:null_value"},
                    {"case": {"$eq": [type_expr, "array"]}, "then": "invalid_revision:array_valued_key"},
                    {
                        "case": {"$not": [{"$in": [type_expr, ["int", "long"]]}]},
                        "then": "invalid_revision:wrong_bson_type",
                    },
                    {"case": {"$lt": [field_ref, 1]}, "then": "invalid_revision:below_minimum"},
                ],
                "default": None,
            }
        }
    if key == "status":
        return {
            "$switch": {
                "branches": [
                    {"case": {"$eq": [type_expr, "missing"]}, "then": "invalid_status:missing_field"},
                    {"case": {"$eq": [type_expr, "null"]}, "then": "invalid_status:null_value"},
                    {"case": {"$eq": [type_expr, "array"]}, "then": "invalid_status:array_valued_key"},
                    {"case": {"$ne": [type_expr, "string"]}, "then": "invalid_status:wrong_bson_type"},
                    {"case": {"$eq": [field_ref, ""]}, "then": "invalid_status:empty_string"},
                    {
                        "case": {"$eq": [{"$trim": {"input": field_ref}}, ""]},
                        "then": "invalid_status:whitespace_only_string",
                    },
                    {
                        "case": {"$not": [{"$in": [field_ref, list(VALID_JOB_CARD_STATUSES)]}]},
                        "then": "invalid_status:unexpected_value",
                    },
                ],
                "default": None,
            }
        }
    return {
        "$switch": {
            "branches": [
                {"case": {"$eq": [type_expr, "missing"]}, "then": "missing_field"},
                {"case": {"$eq": [type_expr, "null"]}, "then": "null_value"},
                {"case": {"$eq": [type_expr, "array"]}, "then": "array_valued_key"},
                {"case": {"$ne": [type_expr, "string"]}, "then": "wrong_bson_type"},
                {"case": {"$eq": [field_ref, ""]}, "then": "empty_string"},
                {"case": {"$eq": [{"$trim": {"input": field_ref}}, ""]}, "then": "whitespace_only_string"},
            ],
            "default": None,
        }
    }


async def job_card_index_duplicate_preflight(target_db=None) -> dict[str, list[dict]]:
    """Read-only duplicate and key-integrity check for all seven unique Job Card indexes.

    Fails closed unless targeting factoryos_security_test. Normalizes missing and
    explicit null fields identically (via $ifNull) to match MongoDB unique B-tree
    index semantics, honours partialFilterExpression for duplicate detection on partial
    unique indexes while scanning the full collection for key/status anomalies, and
    detects missing fields, null values, empty or whitespace-only strings, wrong BSON
    types, array-valued fields, invalid revision values, and invalid status values
    independently for each index without modifying records or exposing non-key fields.
    """
    database = _require_security_test_db(target_db)
    issues: dict[str, list[dict]] = {}
    for collection_name, indexes in JOB_CARD_INDEXES.items():
        for index in indexes:
            spec = index.document
            if not spec.get("unique"):
                continue
            keys = list(spec["key"].keys())
            partial_filter = spec.get("partialFilterExpression")
            group_id = {key: {"$ifNull": [f"${key}", None]} for key in keys}
            dup_pipeline: list[dict] = []
            if partial_filter:
                dup_pipeline.append({"$match": partial_filter})
            dup_pipeline.extend([
                {"$group": {"_id": group_id, "count": {"$sum": 1}}},
                {"$match": {"count": {"$gt": 1}}},
                {"$limit": 10},
            ])
            dup_rows = await database[collection_name].aggregate(dup_pipeline).to_list(10)

            anomaly_keys = list(keys)
            if isinstance(partial_filter, dict):
                for filter_key in partial_filter:
                    if not filter_key.startswith("$") and filter_key not in anomaly_keys:
                        anomaly_keys.append(filter_key)

            anomaly_checks = [
                {
                    "field": key,
                    "category": _build_key_anomaly_expression(key),
                    "bson_type": {"$type": f"${key}"},
                }
                for key in anomaly_keys
            ]
            anomaly_pipeline = [
                {
                    "$project": {
                        "_id": 0,
                        "key_values": {key: {"$ifNull": [f"${key}", None]} for key in anomaly_keys},
                        "anomalies": {
                            "$filter": {
                                "input": anomaly_checks,
                                "as": "item",
                                "cond": {"$ne": ["$$item.category", None]},
                            }
                        },
                    }
                },
                {"$match": {"anomalies.0": {"$exists": True}}},
                {"$limit": 10},
            ]
            anomaly_rows = await database[collection_name].aggregate(anomaly_pipeline).to_list(10)

            if dup_rows or anomaly_rows:
                entry: dict = {"index": spec["name"], "keys": keys}
                if dup_rows:
                    entry["samples"] = dup_rows
                    entry["duplicate_samples"] = dup_rows
                if anomaly_rows:
                    entry["anomalies"] = anomaly_rows
                issues.setdefault(collection_name, []).append(entry)
    return issues


async def apply_job_card_indexes_after_preflight(target_db=None) -> dict[str, list[str]]:
    """Explicit Job Card unique index migration helper. Never called at app startup.

    Verifies DB_NAME == 'factoryos_security_test', checks all six unique indexes
    for existing duplicates or malformed indexed keys, fails closed without modifying
    records if any conflict or anomaly exists, and applies indexes idempotently via
    Motor's create_indexes().
    """
    database = _require_security_test_db(target_db)
    duplicates = await job_card_index_duplicate_preflight(database)
    if duplicates:
        raise RuntimeError(f"job card index preflight found duplicate or malformed keys: {duplicates}")
    created: dict[str, list[str]] = {}
    for collection_name, indexes in JOB_CARD_INDEXES.items():
        created[collection_name] = await database[collection_name].create_indexes(indexes)
    return created
