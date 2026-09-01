from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from alphamarket import auth, venues
from alphamarket.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="AlphaMarket", version="0.1.0")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        https_only=False,  # flip to True at deploy (M4)
        same_site="lax",
    )
    app.include_router(auth.router)
    app.include_router(venues.router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
