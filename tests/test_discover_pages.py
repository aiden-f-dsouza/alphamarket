"""/watch/discover and /api/watch/discover (copy strategies step 7, pages half):
the snapshot view rendered for a logged-in user, the add-from-discover return
trip, the static demo sample. Snapshots are inserted directly; no venue call."""

import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import respx
from sqlalchemy import select

from alphamarket import demo, discover
from alphamarket.config import get_settings
from alphamarket.connectors.base import LeaderboardEntry
from alphamarket.connectors.hyperliquid import INFO_URL
from alphamarket.models import Watch, WatchOptout
from test_watchlist import FakeInfo, add, signup


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "watch_discover_enabled", True)


@pytest.fixture()
def info():
    with respx.mock(assert_all_called=False) as router:
        router.post(INFO_URL).mock(side_effect=FakeInfo())
        yield


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


def snap(session_factory, period="day", n=4, venue="hyperliquid"):
    """n rows: rank i+1, pnl falling with rank, roi rising with rank."""
    rows = [
        discover.snapshot_row(
            LeaderboardEntry(
                address=addr(i + 1),
                rank=i + 1,
                pnl=str(10_000 - i * 1_000),
                roi=f"0.{i + 1}0",
                volume="500000",
                account_value=None if i == 0 else "250000.5",
            )
        )
        for i in range(n)
    ]
    with session_factory() as db:
        discover.store_snapshot(db, venue, period, rows, datetime.now(UTC) - timedelta(minutes=5))
        db.commit()


def order(html: str) -> list[str]:
    return re.findall(r'title="(0x[0-9a-f]{40})"', html)


def test_404_when_disabled(client):
    signup(client, "a@example.com")
    assert client.get("/watch/discover").status_code == 404
    assert client.get("/api/watch/discover").status_code == 404
    assert 'href="/watch/discover"' not in client.get("/watch").text


def test_login_redirect(client, on):
    resp = client.get("/watch/discover", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/login")


def test_headers_and_rows_in_order(client, session_factory, on):
    snap(session_factory)
    signup(client, "a@example.com")
    resp = client.get("/watch/discover")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "private, no-store"
    assert 'name="robots" content="noindex' in resp.text
    html = resp.text
    assert order(html) == [addr(1), addr(2), addr(3), addr(4)]
    assert re.search(r"0 of \d+ on your watchlist", html)
    assert "not Koine-verified" in html and "Updated 5 minutes ago" in html
    assert 'href="/watch/optout"' in html and 'href="#"' not in html
    assert "$10,000" in html and "+10.0%" in html
    assert 'aria-label="Venue"' not in html  # one venue: no venue tabs
    for label in ("Day", "Week", "Month", "All time"):
        assert f">{label}</a>" in html
    assert 'name="return_to" value="discover"' in html
    assert "/watch/discover" in client.get("/watch").text


def test_sort_by_roi_and_bad_params(client, session_factory, on):
    snap(session_factory)
    signup(client, "a@example.com")
    html = client.get("/watch/discover?sort=roi").text
    assert order(html) == [addr(4), addr(3), addr(2), addr(1)]
    resp = client.get("/watch/discover?venue=zz&period=zz&sort=zz")
    assert resp.status_code == 200 and order(resp.text) == [addr(1), addr(2), addr(3), addr(4)]
    # A period with no snapshot yet: empty state, never an error.
    empty = client.get("/watch/discover?period=week")
    assert "first leaderboard refresh hasn't run yet" in empty.text


def test_opted_out_address_not_shown(client, session_factory, on):
    snap(session_factory)
    signup(client, "a@example.com")
    with session_factory() as db:
        db.add(WatchOptout(venue="hyperliquid", address=addr(2), proved_by_user_id=1))
        db.commit()
    assert order(client.get("/watch/discover").text) == [addr(1), addr(3), addr(4)]


def test_add_from_discover_returns_with_params(client, session_factory, on, info):
    snap(session_factory, "week")
    signup(client, "a@example.com")
    resp = client.post(
        "/watch",
        data={
            "venue": "hyperliquid",
            "address": addr(2),
            "label": "",
            "return_to": "discover",
            "d_venue": "hyperliquid",
            "d_period": "week",
            "d_sort": "roi",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == (
        "/watch/discover?venue=hyperliquid&period=week&sort=roi&notice=added"
    )
    page = client.get(resp.headers["location"])
    assert "Added to your watchlist" in page.text
    assert "1 of" in page.text and "On your watchlist" in page.text
    with session_factory() as db:
        assert len(db.scalars(select(Watch)).all()) == 1


def test_return_to_cannot_redirect_elsewhere(client, on, info):
    signup(client, "a@example.com")
    resp = client.post(
        "/watch",
        data={
            "venue": "hyperliquid",
            "address": addr(2),
            "return_to": "https://evil.example/",
            "d_period": "//evil",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/watch/")
    # Hostile params with the right return_to are re-validated, never echoed.
    resp = client.post(
        "/watch",
        data={
            "venue": "hyperliquid",
            "address": addr(3),
            "return_to": "discover",
            "d_venue": "//evil",
            "d_period": '"><x>',
            "d_sort": "zz",
        },
        follow_redirects=False,
    )
    assert resp.headers["location"] == (
        "/watch/discover?venue=hyperliquid&period=day&sort=pnl&notice=added"
    )


def test_errors_come_back_to_discover(client, session_factory, on, info, monkeypatch):
    snap(session_factory)
    signup(client, "a@example.com")
    monkeypatch.setattr(get_settings(), "watch_max_per_user", 0)
    html = client.get("/watch/discover").text
    assert "Watchlist full" in html and 'name="address"' not in html
    # A forced post past the full state: the cap error renders on discover.
    resp = client.post(
        "/watch", data={"venue": "hyperliquid", "address": addr(1), "return_to": "discover"}
    )
    assert resp.status_code >= 400
    assert "Discover addresses" in resp.text and 'role="alert"' in resp.text
    monkeypatch.setattr(get_settings(), "watch_max_per_user", 10)
    with session_factory() as db:
        db.add(WatchOptout(venue="hyperliquid", address=addr(1), proved_by_user_id=1))
        db.commit()
    resp = client.post(
        "/watch", data={"venue": "hyperliquid", "address": addr(1), "return_to": "discover"}
    )
    assert resp.status_code >= 400
    assert "Discover addresses" in resp.text and "asked not to be tracked" in resp.text


def test_other_return_to_behaves_as_before(client, on, info):
    signup(client, "a@example.com")
    bad = client.post("/watch", data={"venue": "hyperliquid", "address": "nope", "return_to": "x"})
    assert bad.status_code == 422 and "Watch a public trader" in bad.text


def test_listed_row_links_to_the_strategy(client, session_factory, on, monkeypatch):
    snap(session_factory)
    signup(client, "a@example.com")
    real = discover.discover_view

    def fake(*a, **kw):
        view = real(*a, **kw)
        rows = [replace(view.rows[0], state="listed", listed_slug="some-slug"), *view.rows[1:]]
        return replace(view, rows=rows)

    monkeypatch.setattr(discover, "discover_view", fake)
    html = client.get("/watch/discover").text
    assert 'href="/s/some-slug"' in html and "Verified on Koine" in html


def test_json_api(client, session_factory, on, info):
    snap(session_factory)
    assert client.get("/api/watch/discover").status_code == 401
    signup(client, "a@example.com")
    body = client.get("/api/watch/discover?sort=roi").json()
    assert body["venue"] == "hyperliquid" and body["period"] == "day" and body["sort"] == "roi"
    assert body["venues"] == ["hyperliquid"] and body["periods"] == ["day", "week", "month", "all"]
    assert body["watch_count"] == 0 and body["watch_max"] == get_settings().watch_max_per_user
    assert datetime.fromisoformat(body["fetched_at"]).tzinfo is not None
    first = body["rows"][0]
    assert first == {
        "address": addr(4),
        "rank": 4,
        "pnl_micros": 7_000_000_000,
        "roi": 0.4,
        "volume_micros": 500_000_000_000,
        "account_value_micros": 250_000_500_000,
        "state": "add",
    }
    assert body["rows"][-1]["account_value_micros"] is None
    add(client, addr(4))
    row = client.get("/api/watch/discover?sort=roi").json()["rows"][0]
    assert row["state"] == "watching" and isinstance(row["watch_strategy_id"], int)


def test_demo_sample_renders_with_banner_and_disabled_add(client, demo_on):
    resp = client.get("/watch/discover")
    assert resp.status_code == 200
    html = resp.text
    assert "Sample data" in html and "Discover addresses" in html
    # a list of addresses, not one watched address: no single-address strip
    assert "not a Koine member" not in html
    assert 'name="robots" content="noindex' in html
    assert resp.headers["cache-control"] == "private, no-store"
    assert 'action="/watch"' not in html and 'href="#"' not in html
    assert "Adding addresses is off in the demo" in html
    assert 'href="/watch/sample-1"' in html and "On your watchlist" in html
    assert 'href="/s/funding-aware-btc-perps"' in html
    assert len(order(html)) == 15
    pm = client.get("/watch/discover?venue=polymarket&period=week&sort=roi")
    assert pm.status_code == 200 and len(order(pm.text)) == 15
    assert 'aria-label="Venue"' in pm.text


def test_demo_sample_is_synthetic_and_sorted():
    for venue in ("hyperliquid", "polymarket"):
        for period in ("day", "week", "month", "all"):
            view = demo.discover_sample(venue, period, "pnl")
            assert len(view.rows) == 15 and view.fetched_at is not None
            pnl = [r.pnl_micros for r in view.rows]
            assert pnl == sorted(pnl, reverse=True)
            assert all(re.fullmatch(r"0x[0-9a-f]{40}", r.address) for r in view.rows)
    bad = demo.discover_sample("x", "y", "z")
    assert (bad.venue, bad.period, bad.sort) == ("hyperliquid", "day", "pnl")


def test_demo_off_and_not_enabled_is_404(client):
    assert client.get("/watch/discover").status_code == 404
