"""Invariant 10: a venue whose terms need written approval refuses connects
(API, form and OAuth start) until the approval is on file, unless a non-prod
deployment opts in with ENABLE_UNAPPROVED_VENUES; preflight refuses that."""

import importlib.util
from pathlib import Path

import pytest

from alphamarket.config import get_settings
from alphamarket.connectors.registry import PROFILES
from authutil import secure_account


@pytest.fixture()
def closed(client):
    settings = get_settings()
    settings.enable_unapproved_venues = False
    client.post(
        "/auth/signup",
        json={"email": "g@example.com", "password": "hunter2hunter2", "display_name": "Gee"},
    )
    secure_account(client, "g@example.com")
    yield client
    settings.enable_unapproved_venues = True


def test_the_gated_venues_are_the_ones_awaiting_approval():
    gated = sorted(v for v, p in PROFILES.items() if p.requires_partner_approval)
    assert gated == ["alpaca", "coinbase", "ibkr", "kraken", "lighter", "snaptrade"]


def test_api_connect_is_refused_before_anything_is_checked(closed):
    resp = closed.post(
        "/venues",
        json={"venue": "kraken", "key_id": "k", "secret": "s", "strategy_name": "Kr"},
    )
    assert resp.status_code == 403
    assert "written approval" in resp.json()["detail"]


def test_oauth_start_is_refused(closed):
    resp = closed.get("/oauth/alpaca/start?strategy_name=Eq", follow_redirects=False)
    assert resp.status_code == 403


def test_connect_page_explains_and_disables_submit(closed):
    html = closed.get("/dashboard/connect?venue=coinbase").text
    assert "written approval" in html
    assert 'type="submit" disabled' in html
    # An approved venue is unaffected.
    assert "written approval" not in closed.get("/dashboard/connect?venue=kalshi").text


def test_preflight_refuses_the_dev_override():
    spec = importlib.util.spec_from_file_location(
        "preflight", Path(__file__).parents[1] / "scripts" / "preflight.py"
    )
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    settings = get_settings()
    assert settings.enable_unapproved_venues  # pinned on for tests
    assert any("ENABLE_UNAPPROVED_VENUES" in p for p in preflight.security_problems(settings))
