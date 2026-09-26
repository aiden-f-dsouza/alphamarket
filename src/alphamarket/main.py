from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from alphamarket import auth, follows, pages, public, strategies, tails, venues
from alphamarket.config import get_settings
from alphamarket.csrf import CSRFMiddleware
from alphamarket.security_headers import SecurityHeadersMiddleware

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Koine", version="0.1.0")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    # Middleware order (platform_plan 2.7 items 5-6): add_middleware() pushes
    # each one in front of the last, so the LAST call here ends up OUTERMOST
    # at request time. We want, outside in: SecurityHeadersMiddleware (so its
    # headers land on every response, including a CSRF 403 or a 5xx) ->
    # CSRFMiddleware (rejects before the session is even parsed; it reads
    # cookies/Origin/Referer straight off the ASGI scope) -> SessionMiddleware
    # -> the app. Hence: Session first, CSRF second, SecurityHeaders last.
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=settings.secure_cookies,
        same_site="lax",
    )
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(auth.router)
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
