"""Authentication and authorization helpers for FactoryOS.

This module provides the stage-3 security foundation for JWT bearer auth and
session validation. It intentionally contains no demo-owner fallback,
no default tenant fallback, and no impersonation flow.
"""

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from fastapi import Depends, HTTPException, Request
from passlib.context import CryptContext

from lib.db import db
from lib.permissions import ROLE_ALIASES, has_permission
from lib.tenant import get_company_context, get_company_id

def _validated_jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET")
    if secret is None:
        raise RuntimeError("JWT_SECRET is missing")

    candidate = secret.strip()
    if not candidate:
        raise RuntimeError("JWT_SECRET is empty")

    placeholder_values = {
        "dev-change-me",
        "change-me",
        "example-secret",
        "your-jwt-secret",
        "your-secret-key",
    }
    if candidate.lower() in placeholder_values:
        raise RuntimeError("JWT_SECRET uses a known placeholder value")

    if len(candidate.encode("utf-8")) < 32:
        raise RuntimeError("JWT_SECRET must be at least 32 bytes")

    return candidate


JWT_SECRET = _validated_jwt_secret()
JWT_ALGORITHM = "HS256"
JWT_ISSUER = os.environ.get("JWT_ISSUER", "factoryos")
JWT_AUDIENCE = os.environ.get("JWT_AUDIENCE", "factoryos")
ACCESS_TOKEN_TTL_SECONDS = int(os.environ.get("ACCESS_TOKEN_TTL_SECONDS", "900"))
REFRESH_TOKEN_TTL_SECONDS = int(os.environ.get("REFRESH_TOKEN_TTL_SECONDS", "2592000"))
REFRESH_COOKIE_NAME = "factoryos_refresh"
COOKIE = REFRESH_COOKIE_NAME

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

APPROVERS = {"owner", "manager"}
PLANNERS = {"owner", "production_manager"}
PRODUCERS = {"owner", "production_manager", "supervisor"}
QC = {"owner", "qc_inspector"}
READY = {"owner", "manager", "qc_inspector", "dispatch_clerk"}
DISPATCH = {"owner", "manager", "dispatch_clerk"}
PAYMENTS = {"owner", "manager", "sales_staff"}
EDITORS = {"owner", "manager", "sales_staff"}
SETTINGS = {"owner", "manager", "admin", "administrator"}

ROLE_LABELS = {
    "owner": "Owner",
    "manager": "Operations manager",
    "production_manager": "Production manager",
    "supervisor": "Department supervisor",
    "qc_inspector": "QC inspector",
    "dispatch_clerk": "Dispatch clerk",
    "sales_staff": "Sales staff",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    """Hash a user password with the approved password library."""
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a submitted password against its stored hash."""
    return pwd_context.verify(password, password_hash)


def hash_refresh_token(token: str) -> str:
    """Return a canonical SHA-256 hash for an opaque refresh token."""
    if not token:
        raise ValueError("refresh token is required")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_refresh_token() -> str:
    """Generate a cryptographically secure opaque refresh token."""
    return secrets.token_urlsafe(32)


def build_refresh_token_family_id() -> str:
    """Generate a new opaque family identifier for refresh rotation."""
    return secrets.token_urlsafe(16)


def sign_access_token(
    user_id: str,
    company_id: str,
    session_id: str,
    role: str | None = None,
    auth_version: int = 1,
    *,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
    issuer: str = JWT_ISSUER,
    audience: str = JWT_AUDIENCE,
) -> str:
    if not user_id or not company_id or not session_id:
        raise ValueError("user_id, company_id and session_id are required to sign the token")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "company_id": company_id,
        "session_id": session_id,
        "role": role,
        "auth_version": auth_version,
        "iss": issuer,
        "aud": audience,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, *, issuer: str = JWT_ISSUER, audience: str = JWT_AUDIENCE) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "nbf", "sub", "session_id", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid access token") from exc


def get_bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization")
    if not auth:
        raise HTTPException(status_code=401, detail="missing authorization header")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="invalid authorization scheme")
    return token


async def require_active_session(request: Request) -> dict[str, Any]:
    """Validate the JWT and the active database-backed session for the authenticated user."""
    context = await get_company_context(request)
    return {"user": context["user"], "membership": context["membership"],
            "session": context["session"], "token": context["token"]}


async def current_user(request: Request, cid: str = Depends(get_company_id)) -> dict:
    """Return the authenticated user only after JWT, session and membership validation succeeds."""
    context = await get_company_context(request)
    if context["company_id"] != cid:
        raise HTTPException(status_code=403, detail="company context mismatch")
    return context["user"]


def require(user: dict, roles: set[str], action: str, department: str | None = None) -> None:
    role = user.get("role")
    canonical = ROLE_ALIASES.get(role, role)
    allowed = any(role == value or canonical == ROLE_ALIASES.get(value, value) for value in roles)
    if not allowed:
        allowed = ", ".join(ROLE_LABELS[r] for r in sorted(roles))
        raise HTTPException(
            403,
            f"{user.get('name', 'User')} ({ROLE_LABELS.get(user.get('role'), user.get('role'))}) is not allowed to {action}. Allowed: {allowed}.",
        )
    departments = user.get("department_ids") or ([user.get("department")] if user.get("department") else [])
    if department and canonical in {"department_manager", "employee"} and department not in departments:
        raise HTTPException(
            403,
            f"{user.get('name', 'User')} is not assigned to {department} and cannot {action} there.",
        )


def require_permission(user: dict, permission: str, department: str | None = None) -> None:
    from lib.permissions import require_permission as enforce

    enforce(user, permission, department_id=department)


def require_any_permission(user: dict, *permissions: str) -> None:
    if not any(has_permission(user, permission) for permission in permissions):
        raise HTTPException(status_code=403, detail="permission denied")


def actor(user: dict) -> dict:
    return {"user_id": user["id"], "name": user["name"], "role": user["role"]}
