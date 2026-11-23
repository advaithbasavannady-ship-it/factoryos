"""Login, refresh rotation, company selection, logout, and password reset APIs."""

import base64
import hashlib
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from lib.audit import AuditPersistenceError, run_audited_mutation, write_audit_event
from lib.authz import (
    ACCESS_TOKEN_TTL_SECONDS, REFRESH_COOKIE_NAME, REFRESH_TOKEN_TTL_SECONDS,
    JWT_SECRET, build_refresh_token, decode_access_token, get_bearer_token, hash_password,
    hash_refresh_token, sign_access_token, verify_password,
)
from lib.db import client, db
from lib.mailer import send_membership_invitation, send_password_reset_email
from lib.permissions import ROLE_PERMISSIONS, ROLE_ALIASES, require_permission
from lib.tenant import get_company_context
from lib.authz import current_user
from lib.tenant import get_company_id
from lib.permissions import ROLE_PERMISSIONS, has_permission
from lib.rate_limit import consume_rate_limit

router = APIRouter(prefix="/auth")
logger = logging.getLogger(__name__)
RESET_TTL_SECONDS = 1800
RATE_WINDOW_SECONDS = 900
REFRESH_DUPLICATE_WINDOW_SECONDS = 30
REFRESH_REQUEST_CLEANUP_SECONDS = 3600
_REFRESH_RESULT_KEY = HKDF(
    algorithm=hashes.SHA256(), length=32, salt=b"factoryos-refresh-result-v1",
    info=b"factoryos refresh duplicate response encryption",
).derive(JWT_SECRET.encode("utf-8"))


class _RefreshRace(Exception):
    """The presented refresh token lost its rotation compare-and-swap."""


def _refresh_request_aad(record: dict) -> bytes:
    bound = {key: record[key] for key in (
        "request_id_hash", "session_id", "user_id", "company_id", "previous_token_hash", "successor_token_hash",
    )}
    return json.dumps(bound, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _encrypt_refresh_result(record: dict, result: dict) -> dict:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_REFRESH_RESULT_KEY).encrypt(
        nonce, json.dumps(result, separators=(",", ":")).encode("utf-8"), _refresh_request_aad(record),
    )
    return {"result_nonce": base64.b64encode(nonce).decode("ascii"),
            "result_ciphertext": base64.b64encode(ciphertext).decode("ascii")}


def _decrypt_refresh_result(record: dict) -> dict:
    nonce = base64.b64decode(record["result_nonce"], validate=True)
    ciphertext = base64.b64decode(record["result_ciphertext"], validate=True)
    plaintext = AESGCM(_REFRESH_RESULT_KEY).decrypt(nonce, ciphertext, _refresh_request_aad(record))
    value = json.loads(plaintext)
    if not isinstance(value, dict) or not isinstance(value.get("body"), dict) or not isinstance(value.get("refresh_token"), str):
        raise ValueError("invalid encrypted refresh result")
    return value


def _refresh_expiry(session: dict) -> datetime | None:
    value = session.get("refresh_expires_at") or session.get("expires_at")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (TypeError, ValueError):
        return None


def _stored_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _validate_password_bytes(value: str | None) -> str | None:
    if value is not None and len(value.encode("utf-8")) > 72:
        raise ValueError("password must be at most 72 UTF-8 bytes")
    return value


def _check_browser_origin(request: Request) -> None:
    """Reject browser requests from untrusted origins; non-browser clients may omit Origin."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    allowed = {value.strip() for value in os.environ.get(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if value.strip()}
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="untrusted request origin")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _cookie_secure() -> bool:
    configured = os.environ.get("AUTH_COOKIE_SECURE")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes"}
    return os.environ.get("ENVIRONMENT", "production").lower() != "development"


def _set_refresh_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        REFRESH_COOKIE_NAME, token, httponly=True, secure=_cookie_secure(),
        samesite="strict", path="/api/auth", max_age=REFRESH_TOKEN_TTL_SECONDS,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/auth", httponly=True,
                           secure=_cookie_secure(), samesite="strict")


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)
    company_id: str | None = Field(default=None, max_length=128)

    @field_validator("password")
    @classmethod
    def password_size(cls, value: str) -> str:
        return _validate_password_bytes(value) or value


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=12, max_length=1024)

    @field_validator("new_password")
    @classmethod
    def password_size(cls, value: str) -> str:
        return _validate_password_bytes(value) or value


class CompanySwitchIn(BaseModel):
    company_id: str = Field(min_length=1, max_length=128)


class MembershipUpdateIn(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|suspended|revoked)$")
    role: str | None = Field(default=None, max_length=64)
    department: str | None = Field(default=None, max_length=128)
    department_ids: list[str] | None = None
    permissions_override: list[str] | None = None


class InvitationIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=64)
    department: str | None = Field(default=None, max_length=128)


class InvitationAcceptIn(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    new_password: str | None = Field(default=None, min_length=12, max_length=1024)

    @field_validator("new_password")
    @classmethod
    def password_size(cls, value: str | None) -> str | None:
        return _validate_password_bytes(value)


async def _active_memberships(user_id: str) -> list[dict]:
    return await db.company_members.find({"user_id": user_id, "status": "active"}, {"_id": 0}).to_list(100)


async def _issue_session(user: dict, membership: dict, request: Request, response: Response,
                         mongo_session=None) -> dict:
    now = _now()
    session_id = secrets.token_urlsafe(32)
    refresh = build_refresh_token()
    auth_version = int(user.get("auth_version", 1))
    session = {
        "id": secrets.token_urlsafe(16), "session_id": session_id,
        "user_id": user["id"], "company_id": membership["company_id"],
        "issued_at": _iso(now), "expires_at": _iso(now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)),
        "refresh_expires_at": _iso(now + timedelta(seconds=REFRESH_TOKEN_TTL_SECONDS)),
        "refresh_token_hash": hash_refresh_token(refresh), "refresh_token_history": [],
        "refresh_version": 1, "status": "active",
        "auth_version": auth_version,
        "user_agent_hash": hashlib.sha256(request.headers.get("user-agent", "").encode()).hexdigest()
            if request.headers.get("user-agent") else None,
        "ip_hash": hashlib.sha256(request.client.host.encode()).hexdigest()
            if request.client and request.client.host else None,
    }
    await db.sessions.insert_one(session, session=mongo_session)
    access = sign_access_token(user["id"], membership["company_id"], session_id,
                               membership.get("role"), auth_version=auth_version)
    _set_refresh_cookie(response, refresh)
    return {"access_token": access, "token_type": "bearer", "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "user": {"id": user["id"], "name": user.get("name", ""), "email": user.get("email", ""),
                     "company_id": membership["company_id"], "membership_id": membership.get("id"),
                     "role": membership.get("role"), "department": membership.get("department"),
                     "department_ids": membership.get("department_ids") or ([membership["department"]] if membership.get("department") else [])}}


def _smtp_ready() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def _deliver_reset(email: str, token: str) -> None:
    try:
        send_password_reset_email(email, token)
    except Exception:
        logger.exception("password-reset delivery failed")


def _deliver_invitation(email: str, token: str, company_id: str) -> None:
    try:
        send_membership_invitation(email, token, company_id)
    except Exception:
        logger.exception("membership invitation delivery failed")


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response):
    _check_browser_origin(request)
    peer = request.client.host if request.client else "unknown"
    email = body.email.strip().casefold()
    ip_ok, ip_retry = await consume_rate_limit("auth.login.ip", peer, limit=30, window_seconds=RATE_WINDOW_SECONDS)
    email_ok, email_retry = await consume_rate_limit("auth.login.email", email, limit=10, window_seconds=RATE_WINDOW_SECONDS)
    if not ip_ok or not email_ok:
        raise HTTPException(status_code=429, detail="too many sign-in attempts",
                            headers={"Retry-After": str(max(ip_retry if not ip_ok else 0, email_retry if not email_ok else 0))})
    user = await db.users.find_one({"email_normalized": email}, {"_id": 0})
    if not user:
        # Compatibility for existing records pending normalized-email backfill.
        user = await db.users.find_one({"email": body.email.strip()}, {"_id": 0})
    password_ok = False
    if user and user.get("password_hash"):
        try:
            password_ok = verify_password(body.password, user["password_hash"])
        except (ValueError, TypeError):
            password_ok = False
    if not user or not password_ok or user.get("status") != "active":
        await write_audit_event(action="auth.login", resource_type="session", result="failure",
                                user=user, request_id=getattr(request.state, "request_id", None),
                                ip=request.client.host if request.client else None,
                                user_agent=request.headers.get("user-agent"))
        raise HTTPException(status_code=401, detail="invalid email or password")

    memberships = await _active_memberships(user["id"])
    if body.company_id:
        memberships = [m for m in memberships if m.get("company_id") == body.company_id]
    if not memberships:
        raise HTTPException(status_code=403, detail="active company membership required")
    if len(memberships) > 1:
        raise HTTPException(status_code=409, detail={
            "code": "company_selection_required",
            "memberships": [{"company_id": m["company_id"], "membership_id": m.get("id"), "role": m.get("role")} for m in memberships],
        })

    membership = memberships[0]
    async def persist(mongo_session):
        result = await _issue_session(user, membership, request, response, mongo_session=mongo_session)
        await db.users.update_one({"id": user["id"]}, {"$set": {"last_login_at": _iso(_now())}},
                                  session=mongo_session)
        return result

    result = await run_audited_mutation(
        company_id=membership["company_id"], user={**user, "membership_id": membership.get("id")},
        request=request, action="auth.login", resource_type="session", resource_id=None,
        mutate=persist, after_state={"status": "active", "company_id": membership["company_id"]},
    )
    return result


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    refresh_request_id: str = Header(..., alias="X-Refresh-Request-ID"),
):
    _check_browser_origin(request)
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise HTTPException(status_code=401, detail="refresh session required")
    try:
        parsed_request_id = uuid.UUID(refresh_request_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=422, detail="X-Refresh-Request-ID must be a UUIDv4") from None
    if parsed_request_id.version != 4 or str(parsed_request_id) != refresh_request_id.lower():
        raise HTTPException(status_code=422, detail="X-Refresh-Request-ID must be a UUIDv4")

    token_hash = hash_refresh_token(raw)
    request_id_hash = hashlib.sha256(refresh_request_id.lower().encode("ascii")).hexdigest()
    now = _now()

    async def rotate(session):
        prior_request = await db.refresh_requests.find_one(
            {"request_id_hash": request_id_hash}, {"_id": 0}, session=session,
        )
        if prior_request:
            return {"kind": "duplicate", "record": prior_request}

        current = await db.sessions.find_one({"refresh_token_hash": token_hash}, {"_id": 0}, session=session)
        if not current:
            return {"kind": "not_current"}
        if current.get("status") != "active":
            return {"kind": "inactive", "session": current}

        expiry = _refresh_expiry(current)
        if expiry is None or expiry <= now:
            await db.sessions.update_one(
                {"session_id": current["session_id"], "status": "active", "refresh_token_hash": token_hash},
                {"$set": {"status": "expired", "expired_at": now}, "$unset": {"refresh_token_hash": ""}},
                session=session,
            )
            return {"kind": "expired", "session": current}

        user = await db.users.find_one({"id": current["user_id"], "status": "active"}, {"_id": 0}, session=session)
        membership = await db.company_members.find_one(
            {"company_id": current["company_id"], "user_id": current["user_id"], "status": "active"},
            {"_id": 0}, session=session,
        )
        if (not user or not membership
                or int(current.get("auth_version", 1)) != int(user.get("auth_version", 1))):
            await db.sessions.update_one(
                {"session_id": current["session_id"], "status": "active", "refresh_token_hash": token_hash},
                {"$set": {"status": "revoked", "revoked_at": now}, "$unset": {"refresh_token_hash": ""}},
                session=session,
            )
            return {"kind": "inactive", "session": current}

        replacement = build_refresh_token()
        successor_hash = hash_refresh_token(replacement)
        access = sign_access_token(
            user["id"], current["company_id"], current["session_id"], membership.get("role"),
            auth_version=int(user.get("auth_version", 1)),
        )
        result_body = {
            "access_token": access, "token_type": "bearer", "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "user": {"id": user["id"], "name": user.get("name", ""), "company_id": current["company_id"],
                     "membership_id": membership.get("id"), "role": membership.get("role"),
                     "department": membership.get("department")},
        }
        created_at = _now()
        record = {
            "request_id_hash": request_id_hash,
            "session_id": current["session_id"], "user_id": current["user_id"],
            "company_id": current["company_id"], "previous_token_hash": token_hash,
            "successor_token_hash": successor_hash, "created_at": created_at,
            "window_expires_at": created_at + timedelta(seconds=REFRESH_DUPLICATE_WINDOW_SECONDS),
            "cleanup_at": created_at + timedelta(seconds=REFRESH_REQUEST_CLEANUP_SECONDS),
        }
        record.update(_encrypt_refresh_result(record, {"body": result_body, "refresh_token": replacement}))
        swapped = await db.sessions.update_one(
            {"session_id": current["session_id"], "status": "active", "refresh_token_hash": token_hash,
             "refresh_expires_at": {"$gt": _iso(now)}},
            {"$set": {"refresh_token_hash": successor_hash, "last_seen_at": _iso(created_at)},
             "$addToSet": {"refresh_token_history": token_hash}, "$inc": {"refresh_version": 1}},
            session=session,
        )
        if swapped.matched_count != 1:
            raise _RefreshRace()
        await db.refresh_requests.insert_one(record, session=session)
        await write_audit_event(
            session=session,
            event_id=hashlib.sha256(f"factoryos-auth-refresh:{request_id_hash}".encode("utf-8")).hexdigest(),
            action="auth.refresh", resource_type="session", resource_id=current["session_id"],
            company_id=current["company_id"],
            user={**user, "membership_id": membership.get("id")}, result="success",
            request_id=getattr(request.state, "request_id", None),
        )
        return {"kind": "rotated", "record": record, "result": {"body": result_body, "refresh_token": replacement},
                "user": user}

    async def replay_result(record: dict) -> dict:
        if record.get("request_id_hash") != request_id_hash:
            raise HTTPException(status_code=409, detail="refresh request ID was already used")
        if (record.get("previous_token_hash") not in {token_hash, None}
                and record.get("successor_token_hash") != token_hash):
            raise HTTPException(status_code=409, detail="refresh request ID belongs to another session or token")
        if (record.get("session_id") is None or record.get("user_id") is None
                or record.get("company_id") is None):
            raise HTTPException(status_code=401, detail="invalid refresh session")

        bound_session = await db.sessions.find_one(
            {"session_id": record["session_id"], "user_id": record["user_id"],
             "company_id": record["company_id"]}, {"_id": 0},
        )
        bound_user = await db.users.find_one({"id": record["user_id"], "status": "active"}, {"_id": 0})
        bound_membership = await db.company_members.find_one(
            {"company_id": record["company_id"], "user_id": record["user_id"], "status": "active"},
            {"_id": 0},
        )
        expiry = _refresh_expiry(bound_session or {})
        if (bound_session and bound_session.get("status") == "active"
                and (expiry is None or expiry <= _now())):
            await db.sessions.update_one(
                {"session_id": bound_session["session_id"], "status": "active"},
                {"$set": {"status": "expired", "expired_at": _iso(_now())},
                 "$unset": {"refresh_token_hash": ""}},
            )
            _clear_refresh_cookie(response)
            raise HTTPException(status_code=401, detail="refresh session expired")
        if (not bound_session or bound_session.get("status") != "active" or not bound_user or not bound_membership
                or int(bound_session.get("auth_version", 1)) != int(bound_user.get("auth_version", 1))):
            if bound_session and bound_session.get("status") == "active":
                await db.sessions.update_one(
                    {"session_id": bound_session["session_id"], "status": "active"},
                    {"$set": {"status": "revoked", "revoked_at": _iso(_now())},
                     "$unset": {"refresh_token_hash": ""}},
                )
            _clear_refresh_cookie(response)
            raise HTTPException(status_code=401, detail="session is no longer active")

        window_expires_at = _stored_utc(record.get("window_expires_at"))
        duplicate_window_open = window_expires_at is not None and window_expires_at > _now()
        if record.get("previous_token_hash") == token_hash:
            if not duplicate_window_open:
                await _revoke_refresh_replay(bound_session, request, _now())
                raise HTTPException(status_code=401, detail="refresh token replay detected")
        elif record.get("successor_token_hash") != token_hash:
            raise HTTPException(status_code=409, detail="refresh request ID belongs to another token")
        elif not duplicate_window_open:
            raise HTTPException(status_code=409, detail="refresh request ID duplicate window expired")

        if bound_session.get("refresh_token_hash") != record.get("successor_token_hash"):
            raise HTTPException(status_code=409, detail="refresh session has since rotated")
        try:
            cached = _decrypt_refresh_result(record)
        except Exception:
            logger.exception("unable to decrypt bounded refresh result")
            raise HTTPException(status_code=401, detail="refresh retry result is unavailable") from None
        _set_refresh_cookie(response, cached["refresh_token"])
        return cached["body"]

    try:
        async with await client.start_session() as mongo_session:
            outcome = await mongo_session.with_transaction(rotate)
    except AuditPersistenceError:
        raise HTTPException(status_code=503,
                            detail="required audit persistence failed; refresh was not rotated") from None
    except _RefreshRace:
        outcome = {"kind": "not_current"}
    except DuplicateKeyError:
        outcome = {"kind": "duplicate_lookup"}

    if outcome["kind"] in {"duplicate", "duplicate_lookup", "not_current"}:
        prior = outcome.get("record") or await db.refresh_requests.find_one(
            {"request_id_hash": request_id_hash}, {"_id": 0},
        )
        if prior:
            return await replay_result(prior)
        replay = await db.sessions.find_one({"refresh_token_history": token_hash}, {"_id": 0})
        if replay:
            await _revoke_refresh_replay(replay, request, now)
            _clear_refresh_cookie(response)
            raise HTTPException(status_code=401, detail="refresh token replay detected")
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="invalid refresh session")

    if outcome["kind"] in {"inactive", "expired"}:
        _clear_refresh_cookie(response)
        detail = "refresh session expired" if outcome["kind"] == "expired" else "session is no longer active"
        raise HTTPException(status_code=401, detail=detail)

    result = outcome["result"]
    _set_refresh_cookie(response, result["refresh_token"])
    return result["body"]


async def _revoke_refresh_replay(session: dict, request: Request, now: datetime) -> None:
    async def revoke(mongo_session):
        result = await db.sessions.update_one(
            {"session_id": session["session_id"], "status": "active"},
            {"$set": {"status": "revoked", "revoked_at": _iso(now), "refresh_reuse_detected_at": _iso(now)},
             "$unset": {"refresh_token_hash": ""}}, session=mongo_session,
        )
        if result.modified_count:
            event_id = hashlib.sha256(
                f"factoryos-refresh-reuse:{session['session_id']}:{session.get('refresh_version', 0)}".encode()
            ).hexdigest()
            await write_audit_event(
                session=mongo_session, event_id=event_id, action="auth.refresh_reuse",
                resource_type="session", resource_id=session["session_id"],
                company_id=session["company_id"], user={"id": session.get("user_id")},
                result="blocked", request_id=getattr(request.state, "request_id", None),
            )

    try:
        async with await client.start_session() as mongo_session:
            await mongo_session.with_transaction(revoke)
    except AuditPersistenceError:
        raise HTTPException(status_code=503,
                            detail="required audit persistence failed; replay revocation was not applied") from None


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response):
    _check_browser_origin(request)
    session_id = None
    user = None
    company_id = None
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw:
        session = await db.sessions.find_one({"refresh_token_hash": hash_refresh_token(raw)}, {"_id": 0})
        if session:
            session_id, company_id = session.get("session_id"), session.get("company_id")
            user = await db.users.find_one({"id": session["user_id"]}, {"_id": 0})
    if not session_id:
        try:
            payload = decode_access_token(get_bearer_token(request))
            session_id = payload.get("session_id")
            company_id = payload.get("company_id")
            user = await db.users.find_one({"id": payload.get("sub")}, {"_id": 0})
        except HTTPException:
            pass
    if session_id:
        async def revoke(mongo_session):
            await db.sessions.update_one({"session_id": session_id, "status": "active"},
                {"$set": {"status": "revoked", "revoked_at": _iso(_now())},
                 "$unset": {"refresh_token_hash": ""}}, session=mongo_session)

        await run_audited_mutation(
            company_id=company_id or "system", user=user or {}, request=request,
            action="auth.logout", resource_type="session", resource_id=session_id,
            mutate=revoke, after_state={"status": "revoked"},
        )
    _clear_refresh_cookie(response)
    response.status_code = 204
    return response


@router.post("/password-reset/request", status_code=202)
async def password_reset_request(body: PasswordResetRequest, request: Request, background: BackgroundTasks):
    # Same response for unknown accounts avoids account enumeration.
    generic = {"message": "If the account is eligible, reset instructions will be sent."}
    peer = request.client.host if request.client else "unknown"
    email = body.email.strip().casefold()
    ip_ok, _ = await consume_rate_limit("auth.reset.ip", peer, limit=20, window_seconds=3600)
    email_ok, _ = await consume_rate_limit("auth.reset.email", email, limit=5, window_seconds=3600)
    if not ip_ok or not email_ok:
        return generic
    if not _smtp_ready():
        logger.error("password reset requested but mail delivery is not configured")
        return generic
    user = await db.users.find_one({"email_normalized": email, "status": "active"}, {"_id": 0})
    if not user:
        user = await db.users.find_one({"email": body.email.strip(), "status": "active"}, {"_id": 0})
    if not user:
        return generic
    token = secrets.token_urlsafe(32)
    expiry = _iso(_now() + timedelta(seconds=RESET_TTL_SECONDS))
    async def persist(mongo_session):
        result = await db.users.update_one({"id": user["id"], "status": "active"},
            {"$set": {"password_reset_token_hash": hash_refresh_token(token),
                      "password_reset_expires_at": expiry}}, session=mongo_session)
        if result.matched_count != 1:
            return False
        return True

    persisted = await run_audited_mutation(
        company_id=user.get("company_id") or "system", user=user, request=request,
        action="auth.password_reset.request", resource_type="user", resource_id=user["id"],
        mutate=persist, after_state={"reset_requested": True},
    )
    if not persisted:
        return generic
    background.add_task(_deliver_reset, user["email"], token)
    return generic


@router.post("/password-reset/confirm", status_code=204)
async def password_reset_confirm(body: PasswordResetConfirm, request: Request, response: Response):
    _check_browser_origin(request)
    now = _iso(_now())
    result_user = None

    async def persist(mongo_session):
        nonlocal result_user
        result_user = await db.users.find_one_and_update(
            {"password_reset_token_hash": hash_refresh_token(body.token), "status": "active",
             "password_reset_expires_at": {"$gt": now}},
            {"$set": {"password_hash": hash_password(body.new_password), "password_last_changed_at": now},
             "$unset": {"password_reset_token_hash": "", "password_reset_expires_at": ""},
             "$inc": {"auth_version": 1}},
            return_document=ReturnDocument.AFTER, session=mongo_session,
        )
        if not result_user:
            raise HTTPException(status_code=400, detail="reset token is invalid or expired")
        await db.sessions.update_many({"user_id": result_user["id"], "status": "active"},
            {"$set": {"status": "revoked", "revoked_at": now}, "$unset": {"refresh_token_hash": ""}},
            session=mongo_session)
        return True

    await run_audited_mutation(
        company_id="system", user={}, request=request, action="auth.password_reset.complete",
        resource_type="user", resource_id=None, mutate=persist,
        after_state={"password_changed": True, "sessions_revoked": True},
    )
    _clear_refresh_cookie(response)
    response.status_code = 204
    return response


@router.get("/memberships")
async def memberships(request: Request):
    context = await get_company_context(request)
    records = await _active_memberships(context["user"]["id"])
    return [{"id": m.get("id"), "company_id": m["company_id"], "role": m.get("role"),
             "department": m.get("department")} for m in records]


@router.post("/invitations", status_code=202)
async def invite_member(body: InvitationIn, request: Request, background: BackgroundTasks,
                        user: dict = Depends(current_user), cid: str = Depends(get_company_id)):
    require_permission(user, "users.manage")
    peer = request.client.host if request.client else "unknown"
    ip_ok, ip_retry = await consume_rate_limit("auth.invite.ip", peer, limit=30, window_seconds=3600)
    actor_ok, actor_retry = await consume_rate_limit("auth.invite.actor", user["id"], limit=10, window_seconds=3600)
    if not ip_ok or not actor_ok:
        raise HTTPException(status_code=429, detail="too many invitations",
                            headers={"Retry-After": str(max(ip_retry if not ip_ok else 0, actor_retry if not actor_ok else 0))})
    if not _smtp_ready():
        raise HTTPException(status_code=503, detail="membership email delivery is not configured")
    normalized = body.email.strip().casefold()
    canonical_role = ROLE_ALIASES.get(body.role, body.role)
    if body.role not in ROLE_PERMISSIONS and canonical_role not in ROLE_PERMISSIONS:
        raise HTTPException(status_code=422, detail="unknown role")
    if canonical_role in {"owner", "admin"} and ROLE_ALIASES.get(user.get("role"), user.get("role")) != "owner":
        raise HTTPException(status_code=403, detail="only an owner may assign owner or administrator roles")
    account = await db.users.find_one({"email_normalized": normalized}, {"_id": 0})
    if not account:
        account = await db.users.find_one({"email": body.email.strip()}, {"_id": 0})
    if account and await db.company_members.find_one({"company_id": cid, "user_id": account["id"]}):
        raise HTTPException(status_code=409, detail="user already has a company membership")
    is_new_account = not account
    if is_new_account:
        account = {"id": secrets.token_urlsafe(18), "email": body.email.strip(), "email_normalized": normalized,
                   "name": body.name.strip(), "status": "pending_verification", "auth_provider": "local",
                   "auth_version": 1, "created_at": _iso(_now())}
    token = secrets.token_urlsafe(32)
    expires = _iso(_now() + timedelta(hours=48))
    membership = {"id": secrets.token_urlsafe(18), "company_id": cid, "user_id": account["id"],
                  "role": body.role, "department": body.department, "status": "invited",
                  "permissions_override": [], "invited_by": user["id"], "joined_at": _iso(_now()),
                  "invitation_token_hash": hash_refresh_token(token), "invitation_expires_at": expires}
    async def persist(mongo_session):
        if is_new_account:
            await db.users.insert_one(account, session=mongo_session)
        await db.company_members.insert_one(membership, session=mongo_session)

    await run_audited_mutation(
        company_id=cid, user=user, request=request, action="membership.invite",
        resource_type="company_membership", resource_id=membership["id"], mutate=persist,
        after_state={"role": body.role, "department": body.department, "status": "invited"},
    )
    background.add_task(_deliver_invitation, account["email"], token, cid)
    return {"membership_id": membership["id"], "status": "invited"}


@router.post("/invitations/accept", status_code=200)
async def accept_invitation(body: InvitationAcceptIn, request: Request):
    now = _iso(_now())
    token_hash = hash_refresh_token(body.token)
    audit_membership = await db.company_members.find_one(
        {"invitation_token_hash": token_hash, "status": "invited", "invitation_expires_at": {"$gt": now}},
        {"_id": 0},
    )
    if not audit_membership:
        raise HTTPException(status_code=400, detail="invitation is invalid or expired")
    audit_user = await db.users.find_one({"id": audit_membership["user_id"]}, {"_id": 0})
    if not audit_user:
        raise HTTPException(status_code=400, detail="invitation is invalid or expired")
    activated = None

    async def persist(mongo_session):
        nonlocal activated
        membership = await db.company_members.find_one(
            {"invitation_token_hash": token_hash, "status": "invited", "invitation_expires_at": {"$gt": now}},
            {"_id": 0}, session=mongo_session,
        )
        if not membership:
            raise HTTPException(status_code=400, detail="invitation is invalid or expired")
        user = await db.users.find_one({"id": membership["user_id"]}, {"_id": 0}, session=mongo_session)
        if not user:
            raise HTTPException(status_code=400, detail="invitation is invalid or expired")
        if user.get("status") == "pending_verification":
            if not body.new_password:
                raise HTTPException(status_code=422, detail="new_password is required to activate this account")
            await db.users.update_one({"id": user["id"], "status": "pending_verification"},
                {"$set": {"password_hash": hash_password(body.new_password), "status": "active",
                           "password_last_changed_at": now},
                 "$unset": {"activation_token_hash": "", "activation_expires_at": ""}}, session=mongo_session)
        activated = await db.company_members.find_one_and_update(
            {"id": membership["id"], "company_id": membership["company_id"], "status": "invited",
             "invitation_token_hash": token_hash, "invitation_expires_at": {"$gt": now}},
            {"$set": {"status": "active", "updated_at": now, "joined_at": now},
             "$unset": {"invitation_token_hash": "", "invitation_expires_at": ""}},
            return_document=ReturnDocument.AFTER, session=mongo_session,
        )
        if not activated:
            raise HTTPException(status_code=400, detail="invitation is invalid or expired")
        return {"status": "active", "company_id": activated["company_id"]}

    result = await run_audited_mutation(
        company_id=audit_membership["company_id"], user=audit_user, request=request,
        action="membership.accept_invitation", resource_type="company_membership",
        resource_id=audit_membership["id"], mutate=persist,
        after_state={"status": "active"},
    )
    return result


@router.get("/session")
async def auth_session(user: dict = Depends(current_user)):
    catalogue = set().union(*ROLE_PERMISSIONS.values())
    permissions = sorted(permission for permission in catalogue if has_permission(user, permission))
    return {"user": {"id": user["id"], "name": user.get("name", ""), "email": user.get("email", ""),
                      "company_id": user["company_id"], "membership_id": user.get("membership_id"),
                      "role": user.get("role"), "department": user.get("department"),
                      "department_ids": user.get("department_ids", [])}, "permissions": permissions}


@router.post("/switch-company")
async def switch_company(body: CompanySwitchIn, request: Request, response: Response):
    _check_browser_origin(request)
    context = await get_company_context(request)
    user = context["user"]
    membership = await db.company_members.find_one({"company_id": body.company_id, "user_id": user["id"],
                                                     "status": "active"}, {"_id": 0})
    if not membership:
        raise HTTPException(status_code=403, detail="active company membership required")

    async def persist(mongo_session):
        result = await _issue_session(user, membership, request, response, mongo_session=mongo_session)
        revoked = await db.sessions.update_one(
            {"session_id": context["session"]["session_id"], "status": "active"},
            {"$set": {"status": "revoked", "revoked_at": _iso(_now())},
             "$unset": {"refresh_token_hash": ""}}, session=mongo_session,
        )
        if revoked.modified_count != 1:
            raise HTTPException(status_code=401, detail="session is no longer active")
        return result

    return await run_audited_mutation(
        company_id=body.company_id, user=user, request=request, action="auth.company_switch",
        resource_type="session", resource_id=context["session"]["session_id"], mutate=persist,
        after_state={"active_company_id": body.company_id, "prior_session_revoked": True},
    )


@router.patch("/memberships/{membership_id}")
async def update_membership_status(membership_id: str, body: MembershipUpdateIn,
                                   request: Request, user: dict = Depends(current_user),
                                   cid: str = Depends(get_company_id)):
    require_permission(user, "users.manage")
    membership = await db.company_members.find_one({"id": membership_id, "company_id": cid}, {"_id": 0})
    if not membership:
        raise HTTPException(status_code=404, detail="membership not found")
    patch = body.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=422, detail="at least one membership field is required")
    if any(key in patch for key in ("role", "permissions_override", "department", "department_ids")):
        require_permission(user, "roles.manage")
    if "role" in patch:
        canonical = ROLE_ALIASES.get(patch["role"], patch["role"])
        if patch["role"] not in ROLE_PERMISSIONS and canonical not in ROLE_PERMISSIONS:
            raise HTTPException(status_code=422, detail="unknown role")
        if canonical in {"owner", "admin"} and ROLE_ALIASES.get(user.get("role"), user.get("role")) != "owner":
            raise HTTPException(status_code=403, detail="only an owner may assign owner or administrator roles")
    if "permissions_override" in patch:
        catalog = set().union(*ROLE_PERMISSIONS.values())
        unknown = [p for p in patch["permissions_override"] if p.removeprefix("deny:") not in catalog]
        if unknown:
            raise HTTPException(status_code=422, detail="unknown permission override")
        protected = {"company.manage", "users.manage", "roles.manage", "inventory.approve", "orders.approve"}
        if any(p.removeprefix("deny:") in protected and not p.startswith("deny:") for p in patch["permissions_override"]):
            if ROLE_ALIASES.get(user.get("role"), user.get("role")) != "owner":
                raise HTTPException(status_code=403, detail="only an owner may grant protected permissions")
    before = {key: membership.get(key) for key in patch}
    patch["updated_at"] = _iso(_now())

    async def persist(mongo_session):
        changed = await db.company_members.update_one(
            {"id": membership_id, "company_id": cid}, {"$set": patch}, session=mongo_session,
        )
        if changed.matched_count != 1:
            raise HTTPException(status_code=404, detail="membership not found")
        if ((body.status and body.status != "active")
                or any(key in patch for key in ("role", "permissions_override", "department", "department_ids"))):
            await db.sessions.update_many(
                {"user_id": membership["user_id"], "company_id": cid, "status": "active"},
                {"$set": {"status": "revoked", "revoked_at": _iso(_now())},
                 "$unset": {"refresh_token_hash": ""}}, session=mongo_session,
            )
        return {"id": membership_id, "company_id": cid,
                **{k: v for k, v in patch.items() if k != "updated_at"}}

    return await run_audited_mutation(
        company_id=cid, user=user, request=request, action="membership.status",
        resource_type="company_membership", resource_id=membership_id, mutate=persist,
        after_state={"before": before, "after": patch},
    )
