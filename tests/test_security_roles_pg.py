"""docs/security_roles.sql on real Postgres: the web role cannot read
credential ciphertext yet can connect and revoke; the worker role decrypts.

Roles are cluster-wide, so the test renames them uniquely per run."""

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlalchemy.orm import sessionmaker

from alphamarket import audit, credentials
from alphamarket.config import get_settings
from alphamarket.models import User, VenueAccount

pytestmark = pytest.mark.pg

PG_ADMIN_URL = os.environ.get(
    "KOINE_TEST_PG_URL",
    "postgresql+psycopg://alphamarket:alphamarket@localhost:5433/alphamarket_test",
)
ROLES_SQL = Path(__file__).parent.parent / "docs" / "security_roles.sql"


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


@pytest.fixture()
def roles_db():
    from alembic import command
    from alembic.config import Config

    admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    suffix = uuid.uuid4().hex[:10]
    name, web, worker = f"koine_roles_{suffix}", f"koine_web_{suffix}", f"koine_worker_{suffix}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        admin.dispose()
        pytest.skip("Postgres test DB not reachable")
    owner_url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    settings = get_settings()
    original = settings.database_url
    settings.database_url = owner_url
    try:
        cfg = Config()
        cfg.set_main_option("script_location", "alembic")
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = original

    sql = (
        ROLES_SQL.read_text()
        .replace("\\set ON_ERROR_STOP on", "")
        .replace(":web_pw", "'web-pw'")
        .replace(":worker_pw", "'worker-pw'")
        .replace("koine_worker", worker)
        .replace("koine_web", web)
    )
    with psycopg.connect(_libpq(owner_url), autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)  # idempotent: re-run after every migration

    host = owner_url.split("@", 1)[1]
    yield (
        f"postgresql+psycopg://{web}:web-pw@{host}",
        f"postgresql+psycopg://{worker}:worker-pw@{host}",
    )
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        conn.execute(text(f'DROP ROLE IF EXISTS "{web}"'))
        conn.execute(text(f'DROP ROLE IF EXISTS "{worker}"'))
    admin.dispose()


def test_web_role_cannot_read_ciphertext_but_app_paths_work(roles_db):
    web_url, worker_url = roles_db
    web = create_engine(web_url)
    worker = create_engine(worker_url)
    WebSession = sessionmaker(bind=web, expire_on_commit=False)
    WorkerSession = sessionmaker(bind=worker, expire_on_commit=False)

    # Web: sign-up, connect (insert the credential), list, audit.
    with WebSession() as db:
        user = User(email="roles@x.com", password_hash="x", display_name="R")
        db.add(user)
        db.flush()
        account = VenueAccount(user_id=user.id, venue="kalshi", venue_env="demo", key_id="k")
        db.add(account)
        db.flush()
        credentials.store(db, account, "web-stored-pem", kind="rsa_pem", actor_user_id=user.id)
        db.commit()
        account_id = account.id
        assert credentials.active_credential(db, account).key_version.startswith("local:")
        assert db.scalars(select(VenueAccount)).one().id == account_id

    for column, table in (
        ("ciphertext", "venue_credentials"),
        ("wrapped_dek", "venue_credentials"),
        ("refresh_ciphertext", "venue_credentials"),
        ("encrypted_private_key", "venue_accounts"),
    ):
        with web.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(f"SELECT {column} FROM {table}"))

    # Worker: the only role that decrypts.
    with WorkerSession() as db, audit.job_context("sync"):
        account = db.get(VenueAccount, account_id)
        opened = credentials.open_credential(db, account, purpose="sync")
        assert opened.secret == "web-stored-pem"

    # Web: disconnect erases the key without ever reading it.
    with WebSession() as db:
        credentials.disconnect(db, db.get(VenueAccount, account_id), actor_user_id=None)
    with worker.connect() as conn:
        assert conn.execute(text("SELECT ciphertext FROM venue_credentials")).scalar() is None
        actions = conn.execute(text("SELECT action FROM audit_log ORDER BY id")).scalars().all()
    assert actions == ["credential.connect", "credential.decrypt", "credential.disconnect"]

    # Neither role can rewrite history.
    for engine in (web, worker):
        with engine.connect() as conn, pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text("DELETE FROM audit_log"))
    web.dispose()
    worker.dispose()
