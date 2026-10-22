"""Per-venue /watch/discover columns and Polymarket's wallet-stats enrichment
(copy strategies step 7 follow-up). Venue traffic is respx-mocked from small
recorded fixtures (invariant 6); expected values are worked out by hand."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import discover
from alphamarket.config import get_settings
from alphamarket.connectors.base import LeaderboardDetails, LeaderboardEntry, LeaderboardPeriod
from alphamarket.connectors.polymarket import PolymarketMarketData, volume_window
from alphamarket.connectors.registry import get_profile
from alphamarket.models import LeaderboardSnapshot
from test_watchlist import signup

PM = Path(__file__).parent / "fixtures" / "polymarket"
BOARD = json.loads((PM / "leaderboard_week_20261014.json").read_text())
VOLUME = json.loads((PM / "user_volume_20261022.json").read_text())
VALUE = json.loads((PM / "value_20261022.json").read_text())
STATS = json.loads((PM / "user_stats_20261022.json").read_text())
BASE = "https://data-api.polymarket.com/v2"
NOW = datetime(2026, 10, 14, 18, 0, tzinfo=UTC)
DAY, WEEK, MONTH, ALL = (
    LeaderboardPeriod.DAY,
    LeaderboardPeriod.WEEK,
    LeaderboardPeriod.MONTH,
    LeaderboardPeriod.ALL,
)


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("alphamarket.connectors.polymarket.time.sleep", lambda _s: None)


@pytest.fixture()
def settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "watch_discover_enabled", True)
    monkeypatch.setattr(s, "watch_venues", "hyperliquid,polymarket")
    monkeypatch.setattr(s, "watch_discover_rows", 2)
    return s


def md() -> PolymarketMarketData:
    return PolymarketMarketData(clob_url="https://clob.invalid")


def mock_wallet_routes(router, volume=None, value=None, stats=None):
    return SimpleNamespace(
        volume=router.get(f"{BASE}/user-volume").mock(
            return_value=volume or httpx.Response(200, json=VOLUME)
        ),
        value=router.get(f"{BASE}/value").mock(
            return_value=value or httpx.Response(200, json=VALUE)
        ),
        stats=router.get(f"{BASE}/user-stats").mock(
            return_value=stats or httpx.Response(200, json=STATS)
        ),
    )


# ---- profile columns ----------------------------------------------------------------


def test_profile_columns():
    assert get_profile("hyperliquid").leaderboard_columns == (
        "pnl",
        "roi",
        "volume",
        "account_value",
    )
    assert get_profile("polymarket").leaderboard_columns == (
        "pnl",
        "volume",
        "positions_value",
        "markets_traded",
        "biggest_win",
        "active_since",
    )
    assert get_profile("hyperliquid").leaderboard_note == ""
    assert "no ROI or cash balance" in get_profile("polymarket").leaderboard_note


# ---- window arithmetic --------------------------------------------------------------


def test_volume_window_is_whole_utc_days_ending_today():
    # now = 2026-10-14 18:00 UTC = 1792000800; today 00:00 UTC = 1791936000.
    assert volume_window(DAY, NOW) == {"start": 1791936000, "end": 1792000800}
    # week: 7 UTC days incl. today -> start Oct 8 00:00 = 1791936000 - 6*86400
    assert volume_window(WEEK, NOW) == {"start": 1791417600, "end": 1792000800}
    # month: 30 UTC days incl. today -> start Sep 15 00:00 = 1791936000 - 29*86400
    assert volume_window(MONTH, NOW) == {"start": 1789430400, "end": 1792000800}
    assert volume_window(ALL, NOW) == {}  # unbounded
    assert 1791936000 - 6 * 86400 == 1791417600 and 1791936000 - 29 * 86400 == 1789430400


# ---- the connector ------------------------------------------------------------------


@respx.mock
def test_details_parse_fixtures_and_send_the_window():
    r = mock_wallet_routes(respx)
    out = md().leaderboard_details(WEEK, [addr(1)], NOW)
    assert out == {
        addr(1): LeaderboardDetails(
            volume="64674.317178",  # volume_usdc, not the shares `volume`
            positions_value="144.175200",
            markets_traded=411,
            biggest_win="1557137.625996",
            active_since=1740064569,
        )
    }
    assert dict(r.volume.calls.last.request.url.params) == {
        "user": addr(1),
        "start": "1791417600",
        "end": "1792000800",
    }
    assert dict(r.value.calls.last.request.url.params) == {"user": addr(1)}
    assert dict(r.stats.calls.last.request.url.params) == {"user": addr(1)}
    assert "views" not in LeaderboardDetails.model_fields


@respx.mock
def test_details_all_period_sends_no_bounds():
    r = mock_wallet_routes(respx)
    md().leaderboard_details(ALL, [addr(1)], NOW)
    assert dict(r.volume.calls.last.request.url.params) == {"user": addr(1)}


@respx.mock
def test_value_and_stats_fetched_once_per_address_across_periods():
    r = mock_wallet_routes(respx)
    source = md()
    for period in (DAY, WEEK, MONTH, ALL):
        out = source.leaderboard_details(period, [addr(1), addr(2)], NOW)
        assert set(out) == {addr(1), addr(2)}
        assert out[addr(2)].positions_value == "144.175200"
    assert r.volume.call_count == 8  # per period and address
    assert r.value.call_count == 2 and r.stats.call_count == 2  # per address only


@respx.mock
def test_unknown_user_leaves_stats_none():
    mock_wallet_routes(respx, stats=httpx.Response(200, json={"data": None}))
    d = md().leaderboard_details(WEEK, [addr(1)], NOW)[addr(1)]
    assert (d.markets_traded, d.biggest_win, d.active_since) == (None, None, None)
    assert d.volume == "64674.317178"


@respx.mock
def test_null_join_date_and_failed_calls_are_none():
    stats = {"data": {**STATS["data"], "join_date": None}}
    mock_wallet_routes(
        respx,
        volume=httpx.Response(500),
        value=httpx.Response(400, json={"error": "bad"}),
        stats=httpx.Response(200, json=stats),
    )
    d = md().leaderboard_details(WEEK, [addr(1)], NOW)[addr(1)]
    assert d.volume is None and d.positions_value is None
    assert d.active_since is None and d.markets_traded == 411


@respx.mock
def test_429_stops_enrichment_and_is_reported():
    r = mock_wallet_routes(respx, volume=httpx.Response(429, headers={"Retry-After": "7"}, json={}))
    source = md()
    assert source.leaderboard_details(WEEK, [addr(1), addr(2)], NOW) == {}
    assert source.enrichment_rate_limited is not None
    assert source.enrichment_rate_limited.retry_after_seconds == 7
    assert r.volume.call_count == 4  # _PublicHttp's bounded retries, then stop
    assert source.leaderboard_details(DAY, [addr(1)], NOW) == {}  # stays stopped
    assert r.volume.call_count == 4


# ---- refresh ------------------------------------------------------------------------


def _ctx(db):
    return SimpleNamespace(db=db, limiter=None, clock=lambda: NOW, settings=get_settings())


def _hl_quiet(settings, monkeypatch):
    monkeypatch.setattr(settings, "watch_venues", "polymarket")


@respx.mock
def test_refresh_enriches_only_kept_rows_and_stores_micros(session_factory, settings, monkeypatch):
    _hl_quiet(settings, monkeypatch)
    respx.get(f"{BASE}/leaderboard").mock(return_value=httpx.Response(200, json=BOARD))
    r = mock_wallet_routes(respx)
    with session_factory() as db:
        discover.refresh(_ctx(db))
        snaps = {s.period: s for s in db.scalars(select(LeaderboardSnapshot))}
    # rows=2 kept per period (no roi): addresses 1 and 2. Per refresh: 4 periods x 2
    # volume calls, and value + stats once for each of the 2 addresses.
    assert r.volume.call_count == 8 and r.value.call_count == 2 and r.stats.call_count == 2
    row = snaps["week"].rows[0]
    assert row["address"] == addr(1)
    assert row["pnl_micros"] == 3_054_314_534_295
    assert row["volume_micros"] == 64_674_317_178
    assert row["positions_value_micros"] == 144_175_200
    assert row["markets_traded"] == 411
    assert row["biggest_win_micros"] == 1_557_137_625_996
    assert row["active_since"] == 1740064569
    assert row["roi"] is None and row["account_value_micros"] is None
    blob = json.dumps([s.rows for s in snaps.values()])
    for marker in ("placeholder", "example.invalid", "verified", "views", "user_name"):
        assert marker not in blob
    assert set(row) == {
        "address",
        "rank",
        "pnl_micros",
        "roi",
        "volume_micros",
        "account_value_micros",
        "positions_value_micros",
        "markets_traded",
        "biggest_win_micros",
        "active_since",
    }


@respx.mock
def test_enrichment_failure_keeps_the_board(session_factory, settings, monkeypatch):
    _hl_quiet(settings, monkeypatch)
    respx.get(f"{BASE}/leaderboard").mock(return_value=httpx.Response(200, json=BOARD))
    mock_wallet_routes(
        respx,
        volume=httpx.Response(429, headers={"Retry-After": "3"}, json={}),
        value=httpx.Response(500),
        stats=httpx.Response(500),
    )
    with session_factory() as db:
        counts = discover.refresh(_ctx(db))
        snaps = {s.period: s.rows for s in db.scalars(select(LeaderboardSnapshot))}
    assert counts == {("polymarket", p): 2 for p in ("day", "week", "month", "all")}
    row = snaps["week"][0]
    assert row["pnl_micros"] == 3_054_314_534_295
    assert row.get("volume_micros") is None and row.get("positions_value_micros") is None


@respx.mock
def test_enrichment_exception_keeps_the_board(session_factory, settings, monkeypatch):
    _hl_quiet(settings, monkeypatch)
    respx.get(f"{BASE}/leaderboard").mock(return_value=httpx.Response(200, json=BOARD))

    def boom(self, period, addresses, now=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(PolymarketMarketData, "leaderboard_details", boom)
    with session_factory() as db:
        counts = discover.refresh(_ctx(db))
        assert counts[("polymarket", "week")] == 2
        assert db.scalar(select(LeaderboardSnapshot)).rows[0]["address"] == addr(1)


# ---- the page and the JSON API ------------------------------------------------------


def _pm_snapshot(session_factory, with_details=True):
    rows = []
    for i in range(3):
        row = discover.snapshot_row(
            LeaderboardEntry(address=addr(i + 1), rank=i + 1, pnl=str(9_000 - i * 1_000))
        )
        if with_details:
            discover.merge_details(
                row,
                LeaderboardDetails(
                    volume="250000.5",
                    positions_value="1234.5",
                    markets_traded=1234,
                    biggest_win="55000",
                    active_since=1740064569,
                ),
            )
        rows.append(row)
    with session_factory() as db:
        discover.store_snapshot(db, "polymarket", "week", rows, NOW)
        db.commit()


def _hl_snapshot(session_factory):
    rows = [
        discover.snapshot_row(
            LeaderboardEntry(
                address=addr(10 + i),
                rank=i + 1,
                pnl=str(5_000 - i),
                roi=f"0.{i + 1}0",
                volume="500000",
                account_value="250000",
            )
        )
        for i in range(2)
    ]
    with session_factory() as db:
        discover.store_snapshot(db, "hyperliquid", "week", rows, NOW)
        db.commit()


def test_polymarket_page_shows_only_its_columns(client, session_factory, settings):
    _pm_snapshot(session_factory)
    signup(client, "a@example.com")
    html = client.get("/watch/discover?venue=polymarket&period=week").text
    for label in ("PnL", "Volume (UTC days)", "Positions value", "Markets", "Biggest win", "Since"):
        assert f'class="label r{"" if label == "PnL" else " hide"}">{label}</span>' in html
    assert ">ROI</span>" not in html and "Account value" not in html
    assert ">ROI</a>" not in html  # no roi sort toggle
    assert ">PnL</a>" in html
    assert "$250,000" in html and "$1,234" in html and "1,234" in html and "$55,000" in html
    assert "Feb 2025" in html
    assert "Polymarket publishes no ROI or cash balance" in html
    assert "not Koine-verified" in html
    # ?sort=roi on a venue without roi falls back to pnl
    resp = client.get("/watch/discover?venue=polymarket&period=week&sort=roi")
    assert resp.status_code == 200 and "sort=pnl" in resp.text
    assert 'class="on">PnL</a>' in resp.text


def test_hyperliquid_page_is_unchanged(client, session_factory, settings):
    _hl_snapshot(session_factory)
    signup(client, "a@example.com")
    html = client.get("/watch/discover?venue=hyperliquid&period=week&sort=roi").text
    for label in ("PnL", "ROI", "Volume", "Account value"):
        assert f">{label}</span>" in html
    assert ">ROI</a>" in html and ">PnL</a>" in html
    assert "Positions value" not in html and "Biggest win" not in html
    assert 'class="on">ROI</a>' in html
    assert "Polymarket" not in html.split('aria-label="Period"')[1].split("</nav>")[1]


def test_rows_from_before_the_new_keys_still_render(client, session_factory, settings):
    _pm_snapshot(session_factory, with_details=False)
    signup(client, "a@example.com")
    resp = client.get("/watch/discover?venue=polymarket&period=week")
    assert resp.status_code == 200 and "$9,000" in resp.text
    api = client.get("/api/watch/discover?venue=polymarket&period=week").json()
    assert api["rows"][0]["positions_value_micros"] is None
    assert api["rows"][0]["markets_traded"] is None


def test_json_api_shape_per_venue(client, session_factory, settings):
    _pm_snapshot(session_factory)
    _hl_snapshot(session_factory)
    signup(client, "a@example.com")
    pm = client.get("/api/watch/discover?venue=polymarket&period=week&sort=roi").json()
    assert pm["columns"] == [
        "pnl",
        "volume",
        "positions_value",
        "markets_traded",
        "biggest_win",
        "active_since",
    ]
    assert pm["sort"] == "pnl"
    assert pm["rows"][0] == {
        "address": addr(1),
        "rank": 1,
        "pnl_micros": 9_000_000_000,
        "volume_micros": 250_000_500_000,
        "positions_value_micros": 1_234_500_000,
        "markets_traded": 1234,
        "biggest_win_micros": 55_000_000_000,
        "active_since": 1740064569,
        "state": "add",
    }
    hl = client.get("/api/watch/discover?venue=hyperliquid&period=week").json()
    assert hl["columns"] == ["pnl", "roi", "volume", "account_value"]
    assert hl["rows"][0] == {
        "address": addr(10),
        "rank": 1,
        "pnl_micros": 5_000_000_000,
        "roi": 0.1,
        "volume_micros": 500_000_000_000,
        "account_value_micros": 250_000_000_000,
        "state": "add",
    }


def test_demo_sample_polymarket_has_the_new_columns(client, settings):
    from alphamarket import demo

    view = demo.discover_sample("polymarket", "week", "roi")
    assert view.sort == "pnl" and "roi" not in view.columns and "ROI" not in view.label("pnl")
    row = view.rows[0]
    assert row.roi is None and row.account_value_micros is None
    assert None not in (
        row.volume_micros,
        row.positions_value_micros,
        row.markets_traded,
        row.biggest_win_micros,
        row.active_since,
    )
    hl = demo.discover_sample("hyperliquid", "week", "roi")
    assert hl.sort == "roi" and hl.rows[0].roi is not None
