from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from alphamarket import (
    account,
    auth,
    follows,
    mfa,
    pages,
    public,
    security_pages,
    strategies,
    tails,
    venues,
)
from alphamarket.config import get_settings
from alphamarket.ratelimit import ApiRateLimitMiddleware, build_limiter
from alphamarket.sessions import SessionCookieMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Koine", version="0.1.0")
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
    app.include_router(auth.router)
    app.include_router(mfa.router)
    app.include_router(account.router)
    app.include_router(security_pages.router)
    app.include_router(venues.router)
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
