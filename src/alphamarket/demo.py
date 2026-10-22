# ruff: noqa: E501  (sample-data prose; wrapping it would only hurt readability)
"""Sample data for the demo build (DEMO_MODE=true).

Everything here is invented: names, handles, markets, curves and every number.
It exists so the accelerator can see the whole product (marketplace, paper tailing,
portfolio, feed) before those parts can be computed from venue data. Every
page that draws on this module renders a "Sample data" banner, `pages.py`
returns 404 for these routes when demo mode is off, and `scripts/preflight.py`
refuses to launch with it on. Invariant #5 (public pages show only
platform-computed numbers) holds for real users because the demo dataset
never reaches a deployment that passes preflight.

Shapes mirror `public.page_data` / `public.list_visible` so the same templates
render real and sample pages; sample pages carry extra keys (benchmarks,
worst periods, paper-tail terms) that real pages simply omit. Asset samples
(crypto spot, perps, US equities) carry the v2 page shape: holdings and
exposure built through `public.holdings_view` / `exposure_view`, recent
trades as signals. Tailing is paper only (invariant 8) and pricing is flat:
nothing here describes a live account, a performance fee or a high-water mark.
"""

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from alphamarket import trader_profile as tp
from alphamarket.charts import drawdown_window, polyline, window_x
from alphamarket.config import get_settings
from alphamarket.connectors.base import InstrumentType
from alphamarket.connectors.registry import PROFILES
from alphamarket.public import (
    ASSET_CLASS_LABELS,
    exposure_view,
    holdings_view,
    verification_view,
)
from alphamarket.tail_terms import PRICING_ROWS, paper_terms, tailing_card, venue_label

VERIFIED_SINCE = date(2026, 5, 6)
AS_OF = date(2026, 9, 1)


def _walk(seed: int, n: int, drift: float, vol: float, start: float = 100.0) -> list[float]:
    r = random.Random(seed)
    v = start
    out = []
    for _ in range(n):
        v *= 1 + r.gauss(drift, vol)
        out.append(v)
    return out


def _drawdown(vals: list[float]) -> list[float]:
    """Positive drawdown fractions, the shape `stats.drawdown_series` returns."""
    peak = vals[0]
    out = []
    for v in vals:
        peak = max(peak, v)
        out.append((peak - v) / peak)
    return out


def _dates(n: int, start: date = VERIFIED_SINCE) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


# ---------------------------------------------------------------- strategies

_STRATEGIES = [
    dict(
        slug="fed-path-both-directions",
        name="Fed path, both directions",
        handle="rateswatch",
        tags=["macro", "rates"],
        seed=21,
        drift=0.0028,
        vol=0.022,
        days=134,
        total_return=0.407,
        max_drawdown=0.078,
        sharpe=2.10,
        sortino=2.74,
        risk_score=4,
        win_rate=0.61,
        n_settlements=188,
        n_fills=902,
        paper_tails=31,
        score=8.7,
        profitable_months="4 of 5",
        worst_month=-0.021,
        largest_loss=-141.10,
        concentration=0.18,
        description="Trade the FOMC path in both directions: fade the crowd when a meeting is priced past 85% either way, and hold the opposite tail small.",
        worst_periods=[
            (
                "Jun 2 to Jun 14",
                -0.078,
                "13 days",
                "Two meetings repriced against the book in a week.",
            ),
            ("Jul 21 to Jul 25", -0.034, "5 days", ""),
        ],
    ),
    dict(
        slug="election-polling-arbitrage",
        name="Election polling arbitrage",
        handle="median_voter",
        tags=["politics"],
        seed=13,
        drift=0.0022,
        vol=0.015,
        days=201,
        total_return=0.298,
        max_drawdown=0.043,
        sharpe=2.62,
        sortino=3.10,
        risk_score=3,
        win_rate=0.64,
        n_settlements=241,
        n_fills=1310,
        paper_tails=54,
        score=8.4,
        profitable_months="6 of 7",
        worst_month=-0.012,
        largest_loss=-88.00,
        concentration=0.11,
        description="Buy the side that polling averages support when the contract lags the average by more than its historical error band. Quiet between cycles.",
        worst_periods=[
            (
                "Mar 9 to Mar 20",
                -0.043,
                "12 days",
                "Primary results ran against three state contracts.",
            ),
            ("Aug 4 to Aug 6", -0.019, "3 days", ""),
        ],
    ),
    dict(
        slug="weather-cpi-mean-reversion",
        name="Weather & CPI mean reversion",
        handle="quietedge",
        tags=["macro", "weather"],
        seed=7,
        drift=0.0032,
        vol=0.020,
        days=118,
        total_return=0.325,
        max_drawdown=0.143,
        sharpe=1.84,
        sortino=2.31,
        risk_score=6,
        win_rate=0.58,
        n_settlements=214,
        n_fills=1204,
        paper_tails=23,
        score=7.1,
        profitable_months="4 of 5",
        worst_month=-0.031,
        largest_loss=-212.40,
        concentration=0.22,
        description="Fade extreme prints on weather and CPI contracts in the last 48 hours before settlement, when retail flow pushes YES past what the underlying forecast supports. Flat sizing, never adding to losers. Sizing is limited by liquidity in the daily temperature series.",
        worst_periods=[
            (
                "Jul 3 to Jul 29",
                -0.143,
                "31 days",
                "Heat-wave contracts settled against the model four days running.",
            ),
            ("Jun 11 to Jun 16", -0.061, "9 days", ""),
            ("Aug 20 to Aug 23", -0.047, "6 days", ""),
        ],
    ),
    dict(
        slug="long-tail-sports-underdogs",
        name="Long-tail sports underdogs",
        handle="tailrisk",
        tags=["sports"],
        seed=5,
        drift=0.004,
        vol=0.035,
        days=92,
        total_return=0.365,
        max_drawdown=0.155,
        sharpe=1.21,
        sortino=1.52,
        risk_score=8,
        win_rate=0.31,
        n_settlements=402,
        n_fills=1880,
        paper_tails=9,
        score=5.9,
        profitable_months="2 of 4",
        worst_month=-0.088,
        largest_loss=-310.00,
        concentration=0.35,
        description="Small, many, ugly: underdogs priced under 15% across every league, sized so a full week of losses costs under 4%.",
        worst_periods=[
            ("Jul 12 to Aug 2", -0.155, "22 days", "Nineteen straight losing settlements."),
            ("Jun 20 to Jun 24", -0.062, "4 days", ""),
        ],
    ),
    dict(
        slug="earnings-day-binaries",
        name="Earnings-day binaries",
        handle="printday",
        tags=["finance"],
        seed=3,
        drift=0.0015,
        vol=0.012,
        days=96,
        total_return=0.116,
        max_drawdown=0.190,
        sharpe=0.44,
        sortino=0.61,
        risk_score=9,
        win_rate=0.49,
        n_settlements=77,
        n_fills=350,
        paper_tails=3,
        score=3.2,
        profitable_months="2 of 4",
        worst_month=-0.121,
        largest_loss=-560.00,
        concentration=0.41,
        description="Directional bets on whether a company beats, sized by implied move. Concentrated by design.",
        worst_periods=[
            ("Jul 28 to Aug 8", -0.190, "12 days", "Four large caps missed in one week."),
            ("Jun 4 to Jun 9", -0.071, "5 days", ""),
        ],
    ),
    dict(
        slug="daily-temperature-spreads",
        name="Daily temperature spreads",
        handle="quietedge",
        tags=["weather"],
        seed=9,
        drift=-0.0005,
        vol=0.020,
        days=101,
        total_return=-0.081,
        max_drawdown=0.170,
        sharpe=-0.31,
        sortino=-0.40,
        risk_score=7,
        win_rate=0.47,
        n_settlements=160,
        n_fills=690,
        paper_tails=0,
        score=1.8,
        profitable_months="1 of 4",
        worst_month=-0.064,
        largest_loss=-96.20,
        concentration=0.28,
        description="Spread trades between neighbouring city temperature contracts. Has not worked this summer; kept public because a record is a record.",
        worst_periods=[
            (
                "Jun 1 to Aug 30",
                -0.170,
                "not recovered",
                "Persistent drift; the model is being re-examined.",
            ),
            ("Jul 7 to Jul 9", -0.052, "2 days", ""),
        ],
    ),
    # ---- asset venues (crypto spot, perps, US equities): the v2 page shape,
    # holdings and exposure instead of settlements, recent trades as signals.
    dict(
        slug="btc-eth-weekly-trend",
        name="BTC/ETH trend, weekly rebalance",
        handle="slowcandle",
        venue="kraken",
        tags=["crypto", "trend"],
        seed=31,
        drift=0.0021,
        vol=0.024,
        days=162,
        start=12000.0,
        total_return=0.284,
        max_drawdown=0.162,
        sharpe=1.38,
        sortino=1.91,
        risk_score=6,
        n_fills=214,
        paper_tails=41,
        score=7.4,
        description="Long BTC and ETH while the 20-week trend is up, scaled out to stablecoins when it breaks. Rebalanced on Sunday evenings, so expect to give back the first leg of every reversal.",
        worst_periods=[
            (
                "Jun 17 to Jul 8",
                -0.162,
                "29 days",
                "Two trend breaks in a row; the weekly rule gave back gains both times.",
            ),
            ("Aug 2 to Aug 6", -0.058, "5 days", ""),
        ],
        holdings=[
            ("BTC", "spot", 0.48, 63412.50, None),
            ("ETH", "spot", 0.31, 2648.10, None),
            ("USDC", "cash", 0.21, None, None),
        ],
        signals=[
            ("BTC/USD", "2026-08-31T19:02:11", "buy", "0.0315", "63,388.20", 0.13),
            ("ETH/USD", "2026-08-31T19:02:40", "buy", "0.412", "2,646.90", 0.07),
            ("ETH/USD", "2026-08-24T19:01:52", "sell", "0.290", "2,701.35", 0.05),
            ("BTC/USD", "2026-08-17T19:03:05", "sell", "0.0120", "61,950.00", 0.05),
            ("BTC/USD", "2026-08-10T19:02:27", "buy", "0.0240", "60,410.80", 0.09),
        ],
    ),
    dict(
        slug="large-cap-alt-rotation",
        name="Large-cap alt rotation",
        handle="rotationlab",
        venue="coinbase",
        tags=["crypto", "momentum"],
        seed=17,
        drift=0.0034,
        vol=0.038,
        days=121,
        start=8000.0,
        total_return=0.412,
        max_drawdown=0.247,
        sharpe=1.12,
        sortino=1.44,
        risk_score=8,
        n_fills=388,
        paper_tails=18,
        score=6.2,
        description="Hold the three strongest of the top twenty coins by 30-day return, re-ranked every Monday. No leverage. Drawdowns are deep and fast; the edge is in the months that run.",
        worst_periods=[
            (
                "Jul 1 to Jul 26",
                -0.247,
                "34 days",
                "Rotation bought the leaders just before they reversed.",
            ),
            ("Aug 12 to Aug 15", -0.083, "6 days", ""),
        ],
        holdings=[
            ("SOL", "spot", 0.37, 148.62, None),
            ("LINK", "spot", 0.29, 13.874, None),
            ("AVAX", "spot", 0.26, 27.41, None),
            ("USD", "cash", 0.08, None, None),
        ],
        signals=[
            ("LINK-USD", "2026-08-31T13:00:41", "buy", "176.40", "13.862", 0.27),
            ("ARB-USD", "2026-08-31T13:00:18", "sell", "2,410.0", "0.8121", 0.17),
            ("SOL-USD", "2026-08-24T13:01:02", "buy", "6.25", "151.30", 0.08),
            ("AVAX-USD", "2026-08-17T13:00:55", "buy", "41.0", "26.95", 0.10),
            ("DOGE-USD", "2026-08-17T13:00:21", "sell", "9,200", "0.1043", 0.08),
        ],
    ),
    dict(
        slug="funding-aware-btc-perps",
        name="Funding-aware BTC perps",
        handle="basisbook",
        venue="hyperliquid",
        tags=["crypto", "perps"],
        seed=23,
        drift=0.0018,
        vol=0.016,
        days=104,
        start=25000.0,
        total_return=0.196,
        max_drawdown=0.094,
        sharpe=1.71,
        sortino=2.20,
        risk_score=7,
        n_fills=1146,
        paper_tails=27,
        score=7.0,
        description="Long BTC perps when funding is negative or flat, short ETH against it when ETH funding runs hot. Gross leverage capped at 2x; every position is visible on-chain.",
        worst_periods=[
            (
                "Aug 5 to Aug 13",
                -0.094,
                "11 days",
                "Short ETH leg squeezed while funding stayed high.",
            ),
            ("Jun 22 to Jun 25", -0.037, "4 days", ""),
        ],
        holdings=[
            ("USDC", "cash", 1.0, None, None),
            ("BTC-PERP", "perp", 1.35, 63405.0, "61,980.0"),
            ("ETH-PERP", "perp", -0.42, 2647.2, "2,702.4"),
        ],
        signals=[
            ("BTC-PERP", "2026-09-01T03:14:09", "buy", "0.085", "63,402.0", 0.22),
            ("ETH-PERP", "2026-08-31T21:40:33", "sell", "1.20", "2,651.8", 0.13),
            ("BTC-PERP", "2026-08-30T11:05:47", "sell", "0.060", "63,980.0", 0.15),
            ("ETH-PERP", "2026-08-28T16:22:10", "buy", "0.80", "2,688.1", 0.09),
            ("BTC-PERP", "2026-08-27T08:51:36", "buy", "0.140", "61,210.0", 0.34),
        ],
    ),
    dict(
        slug="quality-dividend-compounders",
        name="Quality dividend compounders",
        handle="plainvanilla",
        venue="alpaca",
        tags=["equities", "dividends"],
        seed=41,
        drift=0.0009,
        vol=0.0085,
        days=188,
        start=15000.0,
        total_return=0.112,
        max_drawdown=0.061,
        sharpe=1.52,
        sortino=2.05,
        risk_score=3,
        n_fills=96,
        paper_tails=64,
        score=7.9,
        description="Twelve US companies with ten years of rising dividends and low debt, equal weight, trimmed back when one passes 12% of the book. Dividends are reinvested by hand each month.",
        worst_periods=[
            (
                "Apr 3 to Apr 21",
                -0.061,
                "26 days",
                "Rate scare hit utilities and staples together.",
            ),
            ("Jul 29 to Aug 2", -0.024, "4 days", ""),
        ],
        holdings=[
            ("MSFT", "equity", 0.11, 431.20, None),
            ("JNJ", "equity", 0.09, 162.85, None),
            ("PG", "equity", 0.09, 168.40, None),
            ("KO", "equity", 0.08, 69.12, None),
            ("HD", "equity", 0.08, 371.55, None),
            ("PEP", "equity", 0.08, 172.30, None),
            ("USD", "cash", 0.47, None, None),
        ],
        signals=[
            ("KO", "2026-08-28T14:31:07", "buy", "12", "68.94", 0.06),
            ("MSFT", "2026-08-21T15:02:44", "sell", "3", "438.10", 0.09),
            ("PG", "2026-08-14T14:45:20", "buy", "5", "167.25", 0.06),
            ("JNJ", "2026-08-07T19:12:58", "buy", "4", "160.40", 0.04),
            ("HD", "2026-07-31T14:30:11", "buy", "2", "365.90", 0.05),
        ],
        broker_equity=True,
    ),
    dict(
        slug="semis-swing-long-only",
        name="Semis swing, long only",
        handle="fabwatch",
        venue="ibkr",
        tags=["equities", "momentum"],
        seed=11,
        drift=0.0024,
        vol=0.019,
        days=76,
        start=40000.0,
        total_return=0.137,
        max_drawdown=0.118,
        sharpe=1.29,
        sortino=1.63,
        risk_score=6,
        n_fills=58,
        paper_tails=7,
        score=4.6,
        description="Swing trades in semiconductor names and SMH around earnings and supply-chain data. Long only, two to six positions, held days to weeks.",
        worst_periods=[],
        holdings=[
            ("NVDA", "equity", 0.28, 118.65, None),
            ("SMH", "equity", 0.24, 238.10, None),
            ("AMD", "equity", 0.17, 152.42, None),
            ("USD", "cash", 0.31, None, None),
        ],
        signals=[
            ("AMD", "2026-08-29T15:47:00", "buy", "45", "151.10", 0.17),
            ("TSM", "2026-08-27T19:55:00", "sell", "60", "171.80", 0.26),
            ("NVDA", "2026-08-20T13:41:00", "buy", "95", "116.20", 0.28),
            ("SMH", "2026-08-12T14:02:00", "buy", "40", "231.75", 0.23),
        ],
        tier="daily_verified",
        broker_equity=True,
    ),
]

_CREATORS = {
    "rateswatch": dict(
        bio="Rates desk refugee. Every FOMC since 2019, on Kalshi since 2023.",
        followers=512,
        since="Apr 2026",
    ),
    "median_voter": dict(
        bio="Polling nerd. I trade the gap between the average and the price.",
        followers=891,
        since="Feb 2026",
    ),
    "quietedge": dict(
        bio="Macro and weather contracts on Kalshi since 2024. I do not post trades; the page is the proof.",
        followers=340,
        since="May 2026",
    ),
    "tailrisk": dict(
        bio="Underdogs only. Expect long losing streaks.", followers=204, since="Jun 2026"
    ),
    "printday": dict(bio="Earnings season is the whole year.", followers=77, since="Jun 2026"),
    "slowcandle": dict(
        bio="Trend follower. Two coins, one rule, rebalanced on Sundays.",
        followers=618,
        since="Mar 2026",
    ),
    "rotationlab": dict(
        bio="Momentum across the top twenty coins. Deep drawdowns, posted as they happen.",
        followers=233,
        since="May 2026",
    ),
    "basisbook": dict(
        bio="Perps and funding on Hyperliquid. The address is public; so is every position.",
        followers=402,
        since="May 2026",
    ),
    "plainvanilla": dict(
        bio="Boring on purpose. Dividend growers, equal weight, almost no trading.",
        followers=1104,
        since="Feb 2026",
    ),
    "fabwatch": dict(
        bio="Ex-fab engineer. I trade the chip cycle, long only.",
        followers=151,
        since="Jun 2026",
    ),
}

BENCHMARK_NOTE = (
    "S&P 500 and T-bill lines are sample benchmarks; the live benchmark set is an open question."
)
CRYPTO_BENCHMARK_NOTE = (
    "Bitcoin and T-bill lines are sample benchmarks; the live benchmark set is an open question."
)

_STRIP_SPARK = 60


def _series(s: dict) -> tuple[list[date], list[float]]:
    n = s["days"]
    dates = _dates(n, AS_OF - timedelta(days=n - 1))
    if "start" in s:  # asset samples: growth of the first day's equity, end pinned exactly
        start = s["start"]
        curve = [start] + _walk(s["seed"], n - 1, s["drift"], s["vol"], start=start)
        k = start * (1 + s["total_return"]) / curve[-1]
        return dates, [v * ((k - 1) * i / (n - 1) + 1) for i, v in enumerate(curve)]
    curve = _walk(s["seed"], n, s["drift"], s["vol"], start=6600.0)
    # Pin the end so the headline return matches the curve exactly.
    target = 6600.0 * (1 + s["total_return"])
    k = target / curve[-1]
    curve = [6600.0 + (v - 6600.0) * ((k - 1) * i / (n - 1) + 1) for i, v in enumerate(curve)]
    return dates, curve


def _max_dd(s: dict) -> float:
    """Asset samples take their worst drawdown from their own curve, so the
    headline, the shaded window and the drawdown chart agree."""
    if "start" not in s:
        return s["max_drawdown"]
    return max(_drawdown(_series(s)[1]))


def _worst_periods(s: dict) -> list[tuple]:
    """Asset samples: the worst period is read off the curve (peak to trough,
    then days to recover), carrying the sample's note; binary samples keep
    their hand-written list."""
    if "start" not in s or not s["worst_periods"]:
        return s["worst_periods"]
    dates, curve = _series(s)
    a, b = drawdown_window(curve)
    back = next((i for i in range(b, len(curve)) if curve[i] >= curve[a]), None)
    span = f"{dates[a]:%b %-d} to {dates[b]:%b %-d}"
    recovery = f"{(dates[back] - dates[a]).days} days" if back is not None else "not recovered"
    first = (span, -(1 - curve[b] / curve[a]), recovery, s["worst_periods"][0][3])
    return [first]


def _operator(s: dict) -> str:
    """Who runs the sample: "creator" unless the sample says "koine"."""
    return s.get("operator", "creator")


def _venue(s: dict) -> str:
    return s.get("venue", "kalshi")


_venue_label = venue_label


def _row(s: dict) -> dict:
    dates, curve = _series(s)
    venue = _venue(s)
    classes = PROFILES[venue].asset_classes
    return {
        "slug": s["slug"],
        "name": s["name"],
        "display_name": s["handle"],
        "tags": s["tags"],
        "incubating": s["days"] < 90,
        "venue": venue,
        "venue_label": _venue_label(venue),
        "asset_classes": list(classes),
        "asset_class_label": ASSET_CLASS_LABELS[classes[0]],
        "verification_tier": s.get("tier", "reconciled"),
        "total_return": s["total_return"],
        "max_drawdown": _max_dd(s),
        "risk_score": s["risk_score"],
        "track_days": s["days"],
        "reconciled": True,
        "spark": polyline(curve[-_STRIP_SPARK:], 120, 32),
        "spark_dd": window_x(curve[-_STRIP_SPARK:], 120),
        "sharpe": s["sharpe"],
        "paper_tails": s["paper_tails"],
        "score": s["score"],
        "demo": True,
        "operator": _operator(s),
    }


def marketplace_rows() -> list[dict]:
    """Sample rows for the marketplace, ranked by the sample Koine score."""
    return sorted((_row(s) for s in _STRATEGIES), key=lambda r: r["score"], reverse=True)


_TICKER = [
    ("Weather & CPI mean reversion", "KXHIGHNY-26AUG31-T90", "no", True, 60.0, "2026-09-01"),
    ("Fed path, both directions", "KXFED-26SEP-25", "yes", True, 200.0, "2026-09-01"),
    ("Daily temperature spreads", "KXHIGHCHI-26AUG31-T84", "yes", False, 0.0, "2026-09-01"),
    ("Election polling arbitrage", "KXSEN-26OH-D", "no", True, 150.0, "2026-08-31"),
    ("Weather & CPI mean reversion", "KXHIGHDEN-26AUG30-T95", "no", True, 60.0, "2026-08-31"),
    ("Long-tail sports underdogs", "KXNFLTOT-26W1-KCBAL-O47", "no", False, 0.0, "2026-08-31"),
    ("Fed path, both directions", "KXJOBS-26AUG-150", "no", True, 150.0, "2026-08-29"),
    ("Earnings-day binaries", "KXAAPL-26AUG-B215", "yes", True, 90.0, "2026-08-29"),
    ("Election polling arbitrage", "KXGOV-26NJ-D", "yes", True, 110.0, "2026-08-28"),
    ("Daily temperature spreads", "KXHIGHLAX-26AUG27-T88", "no", True, 40.0, "2026-08-28"),
]


def ticker() -> list[dict]:
    """Sample settled trades for the landing ticker; shape mirrors
    `public.recent_public_settlements`."""
    return [
        {
            "strategy": name,
            "ticker": t,
            "side": side,
            "won": won,
            "payout": pay,
            "settled_at": when,
            "demo": True,
        }
        for name, t, side, won, pay, when in _TICKER
    ]


def featured() -> dict:
    return _row(_by_slug("weather-cpi-mean-reversion"))


def _by_slug(slug: str) -> dict | None:
    return next((s for s in _STRATEGIES if s["slug"] == slug), None)


def has_strategy(slug: str) -> bool:
    return _by_slug(slug) is not None


def _monthly(dates: list[date], curve: list[float]) -> list[list]:
    out = []
    first: dict[tuple[int, int], float] = {}
    last: dict[tuple[int, int], float] = {}
    for d, v in zip(dates, curve, strict=True):
        key = (d.year, d.month)
        first.setdefault(key, v)
        last[key] = v
    keys = sorted(first)
    for i, key in enumerate(keys):
        base = last[keys[i - 1]] if i else first[key]
        out.append([key[0], key[1], last[key] / base - 1])
    return out


def _usd(x: float) -> str:
    return f"{'−' if x < 0 else ''}${abs(x):,.2f}"


def _asset_view(s: dict, equity: float) -> dict:
    """Holdings and exposure for an asset sample, built through the same
    `public.holdings_view` / `exposure_view` a real v2 page uses. Spot and
    equity weights are shares of equity; a perp's weight is signed notional
    over equity and its value is unrealized P&L against the entry."""
    rows, by_underlying = [], {}
    unrealized = 0.0
    for asset, kind, weight, price, entry in s["holdings"]:
        if kind == "perp":
            qty = weight * equity / price
            value = qty * (price - float(entry.replace(",", "")))
            unrealized += value
            notional = abs(qty * price)
            by_underlying[asset.split("-")[0]] = round(notional * 1e6)
            rows.append(
                {
                    "asset": asset,
                    "kind": "perp",
                    "qty": f"{qty:+.4f}",
                    "close_micros": round(price * 1e6),
                    "value_micros": round(value * 1e6),
                    "notional_micros": round(notional * 1e6),
                    "entry": entry,
                    "price_day": AS_OF.isoformat(),
                }
            )
        elif kind == "cash":
            rows.append({"asset": asset, "kind": "cash", "qty": None, "value_micros": 0})
        else:
            value = weight * equity
            qty = value / price
            places = 0 if kind == "equity" else (4 if price > 1000 else 2)
            by_underlying[asset] = round(value * 1e6)
            rows.append(
                {
                    "asset": asset,
                    "kind": kind,
                    "qty": f"{qty:,.{places}f}",
                    "close_micros": round(price * 1e6),
                    "value_micros": round(value * 1e6),
                    "price_day": AS_OF.isoformat(),
                }
            )
    invested = sum(r["value_micros"] for r in rows if r["kind"] in ("spot", "equity"))
    for r in rows:  # cash is what is left of equity after positions and open perp P&L
        if r["kind"] == "cash":
            cash = round((equity - unrealized) * 1e6) - invested
            r["value_micros"] = cash
            r["qty"] = f"{cash / 1e6:,.2f}"
    gross = sum(by_underlying.values())
    top = max(by_underlying, key=by_underlying.get)
    perp_notional = sum(r["notional_micros"] for r in rows if r["kind"] == "perp")
    extras = {
        "holdings": rows,
        "exposure": {
            "day": AS_OF.isoformat(),
            "gross_micros": gross,
            "by_underlying_micros": by_underlying,
            "top_asset": top,
            "top_share": by_underlying[top] / gross,
            "perp_gross_leverage": perp_notional / (equity * 1e6) if perp_notional else None,
        },
    }
    return {
        "holdings": holdings_view(extras),
        "exposure": exposure_view(extras),
        "n_open": sum(1 for r in rows if r["kind"] != "cash"),
    }


_BINARY_DISCUSSION = [
    ("isobar", "2h", False, "July drawdown was the Phoenix heat series. Has sizing changed since?"),
    (None, "1h", True, "Cap per series is now 15% of book. Visible in the concentration number."),
]
_ASSET_DISCUSSION = [
    (
        "isobar",
        "3h",
        False,
        "My paper tail is about 0.4% behind your curve this month. Is that just the spread?",
    ),
    (
        None,
        "2h",
        True,
        "Mostly, plus copy lag on the Sunday rebalance. Your portfolio page shows the lag per fill.",
    ),
]


def strategy_page(slug: str) -> dict | None:
    s = _by_slug(slug)
    if s is None:
        return None
    dates, curve = _series(s)
    dd = _drawdown(curve)
    n = len(curve)
    venue = _venue(s)
    profile = PROFILES[venue]
    asset_class = profile.asset_classes[0]
    is_asset = "holdings" in s
    crypto = asset_class == "crypto"
    bench = _walk(5, n, 0.0006, 0.025, start=curve[0]) if crypto else None
    spx = (
        _walk(5, n, 0.0003, 0.009, start=curve[0])
        if is_asset
        else _walk(4, n, 0.00055, 0.009, start=curve[0])
    )
    cash = [curve[0] * (1 + 0.000115) ** i for i in range(n)]
    benchmark = (
        ("Bitcoin, buy and hold", bench, "Bitcoin") if crypto else ("S&P 500", spx, "S&P 500")
    )
    c = _CREATORS.get(s["handle"], {"followers": 0})  # Koine-run samples have no profile
    mine = [x for x in _STRATEGIES if x["handle"] == s["handle"]]
    curve_rows = [[d.isoformat(), v] for d, v in zip(dates, curve, strict=True)]
    page = {
        "demo": True,
        "operator": _operator(s),
        "slug": s["slug"],
        "name": s["name"],
        "display_name": s["handle"],
        "venue": venue,
        "venue_label": _venue_label(venue),
        "venue_env": "prod",
        "asset_class": asset_class,
        "asset_class_label": ASSET_CLASS_LABELS[asset_class],
        "is_asset": is_asset,
        "status": "live",
        "style_tags": s["tags"],
        "description_unverified": s["description"],
        "verified_since": dates[0].isoformat(),
        "last_reconciled": AS_OF.isoformat() + "T02:14:00+00:00",
        "reconciled": True,
        "verification_tier": s.get("tier", "reconciled"),
        "verification": verification_view(profile, s.get("tier", "reconciled")),
        "incubating": s["days"] < 90,
        "stats": {
            "as_of": AS_OF.isoformat(),
            "track_days": s["days"],
            "n_fills": s["n_fills"],
            "n_settlements": s.get("n_settlements", 0),
            "total_return": s["total_return"],
            "max_drawdown": _max_dd(s),
            "sharpe": s["sharpe"],
            "sortino": s["sortino"],
            "win_rate": s.get("win_rate"),
            "risk_score": s["risk_score"],
            "equity": curve[-1],
            "realized_pnl": curve[-1] - curve[0],
            "flags": [],
            "top_series_share": s.get("concentration"),
            "equity_curve": curve_rows,
            "drawdown_curve": [[d.isoformat(), x] for d, x in zip(dates, dd, strict=True)],
            "monthly_returns": _monthly(dates, curve),
        },
        "benchmarks": [(benchmark[0], benchmark[1], False), ("Cash, T-bill", cash, True)],
        "benchmark_label": benchmark[2],
        "benchmark_note": CRYPTO_BENCHMARK_NOTE if crypto else BENCHMARK_NOTE,
        "vs_benchmark": s["total_return"] - (benchmark[1][-1] / benchmark[1][0] - 1),
        "worst_periods": _worst_periods(s),
        "tailing": tailing_card(venue, s["paper_tails"]),
        "creator": {"handle": s["handle"], "followers": c["followers"], "strategies": len(mine)},
        "discussion": [
            (h or s["handle"], when, is_creator, body)
            for h, when, is_creator, body in (_ASSET_DISCUSSION if is_asset else _BINARY_DISCUSSION)
        ],
    }
    if is_asset:
        view = _asset_view(s, curve[-1])
        page["stats"].update(
            {
                "valuation": "v2",
                "chart_curve": curve_rows,
                "holdings": view["holdings"],
                "exposure": view["exposure"],
                "n_open_positions": view["n_open"],
                "twr": s["total_return"],
                "asset_classes": [asset_class],
                "price_sources": [venue],
                "unpriced": [],
                "carried_forward_days": 0,
                "perp_entry_unknown": [],
                "broker_equity": (
                    {
                        "ok": True,
                        "venue": _usd(curve[-1]),
                        "computed": _usd(curve[-1]),
                        "as_of": AS_OF.isoformat(),
                    }
                    if s.get("broker_equity")
                    else None
                ),
            }
        )
        page["recent_signals"] = [
            {
                "kind": "fill",
                "asset": True,
                "at": at + "+00:00",
                "ticker": ticker,
                "action": action,
                "qty": qty,
                "price": price,
                "stake_fraction": stake,
            }
            for ticker, at, action, qty, price, stake in s["signals"]
        ]
        return page
    page["recent_settlements"] = [
        {
            "ticker": t,
            "result": side if pay else ("no" if side == "yes" else "yes"),
            "side": side,
            "count": float(count),
            "payout": pay,
            "settled_at": when,
        }
        for t, side, count, pay, when in [
            ("KXHIGHDEN-26AUG30-T95", "no", 60, 0.0, "2026-08-31"),
            ("KXJOBS-26AUG-150", "no", 150, 150.0, "2026-08-29"),
            ("KXFED-26SEP-25", "yes", 200, 200.0, "2026-08-28"),
            ("KXHIGHNY-26AUG22-T91", "yes", 80, 0.0, "2026-08-23"),
            ("KXCPI-26JUL-0.3", "no", 120, 120.0, "2026-08-12"),
        ]
    ]
    page["risk_rows"] = [
        ("Sharpe (annualised)", f"{s['sharpe']:.2f}"),
        ("Sortino", f"{s['sortino']:.2f}"),
        ("Worst month", f"{s['worst_month'] * 100:+.1f}%"),
        ("Profitable months", s["profitable_months"]),
        ("Win rate", f"{s['win_rate'] * 100:.0f}%"),
        ("Largest single loss", f"${s['largest_loss']:,.2f}"),
        ("Series concentration", f"{s['concentration'] * 100:.0f}% in one series"),
        ("Settled trades", str(s["n_settlements"])),
    ]
    return page


def tail_page(slug: str) -> dict | None:
    """The paper-tail setup flow. Paper only (invariant 8): there is no
    account, key or balance to choose, and nothing is placed anywhere."""
    page = strategy_page(slug)
    if page is None:
        return None
    allocation = 2000.00
    dd_limit = 0.15
    return {
        "demo": True,
        "strategy": page,
        "allocation": allocation,
        "allocation_max": 25000,
        "min_usd": 100,
        "dd_limit": dd_limit,
        "stop_at": allocation * (1 - dd_limit),
        # The sample portfolio's stopped stock tail is still closing: its tail
        # page carries the same notice a real one does.
        "closing": slug == CLOSING_TAIL_SLUG,
        "terms": paper_terms(page["venue"])["rows"],
        "pricing_rows": PRICING_ROWS,
    }


def creator(handle: str) -> dict | None:
    c = _CREATORS.get(handle)
    if c is None:
        return None
    mine = [_row(s) for s in _STRATEGIES if s["handle"] == handle]
    best = max(mine, key=lambda r: r["total_return"])
    worst = min(mine, key=lambda r: r["total_return"])
    posts = [p for p in _POSTS if p["handle"] == handle]
    return {
        "demo": True,
        "handle": handle,
        "bio": c["bio"],
        "followers": c["followers"],
        "since": c["since"],
        "venues": sorted({r["venue_label"] for r in mine}),
        "strategies": mine,
        "best": best,
        "worst": worst,
        "worst_dd": max(r["max_drawdown"] for r in mine),
        "paper_tails": sum(r["paper_tails"] for r in mine),
        "nights": max(r["track_days"] for r in mine),
        "posts": posts,
    }


def _ctx(handle: str) -> str | None:
    mine = [s for s in _STRATEGIES if s["handle"] == handle]
    if not mine:
        return None
    best = max(mine, key=lambda s: s["total_return"])
    return f"{best['total_return'] * 100:+.1f}% · −{best['max_drawdown'] * 100:.1f}% DD verified"


_POSTS = [
    dict(
        handle="rateswatch",
        when="35m",
        likes=18,
        replies=4,
        strategy="fed-path-both-directions",
        body="September meeting is priced at 86% for a cut. I am on the NO side small, and the page will show it either way.",
    ),
    dict(
        handle="basisbook",
        when="1h",
        likes=21,
        replies=5,
        strategy="funding-aware-btc-perps",
        body="ETH funding is back above 30% annualised, so the short leg is on again at 0.4x. Gross leverage stays under 2x; the exposure panel shows it.",
    ),
    dict(
        handle="quietedge",
        when="2h",
        likes=12,
        replies=3,
        strategy=None,
        body="Cap per series is now 15% of book after the July heat-wave run. You can see it in the concentration figure on the page. No other changes.",
    ),
    dict(
        handle="median_voter",
        when="5h",
        likes=33,
        replies=7,
        strategy=None,
        body="Reminder for anyone tailing: this strategy goes quiet between election cycles. Expect flat months, not zeros because something broke.",
    ),
    dict(
        handle="newtrader_88",
        when="yesterday",
        likes=2,
        replies=1,
        strategy=None,
        body="Anyone else looking at the Q4 GDP contracts? Feels mispriced.",
    ),
    dict(
        handle="quietedge",
        when="Aug 29",
        likes=41,
        replies=9,
        strategy=None,
        body="August was the best month on record (+18.4%) and I want to be clear it was mostly two CPI contracts. Do not extrapolate that.",
    ),
    dict(
        handle="quietedge",
        when="Aug 12",
        likes=27,
        replies=6,
        strategy=None,
        body="Drawdown ran to 14.3% in July. Nothing about the model broke; the heat series settled against the forecast four days straight. Kept sizing flat.",
    ),
]


def feed() -> dict:
    posts = []
    for p in _POSTS[:4]:
        row = dict(p, context=_ctx(p["handle"]))
        if p["strategy"]:
            row["strategy_row"] = _row(_by_slug(p["strategy"]))
        posts.append(row)
    return {
        "demo": True,
        "posts": posts,
        "rooms": [("macro", 412), ("weather", 128), ("politics", 390), ("tailing-questions", 96)],
        "thread": [
            (
                "isobar",
                "10:14",
                "Is the reconciliation panel about the creator's account, or my paper tail?",
            ),
            (
                "koine",
                "10:16",
                "The creator's. A paper tail has no venue account; its fills, fees and copy lag are on your Portfolio page.",
            ),
            (
                "tailrisk",
                "10:20",
                "Would love a filter for strategies with no correlation to my current three.",
            ),
            (
                "median_voter",
                "10:31",
                "Correlation column would help creators too. Followers keep asking me.",
            ),
        ],
    }


# Two stopped tails in the sample portfolio (copy strategies decisions 8 and 9:
# a stop closes every crypto and stock position). Each is built from its book
# (cash plus positions), so the numbers add up by construction and a test pins
# the arithmetic. Prices are invented.
#
# Closed crypto tail (Coinbase, 0.90% paper taker fee, drawdown limit 10%):
#   allocated 1,500.00; high 1,620.00. The stop recorded the pre-close equity:
#   cash 120.00 + 0.20 ETH x 3,310.00 (662.00) + 4 SOL x 167.50 (670.00)
#   = 1,452.00, a 10.37% drawdown from the high, past the limit. The close then
#   sold at the bid: 0.20 ETH x 3,290.00 + 4 SOL x 165.50 = 658.00 + 662.00 =
#   1,320.00, fee 1,320.00 x 0.90% = 11.88. Flat: equity is just cash,
#   120.00 + 1,320.00 - 11.88 = 1,428.12 (return -4.79%, drawdown 11.84%).
_CLOSED_CRYPTO = dict(
    slug="large-cap-alt-rotation",
    allocated=1500.00,
    high=1620.00,
    limit=0.10,
    cash_before_close=120.00,
    marks=[("ETH/USD", 0.20, 3310.00), ("SOL/USD", 4.0, 167.50)],
    bids=[("ETH/USD", 0.20, 3290.00), ("SOL/USD", 4.0, 165.50)],
    fee_rate=0.009,
)
# Closing stock tail (Alpaca): the user pressed Stop at 20:30 UTC on Aug 31,
# after the close, so there is no fresh quote and the positions wait for the
# next one. Book at the last close: cash 1,096.40 + 12 KO x 70.45 (845.40) +
# 4 PG x 156.75 (627.00) = 2,568.80 on 2,500.00 allocated (+2.75%); high
# 2,590.00, drawdown 0.82%. Its value still moves until the positions close.
_CLOSING_STOCK = dict(
    slug="quality-dividend-compounders",
    allocated=2500.00,
    high=2590.00,
    limit=0.15,
    cash=1096.40,
    positions=[("KO", 12, 68.10, 70.45), ("PG", 4, 151.20, 156.75)],  # symbol, qty, entry, mark
)

# The closing tail's strategy: its sample tail page shows the "Closing" notice.
CLOSING_TAIL_SLUG = _CLOSING_STOCK["slug"]


def closed_crypto_book() -> dict:
    """The stopped crypto tail's book: equity before the close, the sale, the
    fee, and the cash it ends with (flat, so that is its whole value)."""
    c = _CLOSED_CRYPTO
    pre_close = c["cash_before_close"] + sum(q * m for _, q, m in c["marks"])
    proceeds = sum(q * b for _, q, b in c["bids"])
    fee = round(proceeds * c["fee_rate"], 2)
    cash = round(c["cash_before_close"] + proceeds - fee, 2)
    return {
        "pre_close": round(pre_close, 2),
        "proceeds": round(proceeds, 2),
        "fee": fee,
        "cash": cash,
    }


def closing_stock_book() -> dict:
    """The closing stock tail's book at the last close: cash plus positions."""
    s = _CLOSING_STOCK
    rows = [
        {
            "symbol": sym,
            "kind": "equity",
            "side": "long",
            "qty": str(qty),
            "entry": entry,
            "mark": mark,
            "mark_source": "close",
            "value": round(qty * mark, 2),
            "unrealized": round(qty * (mark - entry), 2),
        }
        for sym, qty, entry, mark in s["positions"]
    ]
    equity = round(s["cash"] + sum(r["value"] for r in rows), 2)
    return {"cash": s["cash"], "positions": rows, "equity": equity}


def _stopped_allocations() -> list[dict]:
    """The two stopped tails as rows shaped like pages.portfolio_view's
    allocations (every key is a real one; a test pins that), so portfolio.html
    renders them through the same branches as real data."""
    crypto, stock = _CLOSED_CRYPTO, _CLOSING_STOCK
    c_strat, s_strat = _by_slug(crypto["slug"]), _by_slug(stock["slug"])
    c_value = closed_crypto_book()["cash"]  # flat: equity is just cash
    book = closing_stock_book()
    s_value = book["equity"]
    return [
        dict(
            slug=crypto["slug"],
            name=c_strat["name"],
            handle=c_strat["handle"],
            venue=_venue_label(_venue(c_strat)),
            allocated=crypto["allocated"],
            value=c_value,
            ret=c_value / crypto["allocated"] - 1,
            dd=1 - c_value / crypto["high"],  # past the 10% limit; the close cost a bit more
            limit=crypto["limit"],
            days=52,
            status="stopped",
            stop_reason="drawdown_limit",
            closing=False,
            positions=[],
            copy_lag="2.1 min",
        ),
        dict(
            slug=stock["slug"],
            name=s_strat["name"],
            handle=s_strat["handle"],
            venue=_venue_label(_venue(s_strat)),
            allocated=stock["allocated"],
            value=s_value,
            ret=s_value / stock["allocated"] - 1,
            dd=1 - s_value / stock["high"],
            limit=stock["limit"],
            days=61,
            status="stopped",
            stop_reason="user",
            closing=True,
            positions=book["positions"],
            copy_lag="3.4 min",
        ),
    ]


def portfolio() -> dict:
    """A sample paper portfolio: six paper tails across Kalshi, Kraken,
    Coinbase and Alpaca (four active, one crypto tail stopped by its drawdown
    limit and closed, one stock tail stopped by the user and still closing).
    Costs are what the paper copies paid the venue (fees, and the spread
    against the creator's price), never a Koine fee."""
    allocs = [
        ("weather-cpi-mean-reversion", 2000, 2318.40, 0.159, 0.021, 34, "1.8 min"),
        ("election-polling-arbitrage", 3000, 3187.20, 0.062, 0.008, 41, "2.4 min"),
        ("fed-path-both-directions", 2500, 2756.50, 0.103, 0.034, 46, "1.6 min"),
        ("btc-eth-weekly-trend", 2000, 2161.80, 0.081, 0.047, 29, "38 s"),
    ]
    rows = [
        dict(
            slug=slug,
            name=_by_slug(slug)["name"],
            handle=_by_slug(slug)["handle"],
            venue=_venue_label(_venue(_by_slug(slug))),
            allocated=alloc,
            value=value,
            ret=ret,
            dd=dd,
            limit=0.15,
            days=days,
            status="active",
            stop_reason=None,
            closing=False,
            copy_lag=lag,
        )
        for slug, alloc, value, ret, dd, days, lag in allocs
    ] + _stopped_allocations()
    # 10,423.90 (active) + 1,428.12 (closed) + 2,568.80 (closing) = 14,420.82
    # on 13,500.00 allocated: +920.82.
    value = round(sum(r["value"] for r in rows), 2)
    allocated = sum(r["allocated"] for r in rows)
    pnl = round(value - allocated, 2)
    fees = round(61.84 + closed_crypto_book()["fee"], 2)
    curve = _walk(19, 90, 0.0011, 0.007, start=8800.0)
    curve = [v * (value / curve[-1]) for v in curve]
    dates = _dates(90, AS_OF - timedelta(days=89))
    return {
        "demo": True,
        "value": value,
        "pnl": pnl,
        "pnl_pct": pnl / allocated,
        "current_dd": 0.021,
        "worst_dd": 0.068,
        "trading_costs": fees,
        "accounts": len(rows),
        "curve": polyline(curve, 900, 200),
        "curve_lo": min(curve),
        "curve_hi": max(curve),
        "x_labels": [dates[i].strftime("%b %-d") for i in (0, 30, 60, 89)],
        "allocations": rows,
        "activity": [
            ("Today 02:14", "Valued", "5 paper tails marked at the latest quotes", None),
            (
                "Aug 31",
                "Paper fill",
                "BTC/ETH trend: buy 0.0052 BTC @ 63,412.50 Kraken ask, 41 s after the creator",
                None,
            ),
            ("Aug 31", "Venue fee", "Kraken taker fee on that fill, 0.80%", -2.64),
            ("Aug 30", "Paper fill", "Fed path: NO on Sept cut ×120 @ 0.31", -37.20),
            ("Aug 29", "Settled", "Weather & CPI: NYC high above 91°F settled NO", 41.20),
            ("Aug 28", "Paper allocation", "Added $500 of paper money to Fed path", None),
            (
                "Aug 27",
                "Paper fill",
                "Large-cap alt rotation (stopped): sell 0.2 ETH/USD @ 3,290.00 Coinbase bid",
                None,
            ),
            (
                "Aug 27",
                "Paper fill",
                "Large-cap alt rotation (stopped): sell 4 SOL/USD @ 165.50 Coinbase bid",
                None,
            ),
            ("Aug 27", "Venue fee", "Coinbase taker fee on the stop close, 0.90%", -11.88),
        ],
        "risk": [
            ("Portfolio Sharpe, 90d", "1.92"),
            ("Current drawdown", "−2.1%"),
            ("Worst drawdown", "−6.8%"),
            ("Strategy correlation, avg", "0.18"),
            ("Largest allocation", f"{max(r['allocated'] for r in rows) / allocated:.0%}"),
            ("Median copy lag", "1.7 min"),
            ("Signals skipped", "2 (stale)"),
            ("Simulated venue fees", f"${fees:,.2f}"),
        ],
    }


def transparency() -> dict:
    return {
        "demo": True,
        "ever": 25,
        "losing": 7,
        "median_return": 0.064,
        "median_dd": 0.128,
        "removed": 3,
        "histogram": [
            ("< −20%", 1),
            ("−20 to −10", 2),
            ("−10 to 0", 4),
            ("0 to 10", 6),
            ("10 to 20", 5),
            ("20 to 30", 3),
            ("30 to 40", 3),
            ("> 40%", 1),
        ],
        "outcomes": [
            ("Paper tails in profit", 61),
            ("Paper tails stopped by a drawdown limit", 9),
            ("Paper tails closed by the follower", 22),
            ("Crypto and stock paper fills priced at a live quote", 94),
        ],
        "failed": [
            (
                "Momentum on NFL spreads",
                "Delisted · Jul 2026",
                "−34.2% · worst drawdown −41%",
                "Creator disconnected after 62 days. Record frozen and kept.",
            ),
            (
                "Overnight CPI straddle",
                "Quarantined · Aug 2026",
                "diverged $212.40 on Aug 19",
                "Venue balance and ledger disagreed. Under review; page hidden.",
            ),
            (
                "Daily temperature spreads",
                "Live, losing",
                "−8.1% · worst drawdown −17.0%",
                "Still listed. Losing strategies are not removed.",
            ),
        ],
    }


# ---- demo: Run by Koine sample (copy strategies) ----
# One sample house strategy: Koine's own account, so the page says "Run by
# Koine" and never links to a creator profile (there is none: `_CREATORS` has
# no entry, so /u/koine-lab is a 404). The description is Koine's own text,
# labelled unverified like every other description. Invented numbers (invariant 7).
_STRATEGIES.append(
    dict(
        slug="koine-lab-funding-carry",
        name="Koine Lab \u00b7 Funding carry",
        handle="Koine Lab",
        operator="koine",
        venue="hyperliquid",
        tags=["crypto", "perps"],
        seed=29,
        drift=0.0013,
        vol=0.011,
        days=97,
        start=20000.0,
        total_return=0.083,
        max_drawdown=0.052,
        sharpe=1.58,
        sortino=2.02,
        risk_score=4,
        n_fills=412,
        paper_tails=22,
        score=6.8,
        description="Koine runs this one in a Koine-owned account. Hold BTC spot-equivalent exposure and short the perp when funding is positive, so the position earns funding rather than price moves. Gross leverage stays under 1.5x.",
        worst_periods=[
            (
                "Jul 14 to Jul 22",
                -0.052,
                "10 days",
                "Funding flipped negative for a week while the hedge was still on.",
            ),
        ],
        holdings=[
            ("USDC", "cash", 1.0, None, None),
            ("BTC-PERP", "perp", -0.55, 63405.0, "62,310.0"),
        ],
        signals=[
            ("BTC-PERP", "2026-08-31T00:00:14", "sell", "0.060", "63,371.0", 0.12),
            ("BTC-PERP", "2026-08-25T08:00:09", "buy", "0.030", "62,880.5", 0.06),
            ("BTC-PERP", "2026-08-19T00:00:21", "sell", "0.045", "61,455.0", 0.09),
        ],
    )
)


# ---- demo: watchlist sample ----
# The static demo's private watchlist (watch_pages.py serves it to a visitor
# when DEMO_MODE is on and nobody is logged in). The addresses are invented:
# well-formed, obviously fake, never anyone's real address. Only the first has
# a full page (/watch/sample-1, built from the Hyperliquid sample record); the
# rest are list rows. Adding an address is off in the demo.
WATCH_BASE_SLUG = "funding-aware-btc-perps"
_WATCH_SAMPLES = [
    dict(n=1, address="0x5a3c000000000000000000000000000000c0ffee", label="Funding-carry desk"),
    dict(
        n=2,
        address="0x7be1000000000000000000000000000000d15ea5",
        label="Leaderboard BTC swing",
        seed=61,
        days=71,
        total_return=0.094,
        drift=0.0011,
        vol=0.02,
    ),
    dict(
        n=3,
        address="0x9d04000000000000000000000000000000facade",
        label="",
        seed=62,
        days=38,
        total_return=-0.031,
        drift=-0.0004,
        vol=0.024,
    ),
]


def _watch_short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def watch_list() -> list[dict]:
    """The sample /watch rows, same keys as `watch_pages.watch_row`."""
    rows = []
    for w in _WATCH_SAMPLES:
        address = w["address"]
        if w["n"] == 1:
            base = _row(_by_slug(WATCH_BASE_SLUG))
            stats = {
                k: base[k] for k in ("track_days", "incubating", "total_return", "max_drawdown")
            }
            stats.update(spark=base["spark"], spark_dd=base["spark_dd"])
        else:
            curve = _walk(w["seed"], w["days"], w["drift"], w["vol"], start=100.0)
            k = (1 + w["total_return"]) * 100.0 / curve[-1]
            n = len(curve)
            curve = [100.0 + (v - 100.0) * ((k - 1) * i / (n - 1) + 1) for i, v in enumerate(curve)]
            window = curve[-_STRIP_SPARK:]
            stats = {
                "track_days": w["days"],
                "incubating": w["days"] < 90,
                "total_return": w["total_return"],
                "max_drawdown": max(_drawdown(curve)),
                "spark": polyline(window, 120, 32),
                "spark_dd": window_x(window, 120),
            }
        rows.append(
            {
                "id": "sample-1" if w["n"] == 1 else None,  # only the first has a page
                "name": w["label"] or _watch_short(address),
                "label": w["label"],
                "address": address,
                "short_address": _watch_short(address),
                "venue_label": _venue_label("hyperliquid"),
                "tailing": w["n"] == 1,
                "stats": stats,
            }
        )
    return rows


# ---- demo: discover sample ----
# /watch/discover for a visitor in the static demo: invented leaderboard rows
# for both watchable venues (prod opens only Hyperliquid; the demo shows the
# full capability). The rows are built through `discover.snapshot_row` so the
# money path is the real one. The first sample watch address shows as being
# watched, one row as a verified Koine strategy; Add is disabled in the template.
_DISCOVER_SUFFIXES = [
    "c0ffee",
    "d15ea5",
    "facade",
    "decade",
    "feeded",
    "b00b1e",
    "ba5eba",
    "bedded",
    "f1a7ed",
    "dec0de",
    "accede",
    "ab1e5e",
    "5eeded",
    "c0def0",
    "d0decc",
]
_DISCOVER_SCALE = {"day": 0.012, "week": 0.06, "month": 0.22, "all": 1.0}
_DISCOVER_ROWS = 15


def _discover_address(venue: str, i: int) -> str:
    prefix = f"{(0x2000 + i * 0x0B3D + (7 if venue == 'polymarket' else 0)) % 0x10000:04x}"
    return "0x" + prefix + "0" * 30 + _DISCOVER_SUFFIXES[i % len(_DISCOVER_SUFFIXES)]


def discover_sample(venue: str | None, period: str | None, sort: str):
    """The sample `discover.DiscoverView` (same bad-param fallbacks as the real one)."""
    from alphamarket import discover
    from alphamarket.connectors.base import LeaderboardDetails, LeaderboardEntry

    venues = ("hyperliquid", "polymarket")
    periods = tuple(_DISCOVER_SCALE)
    venue = venue if venue in venues else venues[0]
    period = period if period in periods else periods[0]
    columns = discover.columns_for(venue)
    sort = sort if sort in discover.SORTS and sort in ("pnl", *columns) else "pnl"
    rng = random.Random(f"discover-{venue}-{period}")
    base = _DISCOVER_SCALE[period] * (1.0 if venue == "hyperliquid" else 0.4)
    stored = []
    for i in range(_DISCOVER_ROWS):
        pnl = round(2_400_000 * base * (0.86**i) * rng.uniform(0.85, 1.15), 2)
        value = round(rng.uniform(0.4, 6.0) * 1_000_000, 2)
        roi = round(pnl / value * rng.uniform(0.5, 1.5), 4)
        volume = f"{round(value * rng.uniform(3, 40), 2):.2f}"
        if venue == "polymarket":  # pnl only on the board; the rest are wallet stats
            row = discover.snapshot_row(
                LeaderboardEntry(address=_discover_address(venue, i), rank=i + 1, pnl=f"{pnl:.2f}")
            )
            # Lifetime figures scale with an all-time pnl, not the period's: a
            # biggest win under one day's pnl, or volume 2,000x pnl, reads wrong.
            lifetime = 2_400_000 * 0.4 * (0.86**i) * rng.uniform(0.85, 1.15)
            discover.merge_details(
                row,
                LeaderboardDetails(
                    volume=f"{round(abs(pnl) * rng.uniform(8, 40), 2):.2f}",
                    positions_value=f"{round(lifetime * rng.uniform(0.05, 0.6), 2):.2f}",
                    markets_traded=rng.randint(40, 1800),
                    biggest_win=f"{round(lifetime * rng.uniform(0.08, 0.35), 2):.2f}",
                    active_since=int(
                        datetime(2023, 1, 1, tzinfo=UTC).timestamp()
                        + rng.randint(0, 3 * 365) * 86400
                    ),
                ),
            )
            stored.append(row)
            continue
        stored.append(
            discover.snapshot_row(
                LeaderboardEntry(
                    address=_discover_address(venue, i),
                    rank=i + 1,
                    pnl=f"{pnl:.2f}",
                    roi=f"{roi:.4f}",
                    volume=volume,
                    account_value=f"{value:.2f}",
                )
            )
        )
    stored.sort(key=lambda r: r["pnl_micros"], reverse=True)  # the venue ranks by PnL
    for i, r in enumerate(stored):
        r["rank"] = i + 1
    if sort == "roi":
        stored.sort(key=lambda r: Decimal(r["roi"]), reverse=True)
    watched = _WATCH_SAMPLES[0]["address"]
    rows = []
    for r in stored:
        state, slug = discover.RowState.ADD, None
        if venue == "hyperliquid" and r["rank"] == 3:
            r = {**r, "address": watched}
            state = discover.RowState.WATCHING
        elif venue == "hyperliquid" and r["rank"] == 6:
            state, slug = discover.RowState.LISTED, WATCH_BASE_SLUG
        rows.append(
            discover.DiscoverRow(
                venue=venue,
                address=r["address"],
                short=_watch_short(r["address"]),
                rank=r["rank"],
                pnl_micros=r["pnl_micros"],
                roi=float(r["roi"]) if r["roi"] is not None else None,
                volume_micros=r["volume_micros"],
                account_value_micros=r["account_value_micros"],
                positions_value_micros=r.get("positions_value_micros"),
                markets_traded=r.get("markets_traded"),
                biggest_win_micros=r.get("biggest_win_micros"),
                active_since=r.get("active_since"),
                state=state,
                watch_strategy_id=1 if state == discover.RowState.WATCHING else None,
                listed_slug=slug,
            )
        )
    return discover.DiscoverView(
        venue=venue,
        period=period,
        sort=sort,
        venues=venues,
        periods=periods,
        fetched_at=datetime.now(UTC) - timedelta(minutes=23),
        rows=rows,
        watch_count=len(_WATCH_SAMPLES),
        watch_max=get_settings().watch_max_per_user,
        columns=columns,
        labels=discover.labels_for(venue),
        note=PROFILES[venue].leaderboard_note,
    )


# The opt-out walkthrough (watch_pages, DEMO_MODE only): the same sample
# address as /watch/sample-1, an invented code and invented counts. Nothing is
# computed from a request and nothing is stored.
OPTOUT_SAMPLE = dict(
    venue="hyperliquid",
    address=_WATCH_SAMPLES[0]["address"],
    code="koine-optout-demo-sample",
    watches_removed=3,
    tails_stopped=1,
)


def watch_sample_page(sample_id: str) -> dict | None:
    """The one full sample watch page, the shape `watch_pages._watch_page`
    returns. Its tail card says "active": the sample is being paper-tailed."""
    if sample_id != "sample-1":
        return None
    w = _WATCH_SAMPLES[0]
    page = strategy_page(WATCH_BASE_SLUG)
    page.update(
        display_name=w["label"],
        label=w["label"],
        address=w["address"],
        short_address=_watch_short(w["address"]),
        watch_id=sample_id,
        sample=True,
        description_unverified="",
        discussion=[],
        creator=None,
    )
    page["verification"] = {
        **(page["verification"] or {}),
        "proof": "Public address, no key held; trades read from the venue's public history",
    }
    page["tailing"] = {**page["tailing"], "state": "active", "href": "/portfolio"}
    return page


# "How this trader trades" for /watch/sample-1. The fills below are invented
# (BTC and ETH perps, the last five are the sample's recent trades above); every
# metric on the page is computed from them by the real trader_profile functions,
# so the sample cannot drift from the code. Equity is the sample's own curve.
# (iso time UTC, instrument, action, quantity, price, order id)
_WATCH_SAMPLE_FILLS = [
    ("2026-06-02T00:00:14", "BTC-PERP", "buy", "0.250", "61240.0", "s01"),
    ("2026-06-02T00:00:15", "BTC-PERP", "buy", "0.070", "61242.5", "s01"),
    ("2026-06-04T08:00:31", "BTC-PERP", "sell", "0.320", "62105.0", "s02"),
    ("2026-06-04T08:01:02", "ETH-PERP", "sell", "3.5", "2512.0", "s03"),
    ("2026-06-09T16:00:20", "ETH-PERP", "buy", "3.5", "2448.5", "s04"),
    ("2026-06-11T00:00:12", "BTC-PERP", "buy", "0.280", "60870.0", "s05"),
    ("2026-06-12T13:42:07", "BTC-PERP", "buy", "0.080", "61455.0", "s06"),
    ("2026-06-12T20:15:40", "BTC-PERP", "sell", "0.120", "62310.0", "s07"),
    ("2026-06-16T16:00:44", "ETH-PERP", "sell", "4.2", "2590.0", "s09"),
    ("2026-06-19T08:00:09", "BTC-PERP", "sell", "0.240", "63020.0", "s08"),
    ("2026-06-22T00:00:27", "ETH-PERP", "buy", "4.2", "2611.0", "s10"),
    ("2026-06-24T00:00:18", "BTC-PERP", "buy", "0.300", "62880.0", "s11"),
    ("2026-06-24T00:00:19", "BTC-PERP", "buy", "0.040", "62881.0", "s11"),
    ("2026-06-24T15:20:33", "BTC-PERP", "sell", "0.340", "62415.0", "s12"),
    ("2026-06-30T14:05:11", "BTC-PERP", "buy", "0.220", "61900.0", "s13"),
    ("2026-06-30T14:48:52", "BTC-PERP", "sell", "0.220", "62150.0", "s14"),
    ("2026-06-26T15:02:30", "ETH-PERP", "sell", "2.0", "2598.0", "s31"),
    ("2026-06-26T15:04:12", "ETH-PERP", "buy", "2.0", "2594.0", "s32"),
    ("2026-07-03T08:00:36", "ETH-PERP", "sell", "3.0", "2705.0", "s15"),
    ("2026-07-07T00:00:22", "BTC-PERP", "buy", "0.300", "64100.0", "s17"),
    ("2026-07-14T08:01:15", "ETH-PERP", "buy", "3.0", "2622.0", "s16"),
    ("2026-07-20T16:00:29", "ETH-PERP", "sell", "3.6", "2640.0", "s19"),
    ("2026-07-27T00:00:13", "BTC-PERP", "sell", "0.300", "66310.0", "s18"),
    ("2026-07-29T16:00:07", "ETH-PERP", "buy", "6.0", "2598.0", "s20"),
    ("2026-07-30T14:12:08", "BTC-PERP", "buy", "0.200", "65880.0", "s33"),
    ("2026-07-30T14:15:19", "BTC-PERP", "sell", "0.200", "65945.0", "s34"),
    ("2026-08-02T08:00:51", "ETH-PERP", "sell", "2.4", "2660.0", "s21"),
    ("2026-08-04T13:30:44", "BTC-PERP", "buy", "0.250", "61120.0", "s22"),
    ("2026-08-05T00:00:26", "BTC-PERP", "buy", "0.150", "60540.0", "s23"),
    ("2026-08-06T16:00:39", "ETH-PERP", "sell", "4.0", "2702.4", "s25"),
    ("2026-08-13T08:00:17", "BTC-PERP", "sell", "0.400", "62010.0", "s24"),
    ("2026-08-27T08:51:36", "BTC-PERP", "buy", "0.140", "61210.0", "s26"),
    ("2026-08-28T16:22:10", "ETH-PERP", "buy", "0.8", "2688.1", "s27"),
    ("2026-08-30T11:05:47", "BTC-PERP", "sell", "0.060", "63980.0", "s28"),
    ("2026-08-31T21:40:33", "ETH-PERP", "sell", "1.2", "2651.8", "s29"),
    ("2026-09-01T03:14:09", "BTC-PERP", "buy", "0.085", "63402.0", "s30"),
]
_SAMPLE_QTY_DECIMALS = 5


def watch_sample_profile() -> tp.TraderProfile:
    """The sample address's profile: the real `trader_profile.build_profile`
    over `_WATCH_SAMPLE_FILLS`, the sample equity curve, and one invented
    funding payment a day (mostly paid, as a net-long perp book would)."""
    fills = []
    for when, instrument, action, qty, price, order in _WATCH_SAMPLE_FILLS:
        atoms = int(Decimal(qty).scaleb(_SAMPLE_QTY_DECIMALS))
        notional = int(Decimal(qty) * Decimal(price) * 1_000_000)
        fills.append(
            tp.FillIn(
                at=datetime.fromisoformat(when).replace(tzinfo=UTC),
                instrument=instrument,
                kind=InstrumentType.PERP,
                action=action,
                qty_atoms=atoms,
                qty_decimals=_SAMPLE_QTY_DECIMALS,
                price=price,
                notional_micros=notional,
                order_id=order,
            )
        )
    dates, curve = _series(_by_slug(WATCH_BASE_SLUG))
    equity = [tp.EquityIn(d, round(v * 1e6)) for d, v in zip(dates, curve, strict=True)]
    rng = random.Random(23)
    first, last = fills[0].at.date(), fills[-1].at.date()
    funding = [
        tp.FundingIn(
            datetime.combine(first + timedelta(days=i), datetime.min.time(), tzinfo=UTC),
            round(rng.gauss(-2.6, 3.5) * 1e6),
        )
        for i in range((last - first).days + 1)
    ]
    return tp.build_profile(
        fills,
        equity=equity,
        funding=funding,
        copy_delay_seconds=get_settings().ingest_interval_seconds,
    )
