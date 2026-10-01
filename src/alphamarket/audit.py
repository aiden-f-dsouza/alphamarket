"""Security audit log (platform_plan 2.7 item 4; append-only, invariant 3).

`record(db, action, ...)` adds one row to the caller's session; the caller
commits (credentials.py commits a decrypt's row BEFORE returning plaintext).
IP, user agent and the request id come from a context variable the HTTP
middleware sets per request; worker jobs set a job id with `job_context` /
`as_job`. Details are scrubbed: secret-looking keys are dropped to
"[REDACTED]" and every string passes through redaction.redact.

Actions are dotted strings. The ones this module's callers use are listed in
AuditAction; the auth module adds its own (login.*, totp.*) through the same
record() call.
"""

import functools
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy.orm import Session

from alphamarket.models import AuditLog, utcnow
from alphamarket.redaction import REDACTED, redact


class AuditAction(StrEnum):
    CREDENTIAL_CONNECT = "credential.connect"
    CREDENTIAL_REJECTED = "credential.rejected"  # writable key refused at connect
    CREDENTIAL_DISCONNECT = "credential.disconnect"
    CREDENTIAL_ROTATE = "credential.rotate"
    CREDENTIAL_DECRYPT = "credential.decrypt"
    SCOPE_CHECK = "scope.check"
    ACCOUNT_SUSPEND = "account.suspend"
    ADMIN_ACTION = "admin.action"  # details.op names the operation
    # ---- wave2 ----
    CREDENTIAL_REFRESH = "credential.refresh"  # OAuth access token refreshed
    VENUE_CONSENT = "venue.consent"  # consent screen accepted (invariant 10)
    EMAIL_CHANGE_REQUEST = "account.email_change_request"
    EMAIL_CHANGE = "account.email_change"


@dataclass(frozen=True)
class AuditContext:
    request_or_job_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None


_context: ContextVar[AuditContext] = ContextVar("audit_context", default=AuditContext())  # noqa: B039


def current_context() -> AuditContext:
    return _context.get()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


@contextmanager
def job_context(kind: str) -> Iterator[str]:
    """Tag everything audited inside with one job id ("sync-<hex>")."""
    job_id = new_id(kind)
    token = _context.set(replace(_context.get(), request_or_job_id=job_id))
    try:
        yield job_id
    finally:
        _context.reset(token)


def as_job(kind: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator form of job_context for worker entry points."""

    def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with job_context(kind):
                return fn(*args, **kwargs)

        return wrapper

    return deco


_SECRET_KEY = re.compile(
    r"(?i)(secret|passphrase|password|token|private|pem|ciphertext|dek|cookie|signature)"
)


def _is_secret_key(key: str) -> bool:
    return _SECRET_KEY.search(key) is not None


def scrub(value: Any, depth: int = 0) -> Any:
    """JSON-safe copy of `value` with secrets removed."""
    if depth > 6:
        return "[truncated]"
    if isinstance(value, dict):
        return {
            str(k): REDACTED if _is_secret_key(str(k)) else scrub(v, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple | set):
        return [scrub(v, depth + 1) for v in value]
    if isinstance(value, bool | int | float) or value is None:
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return "[bytes]"
    if isinstance(value, datetime | date):
        return value.isoformat()
    return redact(str(value))[:1000]


def record(
    db: Session,
    action: str,
    *,
    actor_user_id: int | None = None,
    target_type: str | None = None,
    target_id: int | str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    request_or_job_id: str | None = None,
) -> AuditLog:
    """Add one audit row to `db` (not committed). Explicit arguments win over
    the request/job context."""
    ctx = _context.get()
    row = AuditLog(
        actor_user_id=actor_user_id,
        action=str(action),
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        ip=(ip or ctx.ip or None),
        user_agent=((user_agent or ctx.user_agent or "")[:400] or None),
        request_or_job_id=request_or_job_id or ctx.request_or_job_id,
        details=scrub(details or {}),
        at=utcnow(),
    )
    db.add(row)
    return row


class RequestContextMiddleware:
    """Pure ASGI middleware: request id, client IP and user agent into the
    audit context for the duration of one HTTP request. The id is echoed as
    X-Request-ID so a user report can be matched to audit rows. An incoming
    X-Request-ID is ignored (a client could otherwise forge correlation)."""

    def __init__(self, app: Any, ip_header: str = "") -> None:
        self.app = app
        self.ip_header = ip_header.lower().encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        ip = None
        if self.ip_header and self.ip_header in headers:
            ip = headers[self.ip_header].decode("latin-1").split(",")[0].strip()
        elif scope.get("client"):
            ip = scope["client"][0]
        ua = headers.get(b"user-agent", b"").decode("latin-1")[:400] or None
        request_id = new_id("req")
        token = _context.set(AuditContext(request_or_job_id=request_id, ip=ip, user_agent=ua))

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"] = [
                    *message["headers"],
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            _context.reset(token)


def install_request_context(app: Any) -> None:
    from alphamarket.config import get_settings

    app.add_middleware(RequestContextMiddleware, ip_header=get_settings().client_ip_header)
