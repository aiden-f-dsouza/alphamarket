"""Key connect flow (M1.2). All venue traffic is respx-mocked -- invariant #6."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from alphamarket.models import Fill, Strategy, VenueAccount
from alphamarket.money import to_micros

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


@pytest.fixture()
def user(client):
    resp = client.post(
        "/auth/signup",
        json={"email": "t@example.com", "password": "hunter2hunter2", "display_name": "Trader"},
    )
    assert resp.status_code == 201
    return resp.json()


def _mock_venue(scopes=("read",), fills=None, settlements=None):
    respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_balance.json"))
    )
    if scopes is None:  # venue cannot answer the scope question
        keys_response = httpx.Response(500, json={})
    else:
        keys_response = httpx.Response(
            200,
            json={"api_keys": [{"api_key_id": "demo-key-id", "name": "k", "scopes": list(scopes)}]},
        )
    respx.get(f"{BASE}/api_keys").mock(return_value=keys_response)
    respx.get(f"{BASE}/portfolio/fills").mock(
        return_value=httpx.Response(200, json=fills or _fixture("kalshi_fills.json"))
    )
    respx.get(f"{BASE}/portfolio/settlements").mock(
        return_value=httpx.Response(200, json=settlements or {"settlements": [], "cursor": None})
    )


def _connect(client, pem=None):
    return client.post(
        "/venues",
        json={
            "venue": "kalshi",
            "key_id": "demo-key-id",
            "private_key_pem": pem or _pem(),
            "strategy_name": "BTC hourly fade",
        },
    )


@respx.mock
def test_connect_stores_encrypted_key_and_imports(client, user, session_factory):
    _mock_venue()
    pem = _pem()
    resp = _connect(client, pem)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["strategy_name"] == "BTC hourly fade"
    assert body["imported_fills"] == 1
    assert body["imported_settlements"] == 0
    # Key material never appears in the response.
    assert pem not in resp.text
    assert "private" not in resp.text.lower()

    with session_factory() as db:
        account = db.scalars(select(VenueAccount)).one()
        # Encrypted at rest: stored bytes are a Fernet token, not the PEM.
        assert account.encrypted_private_key is not None
        assert pem.encode() not in account.encrypted_private_key
        assert account.scope_checked_at is not None
        (fill,) = db.scalars(select(Fill)).all()
        assert fill.venue_fill_id == "e9efc650-5012-75a0-6383-21440fdfc3aa"
        assert fill.fee_micros == 139_800


@respx.mock
def test_connect_rejects_write_scope_key(client, user, session_factory):
    _mock_venue(scopes=("read", "write"))
    resp = _connect(client)
    assert resp.status_code == 422
    assert "read-only" in resp.json()["detail"]
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


@respx.mock
def test_connect_accepts_undetermined_scope_but_records_unchecked(client, user, session_factory):
    _mock_venue(scopes=None)
    resp = _connect(client)
    assert resp.status_code == 201
    with session_factory() as db:
        account = db.scalars(select(VenueAccount)).one()
        assert account.scope_checked_at is None


@respx.mock
def test_connect_rejects_bad_credentials_without_echoing_key(client, user):
    respx.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(401, json={}))
    pem = _pem()
    resp = _connect(client, pem)
    assert resp.status_code == 422
    assert pem not in resp.text


def test_connect_requires_login(client):
    resp = _connect(client)
    assert resp.status_code == 401


@respx.mock
def test_disconnect_erases_key_but_keeps_ledger(client, user, session_factory):
    _mock_venue()
    account_id = _connect(client).json()["id"]

    resp = client.delete(f"/venues/{account_id}")
    assert resp.status_code == 204

    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.encrypted_private_key is None
        assert account.status == "disconnected"
        assert len(db.scalars(select(Fill)).all()) == 1  # ledger survives

    # A disconnected account cannot be synced.
    assert client.post(f"/venues/{account_id}/sync").status_code == 409


@respx.mock
def test_accounts_are_scoped_to_their_owner(client, user, session_factory):
    _mock_venue()
    account_id = _connect(client).json()["id"]

    client.post("/auth/logout")
    client.post(
        "/auth/signup",
        json={"email": "other@example.com", "password": "hunter2hunter2", "display_name": "Other"},
    )
    assert client.get("/venues").json() == []
    assert client.delete(f"/venues/{account_id}").status_code == 404
    assert client.post(f"/venues/{account_id}/sync").status_code == 404

    with session_factory() as db:
        strategy = db.scalars(select(Strategy)).one()
        assert strategy.venue_account_id == account_id


@respx.mock
def test_connect_records_reconciliation_anchor_and_reconciles(client, user, session_factory):
    _mock_venue()
    account_id = _connect(client).json()["id"]

    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.anchor_balance_micros == to_micros("1101.8980")  # fixture balance
        assert account.anchor_at is not None

    # The fixture fill predates the anchor, so the ledger explains the balance exactly.
    resp = client.post(f"/venues/{account_id}/reconcile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciled"] is True
    assert body["divergence_micros"] == 0
    assert body["venue_balance_micros"] == to_micros("1101.8980")
