"""Inbound rate limiting (P3): fixed windows keyed by IP, email or user.

Two layers protect the app (implementation_plan P3): Cloudflare at the edge
stops floods; this module enforces per-key budgets on the endpoints that
matter (login, signup, TOTP, password reset, venue connect/claim, the public
JSON API, follow/allocate). Outbound venue budgets are a different problem
(P2) and do not live here.

Fixed window: the window containing `now` starts at
`floor(now / window) * window` (unix seconds). Every hit increments that
window's counter; a hit is allowed while the counter is <= the limit.
`retry_after` is the whole seconds until the window ends, at least 1.

Backends:
- `MemoryLimiter`: a dict in this process. Tests and dev only; preflight
  refuses it, because several web processes would each allow the full limit.
- `PostgresLimiter`: one `INSERT ... ON CONFLICT DO UPDATE ... RETURNING
  count` round trip per hit on `rate_limit_counters`, in its own short
  transaction so a failed request still counts. SQLite (>= 3.35) runs the
  same statement, which is what the unit tests use.

Client IP: `request.client.host`, unless `trust_proxy_headers` is on, in
which case CF-Connecting-IP (Cloudflare) then Fly-Client-IP (Fly edge) win.
Only turn that on when the app is reachable solely through the proxy.
"""

import math
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from alphamarket.config import get_settings
from alphamarket.models import RateLimitCounter, User


@dataclass(frozen=True)
class Decision:
    allowed: bool
    count: int  # hits in this window, including this one
    limit: int
    retry_after: int  # seconds until the window resets (>= 1)


@dataclass(frozen=True)
class Rule:
    name: str
    limit: int
    window_seconds: int


# implementation_plan P3 table (starting values).
RULES: dict[str, Rule] = {
    r.name: r
    for r in (
        Rule("login_ip", 10, 60),
        Rule("login_email", 20, 3600),
        Rule("signup_ip", 5, 3600),
        Rule("totp_user", 5, 300),  # then a 15-minute lock (mfa.py)
        Rule("reset_email", 3, 3600),
        Rule("reset_ip", 10, 3600),
        Rule("venues_user", 10, 3600),  # POST /venues, claim, credential rotate
        Rule("api_ip", 60, 60),
        Rule("follow_user", 60, 3600),  # follow and allocate
        Rule("verify_resend_user", 3, 3600),
        Rule("email_change_user", 5, 3600),  # wave2: each request sends two emails
        # wave2: GET /dashboard/connect/snaptrade/{id} calls SnapTrade twice per
        # load (account list + a fresh portal link) and audits a credential
        # decrypt, so reloading it must not spend the venues_user budget the
        # start/finish POSTs need, nor hammer the shared 250/min SnapTrade pool.
        # A real connect is a handful of views (open the portal, come back, link
        # a second broker); 30 per 10 minutes is generous for that and caps one
        # user at ~6 SnapTrade calls a minute (2.4% of the pool).
        Rule("snaptrade_picker_user", 30, 600),
        # watchlist: each add of a new address is a venue call and an import
        Rule("watch_user", 20, 3600),
        # watchlist opt-out: the proof step is a venue call (signature or bio check)
        Rule("watch_optout_user", 20, 3600),
    )
}


def window_bounds(now: datetime, window_seconds: int) -> tuple[int, int]:
    """(window start, seconds left in the window, at least 1).

    Example: window 60 s, now = 1_000_015 s since the epoch.
    start = floor(1_000_015 / 60) * 60 = 16_666 * 60 = 999_960; the window
    ends at 1_000_020, so 5 seconds remain."""
    ts = now.timestamp()
    start = math.floor(ts / window_seconds) * window_seconds
    left = max(1, math.ceil(start + window_seconds - ts))
    return start, left


class RateLimiter(Protocol):
    def hit(
        self, key: str, limit: int, window_seconds: int, now: datetime | None = None
    ) -> Decision: ...


class MemoryLimiter:
    """Per-process counters. Stale windows are dropped on the next hit of
    the same key, so memory stays bounded by the number of live keys."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._counts: dict[str, tuple[int, int]] = {}  # key -> (window start, count)
        self._lock = threading.Lock()
        # Injectable so tests can hold one fixed window (a loop that straddles
        # a window boundary would otherwise reset the count mid-test).
        self.clock = clock or (lambda: datetime.now(UTC))

    def hit(
        self, key: str, limit: int, window_seconds: int, now: datetime | None = None
    ) -> Decision:
        start, left = window_bounds(now or self.clock(), window_seconds)
        with self._lock:
            prev_start, prev_count = self._counts.get(key, (start, 0))
            count = (prev_count if prev_start == start else 0) + 1
            self._counts[key] = (start, count)
        return Decision(count <= limit, count, limit, left)


class PostgresLimiter:
    """Counters in `rate_limit_counters`, shared by every web process."""

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        if session_factory is None:
            from alphamarket.db import SessionLocal

            session_factory = SessionLocal
        self._session_factory = session_factory

    def hit(
        self, key: str, limit: int, window_seconds: int, now: datetime | None = None
    ) -> Decision:
        now = now or datetime.now(UTC)
        start, left = window_bounds(now, window_seconds)
        expires = datetime.fromtimestamp(start + window_seconds, UTC)
        with self._session_factory() as db:
            insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
            stmt = insert(RateLimitCounter).values(
                key=key[:200], window_start=start, count=1, expires_at=expires
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["key", "window_start"],
                set_={"count": RateLimitCounter.count + 1},
            ).returning(RateLimitCounter.count)
            count = db.execute(stmt).scalar_one()
            db.commit()
        return Decision(count <= limit, count, limit, left)


def prune_counters(db: Session, now: datetime | None = None) -> int:
    """Delete windows that have ended. Safe to run any time (worker job)."""
    result = db.execute(
        delete(RateLimitCounter).where(RateLimitCounter.expires_at < (now or datetime.now(UTC)))
    )
    db.commit()
    return result.rowcount or 0


def build_limiter(kind: str | None = None) -> RateLimiter:
    kind = kind or get_settings().rate_limiter
    if kind == "memory":
        return MemoryLimiter()
    if kind == "postgres":
        return PostgresLimiter()
    raise ValueError(f"Unknown RATE_LIMITER {kind!r} (memory | postgres)")


def get_limiter(app) -> RateLimiter:
    """One limiter per app instance (so each test client starts clean)."""
    limiter = getattr(app.state, "rate_limiter", None)
    if limiter is None:
        limiter = build_limiter()
        app.state.rate_limiter = limiter
    return limiter


def client_ip(request: Request) -> str:
    if get_settings().trust_proxy_headers:
        for header in ("cf-connecting-ip", "fly-client-ip"):
            value = request.headers.get(header, "").strip()
            if value:
                return value[:64]
    return request.client.host if request.client else "unknown"


def too_many(retry_after: int, detail: str = "Too many requests. Try again later.") -> None:
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after)},
    )


def check(request: Request, rule_name: str, subject: str | int) -> Decision:
    """Count one hit of `rule_name` for `subject`; raise 429 when over."""
    rule = RULES[rule_name]
    decision = get_limiter(request.app).hit(
        f"{rule.name}:{subject}", rule.limit, rule.window_seconds
    )
    if not decision.allowed:
        too_many(decision.retry_after)
    return decision


def hit_quietly(request: Request, rule_name: str, subject: str | int) -> bool:
    """Count a hit; return False instead of raising when over the limit (for
    endpoints that must answer the same way either way, e.g. reset requests)."""
    rule = RULES[rule_name]
    return (
        get_limiter(request.app)
        .hit(f"{rule.name}:{subject}", rule.limit, rule.window_seconds)
        .allowed
    )


def rate_limit(rule_name: str, by: str = "ip"):
    """FastAPI dependency factory: `dependencies=[Depends(rate_limit("follow_user", by="user"))]`.
    `by="user"` keys on the logged-in user (and so requires login)."""
    if rule_name not in RULES:
        raise ValueError(f"unknown rate-limit rule {rule_name}")
    if by == "ip":

        def by_ip(request: Request) -> None:
            check(request, rule_name, client_ip(request))

        return by_ip
    if by == "user":
        from alphamarket.auth import current_user

        def by_user(request: Request, user: User = Depends(current_user)) -> None:
            check(request, rule_name, user.id)

        return by_user
    raise ValueError(f"unknown rate-limit key {by}")


class ApiRateLimitMiddleware:
    """`/api/*` per client IP (the public JSON API). Pure ASGI so it wraps
    every API route, whichever router defines it."""

    def __init__(self, app: ASGIApp, prefix: str = "/api/", rule_name: str = "api_ip") -> None:
        self.app = app
        self.prefix = prefix
        self.rule = RULES[rule_name]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        limiter = get_limiter(scope["app"])
        key = f"{self.rule.name}:{client_ip(request)}"
        decision = await run_in_threadpool(
            limiter.hit, key, self.rule.limit, self.rule.window_seconds
        )
        if not decision.allowed:
            response = JSONResponse(
                {"detail": "Too many requests. Try again later."},
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                headers={"Retry-After": str(decision.retry_after)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
