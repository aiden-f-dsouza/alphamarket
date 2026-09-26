"""Nightly read-only proof re-check: evidence stored, writable keys suspended
and erased, users alerted. Kalshi traffic is respx-mocked (invariant 6)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import alerts, credentials, scopecheck, worker
from alphamarket.connectors.base import ReadOnlyProof
from alphamarket.models import AuditLog, Strategy, User, VenueAccount, VenueCredential

BASE = "https://demo-api.kalshi.co/trade-api/v2"
FIXTURES = Path(__file__).parent / "fixtures"


def _pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


PEM = _pem()


def _scopes(scopes):
    if scopes is None:
        resp = httpx.Response(500, json={})
    else:
        resp = httpx.Response(
            200, json={"api_keys": [{"api_key_id": "demo-key-id", "scopes": list(scopes)}]}
        )
    return respx.get(f"{BASE}/api_keys").mock(return_value=resp)


@pytest.fixture()
def captured_alerts():
    seen: list[alerts.SecurityAlert] = []
    alerts.register(seen.append)
    yield seen
    alerts.unregister(seen.append)


def _account(db, venue="kalshi", key_id="demo-key-id") -> VenueAccount:
    owner = User(email=f"{venue}@x.com", password_hash="x", display_name="O")
    db.add(owner)
    db.flush()
    account = VenueAccount(user_id=owner.id, venue=venue, venue_env="demo", key_id=key_id)
    if venue == "polymarket":
        account.venue_env = "prod"
    db.add(account)
    db.flush()
    db.add(Strategy(user_id=owner.id, venue_account_id=account.id, name="s", slug=f"s{account.id}"))
    if venue == "kalshi":
        credentials.store(db, account, PEM, kind="rsa_pem")
    db.commit()
    return account


def _actions(db) -> list[str]:
    return list(db.scalars(select(AuditLog.action).order_by(AuditLog.id)).all())


@respx.mock
def test_read_only_key_passes_and_evidence_is_stored(session_factory, captured_alerts):
    _scopes(["read"])
    with session_factory() as db:
        account = _account(db)
        evidence = scopecheck.recheck_account(db, account)
        assert evidence.read_only is True
        assert account.status == "active"
        assert account.scope_evidence["read_only"] is True
        assert account.scope_evidence["method"] == ReadOnlyProof.SCOPE_ENDPOINT
        assert account.scope_checked_at == evidence.checked_at
        assert _actions(db)[-2:] == ["credential.decrypt", "scope.check"]
        check = db.scalars(select(AuditLog).where(AuditLog.action == "scope.check")).one()
        assert check.details["trigger"] == "nightly"
    assert captured_alerts == []


@respx.mock
def test_writable_key_is_suspended_erased_audited_and_alerted(session_factory, captured_alerts):
    _scopes(["read", "write"])
    with session_factory() as db:
        account = _account(db)
        evidence = scopecheck.recheck_account(db, account)
        assert evidence.read_only is False
        assert account.status == "suspended"
        assert account.scope_checked_at is None  # never confirmed read-only
        assert "Suspended" in account.last_sync_error
        cred = db.scalars(select(VenueCredential)).one()
        assert cred.ciphertext is None and cred.revoked_at is not None
        actions = _actions(db)
        assert actions[-3:] == ["scope.check", "credential.disconnect", "account.suspend"]
        disconnect = db.scalars(
            select(AuditLog).where(AuditLog.action == "credential.disconnect")
        ).one()
        assert disconnect.details["reason"] == "scope_writable"
        with pytest.raises(credentials.CredentialUnavailable):
            credentials.open_credential(db, account, purpose="sync")
    (alert,) = captured_alerts
    assert alert.kind == "key_suspended" and alert.user_id == account.user_id
    assert PEM not in alert.body


@respx.mock
def test_undetermined_scope_keeps_account_and_last_confirmation(session_factory):
    _scopes(None)  # venue cannot answer
    confirmed = datetime(2026, 9, 1, tzinfo=UTC)
    with session_factory() as db:
        account = _account(db)
        account.scope_checked_at = confirmed
        db.commit()
        evidence = scopecheck.recheck_account(db, account)
        assert evidence.read_only is None
        assert account.status == "active"
        assert account.scope_checked_at.replace(tzinfo=UTC) == confirmed
        assert account.scope_evidence["read_only"] is None


def test_probe_exception_is_undetermined_not_a_pass():
    class Boom:
        def prove_read_only(self):
            raise httpx.ConnectError("down")

    evidence = scopecheck.prove(Boom())
    assert evidence.read_only is None
    assert evidence.details == {"error": "ConnectError"}


@respx.mock
def test_recheck_all_counts_and_skips_inactive(session_factory, captured_alerts, monkeypatch):
    _scopes(["read", "write"])
    with session_factory() as db:
        kalshi = _account(db)
        poly = _account(db, venue="polymarket", key_id="0x" + "ab" * 20)
        counts = scopecheck.recheck_all(db)
        # Kalshi writable -> suspended; Polymarket holds no secret -> read-only.
        assert counts == {"read_only": 1, "undetermined": 0, "suspended": 1, "failed": 0}
        assert kalshi.status == "suspended" and poly.status == "active"
        # A second sweep skips the suspended account entirely.
        assert scopecheck.recheck_all(db)["suspended"] == 0

    # The worker's pollers no longer touch it either.
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    synced: list[int] = []
    monkeypatch.setattr(worker, "sync_account", lambda db, a: synced.append(a.id))
    monkeypatch.setattr(worker, "emit_all_signals", lambda: None)
    monkeypatch.setattr(worker, "mirror_all_allocations", lambda: None)
    worker.sync_all_accounts()
    assert synced == [poly.id]


@respx.mock
def test_worker_recheck_runs_under_a_job_id(session_factory, monkeypatch):
    _scopes(["read"])
    with session_factory() as db:
        _account(db)
    monkeypatch.setattr(worker, "SessionLocal", session_factory)
    worker.recheck_all_scopes()
    with session_factory() as db:
        ids = set(
            db.scalars(
                select(AuditLog.request_or_job_id).where(
                    AuditLog.action.in_(["credential.decrypt", "scope.check"])
                )
            ).all()
        )
    (job_id,) = ids
    assert job_id.startswith("scope_recheck-")


def test_alert_hook_failure_does_not_stop_other_hooks(captured_alerts):
    def broken(alert):
        raise RuntimeError("smtp down")

    alerts.register(broken)
    try:
        delivered = alerts.emit(alerts.SecurityAlert("key_suspended", 1, "s", "b"))
    finally:
        alerts.unregister(broken)
    # default log hook + capture hook succeed, the broken one is skipped
    assert delivered == 2 and len(captured_alerts) == 1


def test_connect_evidence_json_is_plain_json():
    from alphamarket.connectors.base import ReadOnlyEvidence

    ev = ReadOnlyEvidence(
        method=ReadOnlyProof.ORDER_PROBE,
        read_only=True,
        checked_at=datetime(2026, 9, 26, 4, 15, tzinfo=UTC),
        details={"validate_order": "refused", "api_secret": "leak"},
    )
    out = scopecheck.evidence_json(ev)
    assert json.loads(json.dumps(out)) == out
    assert out["checked_at"] == "2026-09-26T04:15:00Z"
    assert out["details"] == {"validate_order": "refused", "api_secret": "[REDACTED]"}
