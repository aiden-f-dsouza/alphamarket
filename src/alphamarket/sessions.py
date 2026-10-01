"""DB-backed login sessions (P3), replacing the signed-cookie `user_id`.

The `koine_sid` cookie holds 32 random bytes (urlsafe base64). The database
stores only its SHA-256, so a leaked table cannot be replayed as cookies.
A session is valid while it is not revoked, was seen within the idle timeout
(24 h) and is younger than the absolute limit (30 d). A session that has
passed the password step but not yet TOTP (`mfa_pending`) lives 10 minutes
and authenticates nothing else.

Rotation: every login, every TOTP verification and every step-up revokes the
presented session and issues a fresh id, so a session id fixed before login
is worthless after it.

Cookies are set by `SessionCookieMiddleware`: code that logs someone in only
records the new id on the request state, and the middleware writes the
Set-Cookie header onto whatever response is sent (JSON or a redirect).
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from alphamarket.config import get_settings
from alphamarket.models import User, UserSession

COOKIE_NAME = "koine_sid"
PENDING_TTL = timedelta(minutes=10)
STEP_UP_WINDOW = timedelta(minutes=10)
TOUCH_EVERY = timedelta(minutes=1)  # last_seen_at write throttle
_STATE_KEY = "koine_session_cookie"  # request-state slot the middleware reads


def aware(dt: datetime | None) -> datetime | None:
    """SQLite hands back naive datetimes for timezone=True columns; they
    were written as UTC (invariant 2), so re-attach UTC."""
    if dt is None or dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=UTC)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def idle_timeout() -> timedelta:
    return timedelta(hours=get_settings().session_idle_hours)


def absolute_timeout() -> timedelta:
    return timedelta(days=get_settings().session_absolute_days)


def is_live(sess: UserSession, now: datetime) -> bool:
    """Not revoked, within the idle and absolute limits (pending: 10 min)."""
    if sess.revoked_at is not None:
        return False
    created, seen = aware(sess.created_at), aware(sess.last_seen_at)
    if sess.mfa_pending:
        return now - created < PENDING_TTL
    return now - seen < idle_timeout() and now - created < absolute_timeout()


def has_recent_step_up(sess: UserSession | None, now: datetime | None = None) -> bool:
    if sess is None or sess.step_up_at is None:
        return False
    return (now or datetime.now(UTC)) - aware(sess.step_up_at) < STEP_UP_WINDOW


def _cookie_token(request: Request) -> str | None:
    return request.cookies.get(COOKIE_NAME) or None


def load(db: Session, request: Request, now: datetime | None = None) -> UserSession | None:
    """The live session named by the request cookie, or None. Cached on the
    request so several dependencies share one lookup."""
    if hasattr(request.state, "auth_session"):
        return request.state.auth_session
    token = _cookie_token(request)
    sess = None
    if token:
        found = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
        # A password-only (mfa_pending) session never authenticates a request.
        if found is not None and not found.mfa_pending and is_live(found, now or datetime.now(UTC)):
            sess = found
    request.state.auth_session = sess
    return sess


def touch(db: Session, sess: UserSession, now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    if now - aware(sess.last_seen_at) >= TOUCH_EVERY:
        sess.last_seen_at = now
        db.commit()


def _client_meta(request: Request) -> tuple[str, str]:
    from alphamarket.ratelimit import client_ip

    return client_ip(request), (request.headers.get("user-agent") or "")[:300]


def issue(
    db: Session,
    request: Request,
    user: User,
    *,
    mfa_pending: bool = False,
    step_up: bool = False,
    now: datetime | None = None,
) -> UserSession:
    """Revoke the presented session (if any) and start a new one for `user`.
    Commits, and schedules the new cookie on the response."""
    now = now or datetime.now(UTC)
    revoke_current(db, request, now=now, clear_cookie=False)
    token = secrets.token_urlsafe(32)
    ip, ua = _client_meta(request)
    sess = UserSession(
        token_hash=hash_token(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        ip=ip,
        user_agent=ua,
        mfa_pending=mfa_pending,
        step_up_at=now if step_up else None,
    )
    db.add(sess)
    db.commit()
    request.state.auth_session = None if mfa_pending else sess
    request.state.pending_session = sess if mfa_pending else None
    setattr(request.state, _STATE_KEY, ("set", token))
    return sess


def revoke_current(
    db: Session, request: Request, now: datetime | None = None, clear_cookie: bool = True
) -> None:
    token = _cookie_token(request)
    if token:
        db.execute(
            update(UserSession)
            .where(UserSession.token_hash == hash_token(token), UserSession.revoked_at.is_(None))
            .values(revoked_at=now or datetime.now(UTC))
        )
        db.commit()
    request.state.auth_session = None
    if clear_cookie:
        setattr(request.state, _STATE_KEY, ("clear", ""))


def revoke_all(
    db: Session, user_id: int, except_id: int | None = None, now: datetime | None = None
) -> int:
    stmt = update(UserSession).where(
        UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
    )
    if except_id is not None:
        stmt = stmt.where(UserSession.id != except_id)
    result = db.execute(stmt.values(revoked_at=now or datetime.now(UTC)))
    db.commit()
    return result.rowcount or 0


def revoke_one(db: Session, user_id: int, session_id: int) -> bool:
    result = db.execute(
        update(UserSession)
        .where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
    )
    db.commit()
    return bool(result.rowcount)


def pending(db: Session, request: Request, now: datetime | None = None) -> UserSession | None:
    """The password-passed, TOTP-pending session named by the cookie."""
    cached = getattr(request.state, "pending_session", None)
    if cached is not None:
        return cached
    token = _cookie_token(request)
    if not token:
        return None
    sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(token)))
    if sess is None or not sess.mfa_pending or not is_live(sess, now or datetime.now(UTC)):
        return None
    return sess


def active_sessions(db: Session, user_id: int, now: datetime | None = None) -> list[UserSession]:
    now = now or datetime.now(UTC)
    rows = db.scalars(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.mfa_pending.is_(False),
        )
        .order_by(UserSession.last_seen_at.desc())
    ).all()
    return [s for s in rows if is_live(s, now)]


def seen_ip_before(db: Session, user_id: int, ip: str, exclude_id: int | None = None) -> bool:
    """Has any earlier full session of this user come from `ip`?"""
    stmt = select(UserSession.id).where(
        UserSession.user_id == user_id,
        UserSession.ip == ip,
        UserSession.mfa_pending.is_(False),
    )
    if exclude_id is not None:
        stmt = stmt.where(UserSession.id != exclude_id)
    return db.scalar(stmt.limit(1)) is not None


def has_prior_sessions(db: Session, user_id: int, exclude_id: int | None = None) -> bool:
    stmt = select(UserSession.id).where(
        UserSession.user_id == user_id, UserSession.mfa_pending.is_(False)
    )
    if exclude_id is not None:
        stmt = stmt.where(UserSession.id != exclude_id)
    return db.scalar(stmt.limit(1)) is not None


class SessionCookieMiddleware:
    """Writes the cookie that `issue` / `revoke_current` scheduled.

    HttpOnly always; Secure when SECURE_COOKIES is on; SameSite=Lax; the
    browser keeps it for the absolute session lifetime, and the server-side
    row decides whether it still means anything."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        state = scope.setdefault("state", {})

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                pending_cookie = state.get(_STATE_KEY)
                if pending_cookie is not None:
                    headers = MutableHeaders(scope=message)
                    headers.append("set-cookie", _cookie_header(*pending_cookie))
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _cookie_header(action: str, token: str) -> str:
    settings = get_settings()
    parts = [f"{COOKIE_NAME}={token}", "Path=/", "HttpOnly", "SameSite=Lax"]
    if action == "set":
        parts.append(f"Max-Age={int(absolute_timeout().total_seconds())}")
    else:
        parts += ["Max-Age=0", "Expires=Thu, 01 Jan 1970 00:00:00 GMT"]
    if settings.secure_cookies:
        parts.append("Secure")
    return "; ".join(parts)
