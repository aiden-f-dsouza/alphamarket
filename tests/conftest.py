"""Unit tests run against in-memory SQLite via dependency override.

Integration tests against real Postgres (db_test on :5433) come in M1+.
SQLite is fine here because the models stick to portable column types.
"""

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alphamarket.db import Base, get_db
from alphamarket.main import create_app


@pytest.fixture(autouse=True)
def test_settings():
    """Pin the cached settings so tests never depend on the developer's .env:
    a throwaway Fernet key for the vault, and KALSHI_ENV=demo so respx mocks
    (which target the demo host) always match. Invariant #6: no live calls."""
    from alphamarket.config import get_settings

    settings = get_settings()
    original = (settings.vault_key, settings.kalshi_env)
    settings.vault_key = Fernet.generate_key().decode()
    settings.kalshi_env = "demo"
    yield
    settings.vault_key, settings.kalshi_env = original


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def client(session_factory):
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
