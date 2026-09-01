from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from alphamarket import auth, pages, public, strategies, venues
from alphamarket.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AlphaMarket", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=settings.secure_cookies,
        same_site="lax",
    )
    app.include_router(auth.router)
    app.include_router(venues.router)
    app.include_router(strategies.router)
    app.include_router(public.router)
    app.include_router(pages.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
