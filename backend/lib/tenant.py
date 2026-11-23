"""Resolve identity and company context exclusively from a valid session membership."""

from datetime import datetime, timezone

from fastapi import HTTPException, Request

from lib.db import db


def _not_expired(value: str | None, detail: str, status_code: int) -> None:
    if not value:
        return
    try:
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=status_code, detail="invalid expiry") from exc
    if expiry <= datetime.now(timezone.utc):
        raise HTTPException(status_code=status_code, detail=detail)


async def get_company_context(request: Request) -> dict:
    cached = getattr(request.state, "company_context", None)
    if cached:
        return cached

    from lib.authz import decode_access_token, get_bearer_token

    payload = decode_access_token(get_bearer_token(request))
    user_id, session_id = payload.get("sub"), payload.get("session_id")
    company_id = payload.get("company_id")
    if not user_id or not session_id or not company_id:
        raise HTTPException(status_code=401, detail="token missing required identity claims")

    session = await db.sessions.find_one(
        {"session_id": session_id, "user_id": user_id, "company_id": company_id, "status": "active"},
        {"_id": 0},
    )
    if not session:
        raise HTTPException(status_code=401, detail="session is missing, revoked or inactive")
    _not_expired(session.get("expires_at"), "session expired", 401)

    user = await db.users.find_one({"id": user_id, "status": "active"}, {"_id": 0})
    if not user:
        raise HTTPException(status_code=401, detail="user is not active")
    if payload.get("auth_version", 1) != user.get("auth_version", 1):
        raise HTTPException(status_code=401, detail="session has been revoked")

    membership = await db.company_members.find_one(
        {"company_id": company_id, "user_id": user_id, "status": "active"}, {"_id": 0}
    )
    if not membership:
        raise HTTPException(status_code=403, detail="active company membership required")
    _not_expired(membership.get("expires_at"), "company membership expired", 403)

    # Tenant role and department authority comes from this active membership, never
    # from a global user role or a client-supplied company identifier.
    active_user = {
        **user,
        "company_id": company_id,
        "membership_id": membership.get("id"),
        "role": membership.get("role"),
        "department": membership.get("department"),
        "department_ids": membership.get("department_ids") or ([membership["department"]] if membership.get("department") else []),
        "permissions_override": membership.get("permissions_override", []),
        "cross_department_permissions": membership.get("cross_department_permissions", []),
    }
    context = {"company_id": company_id, "user": active_user, "membership": membership, "session": session, "token": payload}
    request.state.company_context = context
    return context


async def get_company_id(request: Request) -> str:
    return (await get_company_context(request))["company_id"]
