"""Lighter public market data (instruments, quotes, daily closes, funding,
margin tiers) and the pnlLeaderboard, against captures of 2026-10-22
(tests/fixtures/lighter/README.md). No live calls (invariant 6)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from alphamarket.connectors import lighter
from alphamarket.connectors.base import InstrumentType, LeaderboardPeriod
from alphamarket.connectors.lighter import LighterMarketData, copyable

FIXTURES = Path(__file__).parent / "fixtures" / "lighter"
DAY = "2026-10-22"


def fx_text(name: str) -> str:
    return (FIXTURES / f"{name}_{DAY}.json").read_text()


class FakeApi:
    """Answers the API host by path; pnlLeaderboard by (time_window, sort_by,
    offset) from `boards`; records every request's params."""

    def __init__(self, boards: dict | None = None) -> None:
        self.boards = boards or {}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        endpoint = url.path.rsplit("/", 1)[-1]
        self.calls.append((endpoint, params))
        if endpoint == "orderBookDetails":
            return httpx.Response(200, text=fx_text("orderBookDetails"))
        if endpoint == "orderBookOrders":
            assert params == {"market_id": "1", "limit": "1"}
            return httpx.Response(200, text=fx_text("orderBookOrders_btc"))
        if endpoint == "candles":
            return httpx.Response(200, text=fx_text("candles_btc_1d"))
        if endpoint == "fundings":
            rows = json.loads(fx_text(f"fundings_{params['market_id']}"))["fundings"]
            end, back = int(params["end_timestamp"]), int(params["count_back"])
            served = [r for r in rows if int(r["timestamp"]) <= end][-back:]
            return httpx.Response(200, json={"code": 200, "fundings": served})
        if endpoint == "pnlLeaderboard":
            key = (params["time_window"], params["sort_by"], int(params["offset"]))
            rows = self.boards.get(key, [])
            return httpx.Response(200, json={"code": 200, "total": 99496, "entries": rows})
        raise AssertionError(f"unexpected {endpoint} {params}")


def board_rows(name: str) -> list[dict]:
    return json.loads(fx_text(f"pnlLeaderboard_{name}"))["entries"]


def source(**kw) -> LighterMarketData:
    return LighterMarketData(client=httpx.Client(), **kw)


# ---- instruments --------------------------------------------------------------------------


@respx.mock
def test_instruments_carry_decimals_minimums_and_copyable():
    """orderBookDetails: BTC perp size 5 dp / price 1 dp, min 0.00007 BTC
    (7 atoms) and $10; LIT/USDC spot 2 dp. AAPL, XAU, EURUSD, ANTHROPIC and
    rhSPY/USDC are not copyable (counsel Q3); crypto is."""
    respx.route().mock(side_effect=FakeApi())
    listed = {i.symbol: i for i in source().fetch_instruments()}
    btc = listed["BTC"]
    assert (btc.type, btc.base_asset, btc.quote_asset) == (InstrumentType.PERP, "BTC-PERP", "USDC")
    assert (btc.qty_decimals, btc.qty_step_atoms, btc.min_qty_atoms) == (5, 1, 7)
    assert btc.min_notional == "10.000000" and btc.copyable
    spot = listed["LIT/USDC"]
    assert (spot.type, spot.base_asset, spot.quote_asset, spot.qty_decimals) == (
        InstrumentType.SPOT,
        "LIT",
        "USDC",
        2,
    )
    for symbol in ("AAPL", "XAU", "EURUSD", "ANTHROPIC", "RKLB", "rhSPY/USDC"):
        assert listed[symbol].copyable is False, symbol
    for symbol in ("ETH", "SOL", "LIT", "HYPE", "ETH/USDC"):
        assert listed[symbol].copyable is True, symbol
    assert "BIRB" in listed  # inactive markets stay listed: old fills reference them


def test_copyable_reads_the_base_of_a_spot_pair():
    assert copyable("BTC") and copyable("LIT/USDC")
    assert not copyable("SPY") and not copyable("rhQQQ/USDC") and not copyable("XAUT/USDC")


@respx.mock
def test_pricing_symbol_finds_the_spot_pair_and_leaves_usdc_at_par():
    respx.route().mock(side_effect=FakeApi())
    data = source()
    listed = data.fetch_instruments()
    assert data.pricing_symbol("LIT", listed) == "LIT/USDC"
    assert data.pricing_symbol("USDC", listed) is None


# ---- quotes, closes, funding, tiers ---------------------------------------------------------


@respx.mock
def test_quote_is_the_top_of_the_order_book():
    """orderBookOrders_btc: best ask 81376.3, best bid 81374.0."""
    respx.route().mock(side_effect=FakeApi())
    quote = source().fetch_quote("BTC")
    assert (quote.bid, quote.ask) == ("81374.0", "81376.3")
    assert source().fetch_quote("NOPE") is None


@respx.mock
def test_daily_closes_drop_the_open_candle(monkeypatch):
    """candles_btc_1d: 2026-10-17..21 closed plus 10-22 still open when read;
    closes are the JSON floats' exact text (81460.9 on 10-17, 84058.5 on 10-21)."""
    monkeypatch.setattr(lighter, "_now", lambda: datetime(2026, 10, 22, 18, tzinfo=UTC))
    api = FakeApi()
    respx.route().mock(side_effect=api)
    closes = source().fetch_daily_closes("BTC", date(2026, 10, 17), date(2026, 10, 22))
    assert [c.day for c in closes] == [date(2026, 10, d) for d in range(17, 22)]
    assert closes[-1].close == "84058.5" and closes[0].close == "81460.9"
    (_, params) = next(c for c in api.calls if c[0] == "candles")
    assert params["resolution"] == "1d" and params["market_id"] == "1"


@respx.mock
def test_funding_history_is_the_hourly_percent_over_100_signed_by_direction():
    """fundings_1 at 2026-10-21 17:00 UTC (1792602000): rate "0.0010" percent,
    direction long -> 0.00001; at 20:00 (1792612800): "0.0007" short ->
    -0.000007. Shorts receive rate (symmetric; short_rate None)."""
    respx.route().mock(side_effect=FakeApi())
    start = datetime(2026, 10, 21, 17, tzinfo=UTC)
    end = datetime(2026, 10, 21, 20, tzinfo=UTC)
    rates = source().fetch_funding_history("BTC", start, end)
    assert [r.at for r in rates] == [start.replace(hour=h) for h in (17, 18, 19, 20)]
    assert rates[0].rate == "0.00001" and rates[3].rate == "-0.000007"
    assert all(r.short_rate is None for r in rates)
    assert source().fetch_funding_history("LIT/USDC", start, end) == []


@respx.mock
def test_margin_tier_keeps_koines_maintenance_at_or_above_lighters():
    """BTC maintenance_margin_fraction 120 (1.20%): 5000 // 120 = 41x, and
    Koine's 1 / (2 x 41) = 1.22%. LIT 1200 (12%) -> 4x (12.5%)."""
    respx.route().mock(side_effect=FakeApi())
    data = source()
    (btc,) = data.fetch_margin_tiers("BTC")
    assert (btc.lower_bound, btc.max_leverage) == ("0", 41)
    assert data.fetch_margin_tiers("LIT")[0].max_leverage == 4
    assert data.fetch_margin_tiers("LIT/USDC") == []


# ---- leaderboard ------------------------------------------------------------------------------


def boards() -> dict:
    return {
        ("24h", "pnl", 0): board_rows("24h_pnl"),
        ("7d", "pnl", 0): [],
        ("30d", "pnl", 0): board_rows("30d_pnl"),
        ("all", "pnl", 0): board_rows("all_pnl"),
        ("7d", "roi", 0): board_rows("7d_roi"),
        ("30d", "roi", 0): board_rows("7d_roi"),
    }


@respx.mock
def test_leaderboard_covers_all_four_windows_with_lighters_own_figures():
    """30d rank 1: pnl 4,827,967.783095, roi 14.92912091263724 percent ->
    0.1492912091263724, volume 71,039,000.791988, account value
    24,165,041.833642; the EIP-55 address is lowercased. Streak, net
    position, biggest position and long/short shares are dropped."""
    api = FakeApi(boards())
    respx.route().mock(side_effect=api)
    out = source().fetch_leaderboards(tuple(LeaderboardPeriod))
    assert set(out) == set(LeaderboardPeriod)
    top = out[LeaderboardPeriod.MONTH][0]
    assert top.model_dump() == {
        "address": "0x0000000000000000000000000000000000001001",
        "rank": 1,
        "pnl": "4827967.783095",
        "roi": "0.1492912091263724",
        "volume": "71039000.791988",
        "account_value": "24165041.833642",
    }
    assert [e.rank for e in out[LeaderboardPeriod.DAY]] == list(range(1, 13))
    assert out[LeaderboardPeriod.ALL][0].roi == "7.291425131519196"
    # Each window: one pnl page (short, so paging stops) and one roi page.
    windows = [(p["time_window"], p["sort_by"]) for e, p in api.calls if e == "pnlLeaderboard"]
    assert sorted(windows) == sorted(
        (w, s) for w in ("24h", "7d", "30d", "all") for s in ("pnl", "roi")
    )
    assert all(e.address == e.address.lower() for v in out.values() for e in v)


@respx.mock
def test_roi_only_rows_rank_after_the_fetched_pnl_rows():
    """30d: 12 rows by pnl, then the 7d-roi capture served as its roi page:
    those 12 rows are new addresses, ranked 12 + their roi position."""
    respx.route().mock(side_effect=FakeApi(boards()))
    month = source().fetch_leaderboards((LeaderboardPeriod.MONTH,))[LeaderboardPeriod.MONTH]
    assert [e.rank for e in month] == list(range(1, 25))
    assert month[12].address == "0x0000000000000000000000000000000000001025"
    assert month[12].roi == "24.91496152721754"  # 2,491.496...% from the roi board


@respx.mock
def test_leaderboard_floor_and_early_stop():
    """Floor $10,000: rows under it are dropped (the roi capture's small
    accounts: 5,035, 579, 214 ...). With leaderboard_keep=3 the pnl paging
    stops after the first page even though it is full (100 rows)."""
    rows = board_rows("30d_pnl") * 9
    full = [dict(r, rank=i + 1, l1_address=f"0x{i + 1:040x}") for i, r in enumerate(rows)][:100]
    api = FakeApi({("30d", "pnl", 0): full, ("30d", "roi", 0): board_rows("7d_roi")})
    respx.route().mock(side_effect=api)
    data = source(leaderboard_keep=3, leaderboard_min_account_value="10000")
    month = data.fetch_leaderboards((LeaderboardPeriod.MONTH,))[LeaderboardPeriod.MONTH]
    above = [r for r in full if Decimal(str(r["account_value"])) >= 10_000]
    assert len(month) == len(above) < 100
    assert all(Decimal(e.account_value) >= 10_000 for e in month)
    offsets = [(p["sort_by"], p["offset"]) for e, p in api.calls if e == "pnlLeaderboard"]
    assert offsets == [("pnl", "0"), ("roi", "0")]


@respx.mock
def test_malformed_board_rows_are_skipped():
    rows = board_rows("24h_pnl")[:3]
    rows[1] = {**rows[1], "l1_address": "not-an-address"}
    rows[2] = {k: v for k, v in rows[2].items() if k != "pnl"}
    respx.route().mock(side_effect=FakeApi({("24h", "pnl", 0): rows}))
    day = source().fetch_leaderboards((LeaderboardPeriod.DAY,))[LeaderboardPeriod.DAY]
    assert [e.rank for e in day] == [1]


@respx.mock
def test_no_requested_period_means_no_request():
    api = FakeApi()
    respx.route().mock(side_effect=api)
    assert source().fetch_leaderboards(()) == {}
    assert api.calls == []


@pytest.mark.parametrize("window", sorted(lighter.LEADERBOARD_WINDOWS.values()))
def test_windows_map_one_to_one(window):
    assert window in {"24h", "7d", "30d", "all"}
