from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from alphamarket import audit, auth, follows, pages, public, redaction, strategies, tails, venues
from alphamarket.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Koine", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=settings.secure_cookies,
        same_site="lax",
    )
    # p3-vault: scrub secrets from every log record and 422 body; request id,
    # IP and user agent for audit rows.
    redaction.install()
    redaction.install_error_handlers(app)
    audit.install_request_context(app)
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
