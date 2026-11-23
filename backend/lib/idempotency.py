"""Tenant-scoped idempotency for transactional operational mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pymongo.errors import DuplicateKeyError

from lib.audit import AuditPersistenceError, write_audit_event
from lib.db import client, db


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _payload_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return _digest(canonical.encode("utf-8"))


def _check_replay(record: dict, *, actor_id: str, operation: str, payload_hash: str) -> Any:
    if (record.get("actor_id") != actor_id or record.get("operation") != operation
            or record.get("payload_hash") != payload_hash):
        raise HTTPException(status_code=409, detail="idempotency key was already used for another request")
    return record["result"]


async def run_idempotent_mutation(
    *,
    company_id: str,
    actor_id: str,
    operation: str,
    key: str,
    payload: Any,
    mutate: Callable[[Any], Awaitable[Any]],
    user: dict,
    request: Request,
    audit_action: str | None = None,
    audit_resource_type: str | None = None,
) -> Any:
    """Commit a mutation, required audit event and replay result together.

    The raw key is never stored. Completed records are retained indefinitely so
    a delayed client retry cannot repeat an operational effect. A retention
    policy requires an explicit product/operations decision.
    """
    if not key or len(key) < 16 or len(key) > 200 or key.strip() != key:
        raise HTTPException(status_code=422, detail="Idempotency-Key must be 16 to 200 non-padding characters")
    key_hash = _digest(key.encode("utf-8"))
    payload_hash = _payload_digest(payload)
    query = {"company_id": company_id, "key_hash": key_hash}
    audit_event_id = _digest(f"factoryos-audit:{company_id}:{key_hash}".encode("utf-8"))

    async def persist_audit(session):
        await write_audit_event(
            session=session, event_id=audit_event_id,
            action=audit_action or f"mutation.{operation}",
            resource_type=audit_resource_type or operation.split(":", 1)[0].split(".", 1)[0],
            resource_id=operation,
            result="success", company_id=company_id, user=user,
            request_id=getattr(request.state, "request_id", None),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            after_state={"operation": operation, "payload_hash": payload_hash},
        )

    async def execute(session):
        existing = await db.idempotency_records.find_one(query, {"_id": 0}, session=session)
        if existing:
            result = _check_replay(existing, actor_id=actor_id, operation=operation, payload_hash=payload_hash)
            existing_audit = None
            if existing.get("audit_event_id") == audit_event_id:
                existing_audit = await db.audit_logs.find_one({"_id": audit_event_id}, {"_id": 1}, session=session)
            if not existing_audit:
                # Repair records created by the earlier pilot before audit was
                # transactionally associated. This does not claim retroactive
                # atomicity, but makes the missing event visible and durable now.
                await persist_audit(session)
                await db.idempotency_records.update_one(
                    query, {"$set": {"audit_event_id": audit_event_id}}, session=session,
                )
            return result

        result = await mutate(session)
        encoded_result = jsonable_encoder(result)
        await persist_audit(session)
        await db.idempotency_records.insert_one({
            **query,
            "actor_id": actor_id,
            "operation": operation,
            "payload_hash": payload_hash,
            "result": encoded_result,
            "audit_event_id": audit_event_id,
            "completed_at": datetime.now(timezone.utc),
        }, session=session)
        return encoded_result

    try:
        async with await client.start_session() as session:
            result = await session.with_transaction(execute)
            request.state.transactional_audit_committed = True
            return result
    except AuditPersistenceError:
        raise HTTPException(status_code=503,
                            detail="required audit persistence failed; the mutation was not applied") from None
    except DuplicateKeyError:
        # A simultaneous request with the same tenant key may win the unique
        # insert while this transaction is running. Read its committed result
        # only after the failed transaction has ended.
        existing = await db.idempotency_records.find_one(query, {"_id": 0})
        if existing:
            result = _check_replay(existing, actor_id=actor_id, operation=operation, payload_hash=payload_hash)
            request.state.transactional_audit_committed = existing.get("audit_event_id") == audit_event_id
            return result
        raise
