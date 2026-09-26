"""Unit tests run against in-memory SQLite via dependency override.

Integration tests against real Postgres (db_test on :5433) come in M1+.
SQLite is fine here because the models stick to portable column types.
"""

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alphamarket.db import Base, get_db
from alphamarket.main import create_app


@pytest.fixture(autouse=True)
def test_settings():
    """Pin the cached settings so tests never depend on the developer's .env:
    a throwaway Fernet key for the vault, and KALSHI_ENV=demo so respx mocks
    (which target the demo host) always match. Invariant #6: no live calls, so
    a developer .env can never make the suite send real email, query HIBP,
    count rate limits in the dev database, or wrap keys with a real KMS."""
    from alphamarket import email
    from alphamarket.config import get_settings

    settings = get_settings()
    pinned = {
        "vault_key": Fernet.generate_key().decode(),
        "kalshi_env": "demo",
        "email_backend": "console",
        "hibp_enabled": False,
        "rate_limiter": "memory",
        "trust_proxy_headers": False,
        "key_wrapper": "local",
    }
    original = {name: getattr(settings, name) for name in pinned}
    for name, value in pinned.items():
        setattr(settings, name, value)
    email.set_sender(None)
    yield
    for name, value in original.items():
        setattr(settings, name, value)
    email.set_sender(None)


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


# ---- real Postgres (triggers, SKIP LOCKED, ON CONFLICT) --------------------------

PG_ADMIN_URL = os.environ.get(
    "KOINE_TEST_PG_URL",
    "postgresql+psycopg://alphamarket:alphamarket@localhost:5433/alphamarket_test",
)


@pytest.fixture(scope="session")
def pg_url():
    """A fresh, fully migrated Postgres database for this test session.

    Tests that need real Postgres behaviour (append-only triggers, row locks,
    ON CONFLICT upserts) take this fixture and are marked `@pytest.mark.pg`.
    Each session gets its own database, so parallel runs never collide. Skips
    when db_test is not running: `docker compose up -d db_test`.
    """
    from alembic import command
    from alembic.config import Config

    from alphamarket.config import get_settings

    admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"koine_t_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        admin.dispose()
        pytest.skip("Postgres test DB not reachable (docker compose up -d db_test)")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    settings = get_settings()
    original = settings.database_url
    settings.database_url = url  # alembic/env.py reads the URL from settings
    try:
        cfg = Config()  # no ini file: keeps alembic from reconfiguring test logging
        cfg.set_main_option("script_location", "alembic")
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = original
    yield url
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture()
def pg_session_factory(pg_url):
    engine = create_engine(pg_url)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()
