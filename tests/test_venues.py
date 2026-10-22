"""Key connect flow (M1.2). All venue traffic is respx-mocked -- invariant #6."""

import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from alphamarket.models import Fill, Strategy, VenueAccount, VenueCredential
from alphamarket.money import to_micros
from authutil import consent, secure_account

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
    secure_account(client, "t@example.com")  # P3: email + TOTP + step-up before connect
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
    respx.get(f"{BASE}/portfolio/positions").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_positions.json"))
    )


def _connect(client, pem=None):
    return client.post(
        "/venues",
        json={
            "venue": "kalshi",
            "key_id": "demo-key-id",
            "private_key_pem": pem or _pem(),
            "strategy_name": "BTC hourly fade",
            "consent_sha256": consent("kalshi"),
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
        # Encrypted at rest (vault v2): an envelope row in venue_credentials,
        # not the PEM; the legacy column is no longer written.
        cred = db.scalars(select(VenueCredential)).one()
        assert cred.venue_account_id == account.id and cred.revoked_at is None
        assert cred.ciphertext is not None and cred.wrapped_dek is not None
        assert pem.encode() not in cred.ciphertext
        assert account.encrypted_private_key is None
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
    secure_account(client, "other@example.com")  # P3: DELETE needs a step-up first
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
        assert account.anchor_positions == [
            {
                "ticker": "KXETH15M-26AUG312315-15",
                "position_micros": -840_000,
                "cost_micros": 478_800,
            }
        ]

    # The fixture fill predates the anchor, so the ledger explains the balance exactly.
    resp = client.post(f"/venues/{account_id}/reconcile")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reconciled"] is True
    assert body["divergence_micros"] == 0
    assert body["venue_balance_micros"] == to_micros("1101.8980")


# ---- Polymarket: address-keyed, no secret, ownership by profile claim (M7) ----

POLY_DATA = "https://data-api.polymarket.com"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_ADDRESS = "0x124628E686dB22da39b6Ee43CA786C8A074BD6F2"


def _mock_polymarket(bio: str = "getting adversely selected"):
    profile = dict(_fixture("polymarket_profile.json"), bio=bio)
    respx.get(f"{POLY_GAMMA}/public-profile").mock(return_value=httpx.Response(200, json=profile))
    respx.get(f"{POLY_DATA}/v2/activity", params__contains={"type": "TRADE"}).mock(
        return_value=httpx.Response(200, json=_fixture("polymarket/activity_trades_20261009.json"))
    )
    respx.get(f"{POLY_DATA}/v2/activity", params__contains={"type": "REDEEM"}).mock(
        return_value=httpx.Response(200, json=_fixture("polymarket/activity_redeems_20261009.json"))
    )
    respx.get(f"{POLY_DATA}/v2/positions").mock(
        return_value=httpx.Response(200, json=_fixture("polymarket/positions_20261009.json"))
    )


def _connect_polymarket(client):
    return client.post(
        "/venues",
        json={
            "venue": "polymarket",
            "key_id": POLY_ADDRESS,
            "strategy_name": "Weather fade",
            "consent_sha256": consent("polymarket"),
        },
    )


@respx.mock
def test_polymarket_connect_stores_no_secret_and_starts_unclaimed(client, user, session_factory):
    _mock_polymarket()
    resp = _connect_polymarket(client)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["imported_fills"] == 4 and body["imported_settlements"] == 3
    assert body["claimed"] is False and body["claim_code"].startswith("koine-")
    assert body["verification_tier"] == "unclaimed"
    with session_factory() as db:
        account = db.scalars(select(VenueAccount)).one()
        assert account.encrypted_private_key is None  # nothing secret exists
        assert account.key_id == POLY_ADDRESS.lower()
        assert account.venue_env == "prod"
        # No observable balance: anchored at zero, positions at cost basis.
        assert account.anchor_balance_micros == 0
        assert len(account.anchor_positions) == 2
        assert account.anchor_positions[0]["cost_micros"] == 12_599_900
        fills = db.scalars(select(Fill)).all()
        assert {f.notional_micros for f in fills} == {9_999_999}
        strategy = db.scalars(select(Strategy)).one()
        slug = strategy.slug
    # Unclaimed accounts never reach a public page, even with public=True.
    assert client.get(f"/api/strategies/{slug}").status_code == 404


@respx.mock(assert_all_called=False)
def test_address_connect_rejects_a_malformed_address_before_any_venue_call(
    client, user, session_factory
):
    _mock_polymarket()
    resp = client.post(
        "/venues",
        json={
            "venue": "polymarket",
            "key_id": "0x1234",
            "strategy_name": "Weather fade",
            "consent_sha256": consent("polymarket"),
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "Enter a 0x address (42 characters)"
    assert respx.calls.call_count == 0  # no venue request, no limiter tokens
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


@respx.mock
def test_polymarket_claim_requires_code_in_bio(client, user, session_factory):
    _mock_polymarket()
    account_id = _connect_polymarket(client).json()["id"]
    resp = client.post(f"/venues/{account_id}/claim")
    assert resp.status_code == 422
    assert "koine-" in resp.json()["detail"]
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        code = account.claim_code
        assert account.claimed_at is None
    _mock_polymarket(bio=f"weather trader · {code}")
    resp = client.post(f"/venues/{account_id}/claim")
    assert resp.status_code == 200, resp.text
    assert resp.json()["claimed"] is True
    assert resp.json()["claim_code"] is None
    assert resp.json()["verification_tier"] == "fills_only"
    # Idempotent.
    assert client.post(f"/venues/{account_id}/claim").status_code == 200
    # No balance to reconcile against: refused, never quarantined.
    assert client.post(f"/venues/{account_id}/reconcile").status_code == 409


@respx.mock
def test_polymarket_unknown_address_is_rejected(client, user):
    respx.get(f"{POLY_GAMMA}/public-profile").mock(
        return_value=httpx.Response(404, json={"error": "profile not found"})
    )
    resp = _connect_polymarket(client)
    assert resp.status_code == 422
    assert "Polymarket" in resp.json()["detail"]


def test_kalshi_connect_requires_secret(client, user):
    resp = client.post(
        "/venues", json={"venue": "kalshi", "key_id": "k", "strategy_name": "No key"}
    )
    assert resp.status_code == 422
    assert "PEM" in resp.json()["detail"]
