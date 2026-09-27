from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from alphamarket import (
    account,
    audit,
    auth,
    follows,
    mfa,
    oauth,
    pages,
    public,
    redaction,
    security_pages,
    strategies,
    tails,
    venues,
)
from alphamarket.config import get_settings
from alphamarket.csrf import CSRFMiddleware
from alphamarket.ratelimit import ApiRateLimitMiddleware, build_limiter
from alphamarket.security_headers import SecurityHeadersMiddleware
from alphamarket.sessions import SessionCookieMiddleware

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Koine", version="0.1.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Middleware order: add_middleware() pushes each one in front of the last,
    # so the LAST call here ends up OUTERMOST at request time. Outside in:
    # audit request context (request id/IP for every audit row) ->
    # SecurityHeaders (headers on every response, incl. 403/429/5xx) -> CSRF
    # (rejects before any session work; reads cookies and Origin/Referer off
    # the scope) -> /api/* rate limit -> DB session cookie -> starlette
    # SessionMiddleware (non-auth state only) -> the app.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=settings.secure_cookies,
        same_site="lax",
    )
    # p3-auth: login identity lives in DB sessions (sessions.py), not in the
    # signed cookie above; /api/* is rate limited per client IP.
    app.state.rate_limiter = build_limiter()
    app.add_middleware(SessionCookieMiddleware)
    app.add_middleware(ApiRateLimitMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # p3-vault: scrub secrets from every log record and 422 body; request id,
    # IP and user agent for audit rows (outermost).
    redaction.install()
    redaction.install_error_handlers(app)
    audit.install_request_context(app)
    app.include_router(auth.router)
    app.include_router(mfa.router)
    app.include_router(account.router)
    app.include_router(security_pages.router)
    app.include_router(venues.router)
    app.include_router(oauth.router)
    app.include_router(strategies.router)
    app.include_router(public.router)
    app.include_router(follows.router)
    app.include_router(tails.router)
    app.include_router(pages.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
