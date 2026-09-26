"""Credentials v2 + audit log: connect/disconnect/rotate through venue_credentials,
every decrypt audited with a request or job id. Venue traffic is respx-mocked."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from alphamarket import audit, credentials, vault
from alphamarket.config import get_settings
from alphamarket.credentials import CredentialUnavailable
from alphamarket.ingest import connector_for
from alphamarket.models import (
    AppendOnlyViolation,
    AuditLog,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
    VenueCredential,
)
from authutil import secure_account

BASE = "https://demo-api.kalshi.co/trade-api/v2"
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


PEM = _pem()


def _mock_kalshi(scopes=("read",)):
    respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_balance.json"))
    )
    keys = {"api_keys": [{"api_key_id": "demo-key-id", "name": "k", "scopes": list(scopes)}]}
    respx.get(f"{BASE}/api_keys").mock(return_value=httpx.Response(200, json=keys))
    respx.get(f"{BASE}/portfolio/fills").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_fills.json"))
    )
    respx.get(f"{BASE}/portfolio/settlements").mock(
        return_value=httpx.Response(200, json={"settlements": [], "cursor": None})
    )
    respx.get(f"{BASE}/portfolio/positions").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_positions.json"))
    )


@pytest.fixture()
def user(client):
    resp = client.post(
        "/auth/signup",
        json={"email": "c@example.com", "password": "hunter2hunter2", "display_name": "Cred"},
    )
    assert resp.status_code == 201
    secure_account(client, "c@example.com")  # connecting a key needs email + TOTP (P3)
    return resp.json()


def _connect(client, **extra):
    return client.post(
        "/venues",
        json={
            "venue": "kalshi",
            "key_id": "demo-key-id",
            "private_key_pem": PEM,
            "strategy_name": "Vault test",
            **extra,
        },
    )


def _audit(db, action=None) -> list[AuditLog]:
    q = select(AuditLog).order_by(AuditLog.id)
    if action:
        q = q.where(AuditLog.action == action)
    return list(db.scalars(q).all())


@respx.mock
def test_connect_stores_envelope_credential_and_audits(client, user, session_factory):
    _mock_kalshi()
    resp = _connect(client, account_ref="main")
    assert resp.status_code == 201, resp.text
    assert resp.headers["x-request-id"].startswith("req-")
    with session_factory() as db:
        account = db.scalars(select(VenueAccount)).one()
        assert account.encrypted_private_key is None
        cred = db.scalars(select(VenueCredential)).one()
        assert cred.kind == "rsa_pem" and cred.account_ref == "main"
        assert cred.key_version.startswith("local:")
        assert vault.open_record(cred.ciphertext, cred.wrapped_dek, cred.key_version)[0] == PEM
        # Evidence from prove_read_only() is kept on the account.
        assert account.scope_evidence["read_only"] is True
        assert account.scope_evidence["method"] == "scope_endpoint"
        assert account.scope_checked_at is not None

        (connect,) = _audit(db, "credential.connect")
        assert connect.actor_user_id == user["id"]
        assert connect.target_type == "venue_account" and connect.target_id == str(account.id)
        assert connect.ip == "testclient" and connect.user_agent == "testclient"
        assert connect.request_or_job_id == resp.headers["x-request-id"]
        (scope,) = _audit(db, "scope.check")
        assert scope.details["trigger"] == "connect" and scope.details["read_only"] is True
        # The first import (sync_account) re-opened the stored key: audited too.
        (decrypt,) = _audit(db, "credential.decrypt")
        assert decrypt.request_or_job_id == resp.headers["x-request-id"]
        # No audit row anywhere carries the key.
        assert all(PEM not in json.dumps(r.details) for r in _audit(db))


@respx.mock
def test_writable_key_rejected_and_audited(client, user, session_factory):
    _mock_kalshi(scopes=("read", "write"))
    resp = _connect(client)
    assert resp.status_code == 422
    with session_factory() as db:
        assert db.scalars(select(VenueCredential)).all() == []
        (row,) = _audit(db, "credential.rejected")
        assert row.actor_user_id == user["id"] and row.target_id == "kalshi"
        assert row.details["read_only"] is False


@respx.mock
def test_every_decrypt_is_audited_with_request_or_job_id(client, user, session_factory):
    _mock_kalshi()
    account_id = _connect(client).json()["id"]

    resp = client.post(f"/venues/{account_id}/sync")
    assert resp.status_code == 200, resp.text
    with session_factory() as db:
        _connect_decrypt, row = _audit(db, "credential.decrypt")
        assert row.request_or_job_id == resp.headers["x-request-id"]
        assert row.details["purpose"] == "sync" and row.details["source"] == "venue_credentials"
        assert row.target_id == str(account_id) and row.ip == "testclient"

    with session_factory() as db, audit.job_context("sync") as job_id:
        account = db.get(VenueAccount, account_id)
        connector_for(account)
        connector_for(account, purpose="reconcile")
    with session_factory() as db:
        rows = _audit(db, "credential.decrypt")
        assert len(rows) == 4  # one per decrypt (connect, sync, 2 here), never batched
        assert [r.request_or_job_id for r in rows[2:]] == [job_id, job_id]
        assert job_id.startswith("sync-")

    # No request/job context at all still yields an id (never NULL).
    with session_factory() as db:
        connector_for(db.get(VenueAccount, account_id))
    with session_factory() as db:
        assert _audit(db, "credential.decrypt")[-1].request_or_job_id.startswith("adhoc-")


@respx.mock
def test_disconnect_erases_credential_and_audits(client, user, session_factory):
    _mock_kalshi()
    account_id = _connect(client).json()["id"]
    assert client.delete(f"/venues/{account_id}").status_code == 204
    with session_factory() as db:
        cred = db.scalars(select(VenueCredential)).one()
        assert cred.ciphertext is None and cred.wrapped_dek is None
        assert cred.refresh_ciphertext is None and cred.revoked_at is not None
        (row,) = _audit(db, "credential.disconnect")
        assert row.actor_user_id == user["id"]
        assert row.details["revoked_credential_ids"] == [cred.id]
        account = db.get(VenueAccount, account_id)
        with pytest.raises(CredentialUnavailable):
            credentials.open_credential(db, account, purpose="sync")


@respx.mock
def test_dashboard_disconnect_uses_the_same_path(client, user, session_factory):
    _mock_kalshi()
    account_id = _connect(client).json()["id"]
    resp = client.post(f"/dashboard/accounts/{account_id}/disconnect", follow_redirects=False)
    assert resp.status_code == 303
    with session_factory() as db:
        assert db.scalars(select(VenueCredential)).one().ciphertext is None
        assert len(_audit(db, "credential.disconnect")) == 1


def _account(db, **kw) -> VenueAccount:
    owner = User(email=f"{kw.get('key_id', 'k')}@x.com", password_hash="x", display_name="O")
    db.add(owner)
    db.flush()
    kw.setdefault("key_id", "k")
    account = VenueAccount(user_id=owner.id, venue="kalshi", venue_env="demo", **kw)
    db.add(account)
    db.flush()
    db.add(Strategy(user_id=owner.id, venue_account_id=account.id, name="s", slug=f"s{account.id}"))
    db.commit()
    return account


def test_legacy_column_is_read_as_fallback_and_audited(session_factory):
    with session_factory() as db:
        legacy = Fernet(get_settings().vault_key).encrypt(b"old-pem")
        account = _account(db, encrypted_private_key=legacy)
        opened = credentials.open_credential(db, account, purpose="sync")
        assert opened.secret == "old-pem"
        (row,) = _audit(db, "credential.decrypt")
        assert row.details["source"] == "legacy_column"


def test_rotate_all_rewraps_in_batches_and_old_key_can_go(session_factory):
    settings = get_settings()
    old_key = settings.vault_key
    with session_factory() as db:
        accounts = [_account(db, key_id=f"k{i}") for i in range(3)]
        for i, account in enumerate(accounts):
            credentials.store(db, account, f"secret-{i}", kind="rsa_pem")
        # One account still only in the legacy column (connected pre-0012).
        legacy_account = _account(
            db, key_id="legacy", encrypted_private_key=Fernet(old_key).encrypt(b"secret-L")
        )
        # One revoked credential: never rotated, stays erased.
        gone = _account(db, key_id="gone")
        credentials.store(db, gone, "bye", kind="rsa_pem")
        credentials.disconnect(db, gone, actor_user_id=None)
        db.commit()

        new_key = Fernet.generate_key().decode()
        settings.vault_key = f"{new_key},{old_key}"
        target = vault.current_version()
        with audit.job_context("key_rotate") as job_id:
            # 3 envelope rows + 1 adopted legacy row = 4 rotations.
            assert credentials.rotate_all(db, batch_size=2) == 4
        assert credentials.rotate_all(db, batch_size=2) == 0  # idempotent

        live = db.scalars(select(VenueCredential).where(VenueCredential.revoked_at.is_(None))).all()
        assert {c.key_version for c in live} == {target}
        assert all(c.rotated_at is not None and c.wrapped_dek is not None for c in live)
        rotations = _audit(db, "credential.rotate")
        assert len(rotations) == 4 and {r.request_or_job_id for r in rotations} == {job_id}
        # Only the legacy row needed its plaintext (re-sealed); rewraps never do.
        (legacy_decrypt,) = (
            r for r in _audit(db, "credential.decrypt") if r.details["purpose"] == "rotate"
        )
        assert legacy_decrypt.details["key_version"] == "legacy"

        settings.vault_key = new_key  # old key retired: everything still opens
        for i, account in enumerate(accounts):
            assert credentials.open_credential(db, account, purpose="t").secret == f"secret-{i}"
        assert credentials.open_credential(db, legacy_account, purpose="t").secret == "secret-L"


def test_suspended_account_never_decrypts(session_factory):
    with session_factory() as db:
        account = _account(db)
        credentials.store(db, account, "pem", kind="rsa_pem")
        account.status = VenueAccountStatus.SUSPENDED
        db.commit()
        with pytest.raises(CredentialUnavailable):
            credentials.open_credential(db, account, purpose="sync")
        assert _audit(db, "credential.decrypt") == []


def test_store_replaces_previous_credential(session_factory):
    with session_factory() as db:
        account = _account(db)
        first = credentials.store(db, account, "one", kind="rsa_pem")
        second = credentials.store(db, account, "two", kind="rsa_pem")
        db.commit()
        assert first.revoked_at is not None and first.ciphertext is None
        assert credentials.active_credential(db, account).id == second.id
        assert credentials.open_credential(db, account, purpose="t").secret == "two"


def test_audit_log_is_append_only_in_the_orm(session_factory):
    with session_factory() as db:
        row = audit.record(db, "admin.action", details={"op": "test"})
        db.commit()
        row.action = "tampered"
        with pytest.raises(AppendOnlyViolation):
            db.commit()
        db.rollback()
        db.delete(db.get(AuditLog, row.id))
        with pytest.raises(AppendOnlyViolation):
            db.commit()


def test_audit_details_are_scrubbed(session_factory):
    with session_factory() as db:
        row = audit.record(
            db,
            "admin.action",
            details={
                "op": "reclassify",
                "secret": "hunter2",
                "nested": {"refresh_token": "abc", "note": f"pem was {PEM}"},
                "raw": b"\x00bytes",
                "ids": (1, 2),
            },
        )
        db.commit()
        d = db.get(AuditLog, row.id).details
        assert d["op"] == "reclassify" and d["secret"] == "[REDACTED]"
        assert d["nested"]["refresh_token"] == "[REDACTED]"
        assert "BEGIN" not in d["nested"]["note"] or "[REDACTED PEM]" in d["nested"]["note"]
        assert PEM.splitlines()[1] not in json.dumps(d)
        assert d["raw"] == "[bytes]" and d["ids"] == [1, 2]
