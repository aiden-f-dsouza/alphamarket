"""HTML surface and public page (M4)."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from alphamarket.analytics import compute_strategy_stats
from alphamarket.models import Strategy

BASE = "https://demo-api.kalshi.co/trade-api/v2"
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _pem():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _mock_venue():
    respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_balance.json"))
    )
    respx.get(f"{BASE}/api_keys").mock(
        return_value=httpx.Response(
            200, json={"api_keys": [{"api_key_id": "k", "name": "k", "scopes": ["read"]}]}
        )
    )
    respx.get(f"{BASE}/portfolio/fills").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_fills.json"))
    )
    respx.get(f"{BASE}/portfolio/settlements").mock(
        return_value=httpx.Response(200, json={"settlements": [], "cursor": None})
    )
    respx.get(f"{BASE}/portfolio/positions").mock(
        return_value=httpx.Response(200, json={"market_positions": [], "cursor": ""})
    )


@pytest.fixture()
def logged_in(client):
    resp = client.post(
        "/signup-form",
        data={"email": "t@example.com", "password": "hunter2hunter2", "display_name": "Trader"},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
    return client


@pytest.fixture()
def connected(logged_in, session_factory):
    with respx.mock:
        _mock_venue()
        resp = logged_in.post(
            "/dashboard/connect",
            data={"key_id": "k", "private_key_pem": _pem(), "strategy_name": "BTC Hourly Fade"},
            follow_redirects=False,
        )
    assert resp.status_code == 303, resp.text
    with session_factory() as db:
        strategy = db.scalars(select(Strategy)).one()
        compute_strategy_stats(db, strategy, as_of=datetime.now(UTC) + timedelta(days=3))
        return {"id": strategy.id, "slug": strategy.slug}


def test_landing_and_login_flow(client):
    assert client.get("/").status_code == 200
    assert client.get("/dashboard", follow_redirects=False).headers["location"] == "/"
    client.post(
        "/signup-form",
        data={"email": "a@example.com", "password": "hunter2hunter2", "display_name": "Ann"},
    )
    assert "Welcome, Ann" in client.get("/dashboard").text
    bad = client.post("/login-form", data={"email": "a@example.com", "password": "wrong"})
    assert "Invalid credentials" in bad.text


def test_connect_form_and_dashboard(connected, logged_in):
    assert connected["slug"] == f"btc-hourly-fade-{connected['id']}"
    html = logged_in.get("/dashboard").text
    assert "BTC Hourly Fade" in html
    assert "read-only verified" in html
    assert "Public page" in html
    assert "-----BEGIN" not in html  # key material never rendered


def test_public_page_shows_platform_numbers_and_labels_description(connected, logged_in):
    logged_in.post(
        f"/dashboard/strategy/{connected['id']}",
        data={
            "name": "BTC Hourly Fade",
            "description": "I fade the open.",
            "style_tags": "btc, mean-reversion",
            "public": "on",
        },
    )
    html = logged_in.get(f"/s/{connected['slug']}").text
    assert "BTC Hourly Fade" in html
    assert "by Trader" in html
    assert "t@example.com" not in html  # privacy default: display name only
    assert "I fade the open." in html
    assert "unverified" in html.lower()
    assert "platform-verified since" in html
    assert "incubating" in html
    assert "mean-reversion" in html

    data = logged_in.get(f"/api/strategies/{connected['slug']}").json()
    assert data["stats"]["risk_score"] >= 1
    assert data["stats"]["equity_curve"][0][1] == pytest.approx(1101.898)
    assert "email" not in json.dumps(data)


def test_public_page_hidden_when_unlisted_or_quarantined(connected, logged_in, session_factory):
    logged_in.post(
        f"/dashboard/strategy/{connected['id']}",
        data={"name": "BTC Hourly Fade"},  # checkbox absent -> public off
    )
    assert logged_in.get(f"/s/{connected['slug']}").status_code == 404
    assert logged_in.get(f"/api/strategies/{connected['slug']}").status_code == 404

    logged_in.post(
        f"/dashboard/strategy/{connected['id']}", data={"name": "BTC Hourly Fade", "public": "on"}
    )
    assert logged_in.get(f"/s/{connected['slug']}").status_code == 200
    with session_factory() as db:
        db.get(Strategy, connected["id"]).status = "quarantined"
        db.commit()
    assert logged_in.get(f"/s/{connected['slug']}").status_code == 404


def test_listing_editor_api_cannot_touch_numbers(connected, logged_in):
    resp = logged_in.patch(
        f"/strategies/{connected['id']}", json={"description": "new", "risk_score": 1}
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "new"
    assert "risk_score" not in resp.json()


def test_methodology_renders(client):
    html = client.get("/methodology").text
    assert "Reconciliation" in html and "Risk score" in html
