"""Request correlation and generic audit coverage for authenticated API mutations."""

import logging
import secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from lib.audit import write_audit_event

logger = logging.getLogger(__name__)


class RequestAuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        supplied = request.headers.get("x-request-id", "")
        request.state.request_id = supplied[:128] if supplied and supplied.isascii() else secrets.token_urlsafe(16)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id

        if (request.url.path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}
                and not getattr(request.state, "transactional_audit_committed", False)
                and not request.url.path.startswith("/api/auth/")):
            context = getattr(request.state, "company_context", None)
            if context:
                path_parts = [part for part in request.url.path.split("/") if part]
                await write_audit_event(
                    action=f"api.{request.method.lower()}:{request.url.path[:256]}",
                    resource_type=path_parts[1] if len(path_parts) > 1 else "api_resource",
                    result="success" if response.status_code < 400 else "failure",
                    company_id=context["company_id"], user=context["user"],
                    request_id=request.state.request_id,
                    ip=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                )
        return response
