"""Append-only application audit event writer.

Calls made inside a Mongo transaction are required writes: any error is raised
so the caller's business transaction aborts. Non-transactional legacy calls are
best-effort and remain explicitly outside the durability guarantee.
"""

import hashlib
import logging
from typing import Any

from fastapi import HTTPException, Request
from pymongo.client_session import ClientSession

from models.core import AuditEvent, new_id

logger = logging.getLogger(__name__)


class AuditPersistenceError(RuntimeError):
    """A required transactional audit event could not be persisted."""


def _digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None


async def write_audit_event(
    *, action: str, resource_type: str, result: str, company_id: str | None = None,
    user: dict | None = None, resource_id: str | None = None, request_id: str | None = None,
    ip: str | None = None, user_agent: str | None = None, reason: str | None = None,
    before_state: dict[str, Any] | None = None, after_state: dict[str, Any] | None = None,
    session: ClientSession | None = None, event_id: str | None = None,
) -> None:
    """Insert a sanitized audit event. Callers must never pass credentials or token values."""
    from lib.db import db

    event = AuditEvent(
        id=event_id or new_id(),
        tenant_id=company_id or "system",
        user_id=(user or {}).get("id"),
        membership_id=(user or {}).get("membership_id"),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        request_id=request_id,
        ip_hash=_digest(ip),
        user_agent_hash=_digest(user_agent),
        reason=reason,
        before_state=before_state,
        after_state=after_state,
    )
    document = {**event.model_dump(), "_id": event.id}
    if session is not None:
        # Required audit: propagate failures so the encompassing business
        # transaction cannot commit without this event.
        try:
            await db.audit_logs.insert_one(document, session=session)
        except Exception:
            logger.exception("required transactional audit insert failed action=%s resource=%s",
                             action, resource_type)
            raise AuditPersistenceError("required audit event insert failed") from None
        return

    try:
        await db.audit_logs.insert_one(document)
    except Exception:
        logger.exception("best-effort audit event insert failed action=%s resource=%s", action, resource_type)


async def run_audited_mutation(
    *, company_id: str, user: dict, request: Request, action: str,
    resource_type: str, resource_id: str | None, mutate,
    after_state: dict[str, Any] | None = None,
) -> Any:
    """Commit a critical business write and its required audit event together."""
    from lib.db import client

    event_id = new_id()

    async def execute(session: ClientSession):
        result = await mutate(session)
        await write_audit_event(
            session=session, event_id=event_id, action=action,
            resource_type=resource_type, resource_id=resource_id,
            result="success", company_id=company_id, user=user,
            request_id=getattr(request.state, "request_id", None),
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            after_state=after_state or {"mutation": action, "resource_id": resource_id},
        )
        return result

    try:
        async with await client.start_session() as session:
            result = await session.with_transaction(execute)
    except AuditPersistenceError:
        raise HTTPException(status_code=503,
                            detail="required audit persistence failed; the mutation was not applied") from None
    request.state.transactional_audit_committed = True
    return result
