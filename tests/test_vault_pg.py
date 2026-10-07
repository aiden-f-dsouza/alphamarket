"""Migration 0012 on real Postgres: audit_log append-only trigger, the copy of
v1 keys into venue_credentials, and a working downgrade."""

import os
import uuid

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import sessionmaker

from alphamarket import audit, credentials, vault
from alphamarket.config import get_settings
from alphamarket.models import AuditLog, User, VenueAccount

pytestmark = pytest.mark.pg

PG_ADMIN_URL = os.environ.get(
    "KOINE_TEST_PG_URL",
    "postgresql+psycopg://alphamarket:alphamarket@localhost:5433/alphamarket_test",
)


def test_audit_log_trigger_blocks_update_and_delete(pg_session_factory):
    with pg_session_factory() as db:
        row = audit.record(db, "admin.action", details={"op": "pg-test"})
        db.commit()
        row_id = row.id
        for sql in (
            "UPDATE audit_log SET action = 'x' WHERE id = :id",
            "DELETE FROM audit_log WHERE id = :id",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                db.execute(text(sql), {"id": row_id})
            db.rollback()
        assert db.get(AuditLog, row_id).action == "admin.action"
        names = set(
            db.scalars(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE '%_append_only'"))
        )
    assert "audit_log_append_only" in names


def test_rotation_under_row_locks(pg_session_factory):
    with pg_session_factory() as db:
        user = User(email=f"{uuid.uuid4().hex}@x.com", password_hash="x", display_name="P")
        db.add(user)
        db.flush()
        account = VenueAccount(user_id=user.id, venue="kalshi", venue_env="demo", key_id="k")
        db.add(account)
        db.flush()
        credentials.store(db, account, "pg-secret", kind="rsa_pem")
        db.commit()
        settings = get_settings()
        settings.vault_key = f"{Fernet.generate_key().decode()},{settings.vault_key}"
        assert credentials.rotate_all(db, batch_size=10) >= 1
        assert credentials.open_credential(db, account, purpose="t").secret == "pg-secret"


@pytest.fixture()
def scratch_db():
    """A private database so this test can walk migrations up and down."""
    from alembic.config import Config

    admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"koine_mig_{uuid.uuid4().hex[:12]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        admin.dispose()
        pytest.skip("Postgres test DB not reachable")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    cfg = Config()
    cfg.set_main_option("script_location", "alembic")
    settings = get_settings()
    original = settings.database_url
    settings.database_url = url
    yield url, cfg
    settings.database_url = original
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def test_migration_copies_v1_keys_and_downgrades(scratch_db):
    from alembic import command

    url, cfg = scratch_db
    command.upgrade(cfg, "0008")
    engine = create_engine(url)
    v1_blob = Fernet(get_settings().vault_key).encrypt(b"v1-pem")
    with engine.begin() as conn:
        uid = conn.execute(
            text(
                "INSERT INTO users (email, password_hash, display_name, created_at) "
                "VALUES ('m@x.com', 'x', 'M', now()) RETURNING id"
            )
        ).scalar()
        aid = conn.execute(
            text(
                "INSERT INTO venue_accounts (user_id, venue, venue_env, key_id, "
                "encrypted_private_key, status, created_at) VALUES "
                "(:u, 'kalshi', 'demo', 'k', :blob, 'active', now()) RETURNING id"
            ),
            {"u": uid, "blob": v1_blob},
        ).scalar()

    # Head, not 0012: the ORM below maps every current column (0017 added
    # venue_accounts.is_watch), and the downgrade then runs through all of them.
    command.upgrade(cfg, "head")
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        kind, version, wrapped = db.execute(
            text("SELECT kind, key_version, wrapped_dek FROM venue_credentials")
        ).one()
        assert (kind, version, wrapped) == ("rsa_pem", "legacy", None)
        account = db.get(VenueAccount, aid)
        assert credentials.open_credential(db, account, purpose="t").secret == "v1-pem"
        # Rotation turns the legacy row into an envelope row.
        assert credentials.rotate_all(db) == 1
        # And a second, envelope-only account connected after the migration.
        second = VenueAccount(user_id=uid, venue="kalshi", venue_env="demo", key_id="k2")
        db.add(second)
        db.flush()
        credentials.store(db, second, "v2-pem", kind="rsa_pem")
        db.commit()
        second_id = second.id

    command.downgrade(cfg, "0008")
    with engine.connect() as conn:
        rows = dict(
            conn.execute(text("SELECT id, encrypted_private_key FROM venue_accounts")).all()
        )
    # The v1 column still held the original blob; the envelope-only account
    # got a self-contained kv2 blob that vault.decrypt_secret opens.
    assert vault.decrypt_secret(bytes(rows[aid])) == "v1-pem"
    assert bytes(rows[second_id]).startswith(b"kv2.")
    assert vault.decrypt_secret(bytes(rows[second_id])) == "v2-pem"

    command.upgrade(cfg, "head")  # and back up again cleanly
    engine.dispose()
