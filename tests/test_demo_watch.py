"""The private watchlist in the demo builds (invariant 7): with DEMO_MODE on and
nobody logged in, /watch and /watch/sample-1 serve invented sample data under
the "Sample data" strip; adding an address is refused before any venue call;
with DEMO_MODE off the sample routes 404 and nothing changes; a seeded demo
investor's real watch rows carry the strip too."""

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import demo
from alphamarket.config import get_settings
from alphamarket.connectors.hyperliquid import INFO_URL
from alphamarket.models import (
    Strategy,
    StrategyKind,
    StrategyStatus,
    User,
    VenueAccount,
    Watch,
)
from authutil import verify_email

BANNER = "Sample data"
ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"
PASSWORD = "hunter2hunter2"


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def signup(client, email: str) -> None:
    resp = client.post(
        "/signup-form",
        data={"email": email, "password": PASSWORD, "display_name": "User " + email.split("@")[0]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verify_email(client, email)


def test_visitor_sees_the_sample_list(client, demo_on):
    resp = client.get("/watch")
    assert resp.status_code == 200
    html = resp.text
    assert BANNER in html and "Only you can see this page." in html
    for row in demo.watch_list():
        assert row["name"] in html
    assert 'href="/watch/sample-1"' in html
    assert "Adding addresses is off in the demo" in html
    assert 'action="/watch"' not in html and 'name="address"' not in html
    assert re.search(r'<button class="btn" type="button" disabled>', html)
    assert 'href="#"' not in html
    assert 'name="robots" content="noindex' in html
    assert resp.headers["cache-control"] == "private, no-store"


def test_sample_addresses_are_fake_but_well_formed():
    for w in demo.watch_list():
        assert re.fullmatch(r"0x[0-9a-f]{40}", w["address"])
    assert demo.watch_list()[0]["tailing"] is True


def test_visitor_sees_the_sample_page(client, demo_on):
    resp = client.get("/watch/sample-1")
    assert resp.status_code == 200
    html = resp.text
    assert BANNER in html and "Funding-carry desk" in html
    assert "0x5a3c000000000000000000000000000000c0ffee" in html
    assert "private watch" in html
    assert 'href="#"' not in html
    assert "/watch/sample-1/label" not in html and "/remove" not in html
    assert 'href="/portfolio"' in html  # the sample is being paper-tailed
    assert "<svg" in html  # charts rendered
    assert client.get("/watch/sample-2").status_code == 404  # list-only row
    assert client.get("/watch/sample-99").status_code == 404


def test_nav_shows_watchlist_to_visitors_in_demo_mode(client, demo_on):
    assert 'href="/watch"' in client.get("/marketplace").text


def test_nav_hides_watchlist_from_visitors_otherwise(client):
    assert 'href="/watch"' not in client.get("/marketplace").text


def test_sample_routes_404_when_demo_is_off(client):
    assert client.get("/watch/sample-1").status_code == 404
    resp = client.get("/watch", follow_redirects=False)  # unchanged: login redirect
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")
    assert client.get("/watch/1").status_code == 404


def test_sample_page_is_not_shown_to_a_logged_in_user(client, demo_on):
    signup(client, "a@example.com")
    assert client.get("/watch/sample-1").status_code == 404


def test_adding_is_refused_in_demo_mode_without_a_venue_call(client, demo_on):
    signup(client, "a@example.com")
    with respx.mock(assert_all_called=False) as router:
        route = router.post(INFO_URL).mock(return_value=httpx.Response(200, json=[]))
        form = client.post(
            "/watch",
            data={"venue": "hyperliquid", "address": ADDRESS, "label": "x"},
            follow_redirects=False,
        )
        api = client.post("/api/watch", json={"venue": "hyperliquid", "address": ADDRESS})
        assert route.call_count == 0
    assert form.status_code == 403 and "Adding addresses is off in the demo." in form.text
    assert api.status_code == 403
    assert api.json()["detail"] == "Adding addresses is off in the demo."


def _seeded_watch(db, user: User) -> int:
    """A real Watch row for `user`, built directly, owned by an is_demo system user."""
    system = User(
        email="system@koine.invalid",
        password_hash="!",
        display_name="Koine",
        is_system=True,
        is_demo=True,
    )
    db.add(system)
    db.flush()
    now = datetime.now(UTC)
    account = VenueAccount(
        user_id=system.id,
        venue="hyperliquid",
        venue_env="prod",
        key_id=ADDRESS,
        anchor_balance_micros=0,
        anchor_at=now - timedelta(days=30),
        is_watch=True,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=system.id,
        venue_account_id=account.id,
        name="Hyperliquid 0x1a13…caec",
        slug=f"watch-{account.id}",
        kind=StrategyKind.WATCH,
        public=True,
        status=StrategyStatus.LIVE,
    )
    db.add(strategy)
    db.flush()
    db.add(Watch(user_id=user.id, strategy_id=strategy.id, label="Seeded favourite"))
    db.commit()
    return strategy.id


def test_seeded_demo_investor_sees_the_strip_on_real_rows(client, session_factory, demo_on):
    signup(client, "a@example.com")
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        user.is_demo = True
        sid = _seeded_watch(db, user)
        db.commit()
    for path in ("/watch", f"/watch/{sid}", f"/watch/{sid}/tail"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert BANNER in resp.text and "Seeded favourite" in resp.text, path
        assert path == "/watch" or "Only you can see this page." in resp.text, path


def test_a_real_investor_has_no_strip(client, session_factory):
    signup(client, "a@example.com")
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == "a@example.com"))
        sid = _seeded_watch(db, user)
    html = client.get(f"/watch/{sid}").text
    assert BANNER not in html and "Only you can see this page." in html
