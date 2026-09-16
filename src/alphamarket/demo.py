# ruff: noqa: E501  (sample-data prose; wrapping it would only hurt readability)
"""Sample data for the demo build (DEMO_MODE=true).

Everything here is invented: names, handles, markets, curves and every number.
It exists so the accelerator can see the whole product (marketplace, tailing,
portfolio, feed) before those parts can be computed from venue data. Every
page that draws on this module renders a "Sample data" banner, `pages.py`
returns 404 for these routes when demo mode is off, and `scripts/preflight.py`
refuses to launch with it on. Invariant #5 (public pages show only
platform-computed numbers) holds for real users because the demo dataset
never reaches a deployment that passes preflight.

Shapes mirror `public.page_data` / `public.list_visible` so the same templates
render real and sample pages; sample pages carry extra keys (benchmarks,
capacity, worst periods, tailing) that real pages simply omit.
"""

import random
from datetime import date, timedelta

from alphamarket.charts import polyline, window_x

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
        tailing_k=62,
        capacity_k=240,
        investors=31,
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
        tailing_k=118,
        capacity_k=320,
        investors=54,
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
        tailing_k=41,
        capacity_k=180,
        investors=23,
        score=7.1,
        profitable_months="4 of 5",
        worst_month=-0.031,
        largest_loss=-212.40,
        concentration=0.22,
        description="Fade extreme prints on weather and CPI contracts in the last 48 hours before settlement, when retail flow pushes YES past what the underlying forecast supports. Flat sizing, never adding to losers. Capacity is limited by liquidity in the daily temperature series.",
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
        tailing_k=12,
        capacity_k=90,
        investors=9,
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
        tailing_k=4,
        capacity_k=60,
        investors=3,
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
        tailing_k=0,
        capacity_k=45,
        investors=0,
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
}

BENCHMARK_NOTE = (
    "S&P 500 and T-bill lines are sample benchmarks; the live benchmark set is an open question."
)
FEE_TEXT = "20% of profit above your high-water mark"

_STRIP_SPARK = 60


def _series(s: dict) -> tuple[list[date], list[float]]:
    n = s["days"]
    curve = _walk(s["seed"], n, s["drift"], s["vol"], start=6600.0)
    # Pin the end so the headline return matches the curve exactly.
    target = 6600.0 * (1 + s["total_return"])
    k = target / curve[-1]
    curve = [6600.0 + (v - 6600.0) * ((k - 1) * i / (n - 1) + 1) for i, v in enumerate(curve)]
    return _dates(n, AS_OF - timedelta(days=n - 1)), curve


def _row(s: dict) -> dict:
    dates, curve = _series(s)
    return {
        "slug": s["slug"],
        "name": s["name"],
        "display_name": s["handle"],
        "tags": s["tags"],
        "incubating": s["days"] < 90,
        "total_return": s["total_return"],
        "max_drawdown": s["max_drawdown"],
        "risk_score": s["risk_score"],
        "track_days": s["days"],
        "reconciled": True,
        "spark": polyline(curve[-_STRIP_SPARK:], 120, 32),
        "spark_dd": window_x(curve[-_STRIP_SPARK:], 120),
        "sharpe": s["sharpe"],
        "tailing_k": s["tailing_k"],
        "capacity_k": s["capacity_k"],
        "investors": s["investors"],
        "score": s["score"],
        "demo": True,
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


def strategy_page(slug: str) -> dict | None:
    s = _by_slug(slug)
    if s is None:
        return None
    dates, curve = _series(s)
    dd = _drawdown(curve)
    n = len(curve)
    spx = _walk(4, n, 0.00055, 0.009, start=curve[0])
    cash = [curve[0] * (1 + 0.000115) ** i for i in range(n)]
    c = _CREATORS[s["handle"]]
    mine = [x for x in _STRATEGIES if x["handle"] == s["handle"]]
    settlements = [
        ("KXHIGHDEN-26AUG30-T95", "no", 60, 0.0, "2026-08-31"),
        ("KXJOBS-26AUG-150", "no", 150, 150.0, "2026-08-29"),
        ("KXFED-26SEP-25", "yes", 200, 200.0, "2026-08-28"),
        ("KXHIGHNY-26AUG22-T91", "yes", 80, 0.0, "2026-08-23"),
        ("KXCPI-26JUL-0.3", "no", 120, 120.0, "2026-08-12"),
    ]
    return {
        "demo": True,
        "slug": s["slug"],
        "name": s["name"],
        "display_name": s["handle"],
        "venue": "kalshi",
        "venue_env": "prod",
        "status": "live",
        "style_tags": s["tags"],
        "description_unverified": s["description"],
        "verified_since": dates[0].isoformat(),
        "last_reconciled": AS_OF.isoformat() + "T02:14:00+00:00",
        "reconciled": True,
        "incubating": s["days"] < 90,
        "stats": {
            "as_of": AS_OF.isoformat(),
            "track_days": s["days"],
            "n_fills": s["n_fills"],
            "n_settlements": s["n_settlements"],
            "total_return": s["total_return"],
            "max_drawdown": s["max_drawdown"],
            "sharpe": s["sharpe"],
            "sortino": s["sortino"],
            "win_rate": s["win_rate"],
            "risk_score": s["risk_score"],
            "equity": curve[-1],
            "realized_pnl": curve[-1] - curve[0],
            "flags": [],
            "top_series_share": s["concentration"],
            "equity_curve": [[d.isoformat(), v] for d, v in zip(dates, curve, strict=True)],
            "drawdown_curve": [[d.isoformat(), x] for d, x in zip(dates, dd, strict=True)],
            "monthly_returns": _monthly(dates, curve),
        },
        "recent_settlements": [
            {
                "ticker": t,
                "result": side if pay else ("no" if side == "yes" else "yes"),
                "side": side,
                "count": float(count),
                "payout": pay,
                "settled_at": when,
            }
            for t, side, count, pay, when in settlements
        ],
        "benchmarks": [("S&P 500", spx, False), ("Cash, T-bill", cash, True)],
        "benchmark_note": BENCHMARK_NOTE,
        "vs_benchmark": s["total_return"] - (spx[-1] / spx[0] - 1),
        "worst_periods": s["worst_periods"],
        "risk_rows": [
            ("Sharpe (annualised)", f"{s['sharpe']:.2f}"),
            ("Sortino", f"{s['sortino']:.2f}"),
            ("Worst month", f"{s['worst_month'] * 100:+.1f}%"),
            ("Profitable months", s["profitable_months"]),
            ("Win rate", f"{s['win_rate'] * 100:.0f}%"),
            ("Largest single loss", f"${s['largest_loss']:,.2f}"),
            ("Series concentration", f"{s['concentration'] * 100:.0f}% in one series"),
            ("Settled trades", str(s["n_settlements"])),
        ],
        "capacity": {
            "tailing_k": s["tailing_k"],
            "capacity_k": s["capacity_k"],
            "investors": s["investors"],
            "left_k": s["capacity_k"] - s["tailing_k"],
            "min_usd": 500,
            "fee": FEE_TEXT,
        },
        "creator": {"handle": s["handle"], "followers": c["followers"], "strategies": len(mine)},
        "discussion": [
            (
                "isobar",
                "2h",
                False,
                "July drawdown was the Phoenix heat series. Has sizing changed since?",
            ),
            (
                s["handle"],
                "1h",
                True,
                "Cap per series is now 15% of book. Visible in the concentration number.",
            ),
        ],
    }


def tail_page(slug: str) -> dict | None:
    page = strategy_page(slug)
    if page is None:
        return None
    return {
        "demo": True,
        "strategy": page,
        "account": {"venue": "Kalshi", "last4": "4821", "balance": 12400.00, "connected": "Aug 14"},
        "allocation": 2000.00,
        "allocation_pct": 16,
        "min_usd": 500,
        "dd_limit": 0.15,
        "stop_at": 1700.00,
        "fee_rows": [
            ("Performance fee", FEE_TEXT),
            ("High-water mark", "$2,000.00, your starting allocation"),
            ("Charged", "Monthly, only on new highs"),
            (
                "Example",
                "Allocation grows to $2,300: fee is $60. Falls back to $2,100 next month: no fee until it passes $2,300 again.",
            ),
            ("Subscriptions or other fees", "None"),
        ],
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
        "strategies": mine,
        "best": best,
        "worst": worst,
        "worst_dd": max(r["max_drawdown"] for r in mine),
        "tailing_k": sum(r["tailing_k"] for r in mine),
        "investors": sum(r["investors"] for r in mine),
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
                "Is the reconciliation panel showing computed vs. venue balance in the creator's account, or ours?",
            ),
            (
                "koine",
                "10:16",
                "The creator's. Your own account's reconciliation is on your Portfolio page.",
            ),
            (
                "tailrisk",
                "10:20",
                "Would love a filter for strategies with no correlation to my current three.",
            ),
            (
                "median_voter",
                "10:31",
                "Correlation column would help creators too. Investors keep asking me.",
            ),
        ],
    }


def portfolio() -> dict:
    curve = _walk(19, 90, 0.0011, 0.007, start=7500.0)
    curve = [v * (8262.10 / curve[-1]) for v in curve]
    dates = _dates(90, AS_OF - timedelta(days=89))
    allocs = [
        (
            "weather-cpi-mean-reversion",
            "Weather & CPI mean reversion",
            "quietedge",
            2000,
            2318.40,
            0.159,
            0.021,
            34,
        ),
        (
            "election-polling-arbitrage",
            "Election polling arbitrage",
            "median_voter",
            3000,
            3187.20,
            0.062,
            0.008,
            41,
        ),
        (
            "fed-path-both-directions",
            "Fed path, both directions",
            "rateswatch",
            2500,
            2756.50,
            0.103,
            0.034,
            46,
        ),
    ]
    return {
        "demo": True,
        "value": 8262.10,
        "pnl": 762.10,
        "pnl_pct": 0.102,
        "current_dd": 0.021,
        "worst_dd": 0.068,
        "fees_paid": 118.40,
        "accounts": 3,
        "curve": polyline(curve, 900, 200),
        "curve_lo": min(curve),
        "curve_hi": max(curve),
        "x_labels": [dates[i].strftime("%b %-d") for i in (0, 30, 60, 89)],
        "allocations": [
            dict(slug=a, name=b, handle=c, allocated=d, value=e, ret=f, dd=g, days=h)
            for a, b, c, d, e, f, g, h in allocs
        ],
        "activity": [
            ("Today 02:14", "Reconciled", "3 accounts matched venue balances", None),
            ("Yesterday", "Mirrored", "Fed path: NO on Sept cut ×120 @ 0.31", -37.20),
            ("Aug 31", "Fee", "Election polling arbitrage, new high-water mark $3,187.20", -18.40),
            ("Aug 29", "Mirrored", "Weather & CPI: settled NYC high above 91°F, NO", 41.20),
            ("Aug 28", "Allocation", "Added $500 to Fed path, both directions", None),
        ],
        "risk": [
            ("Portfolio Sharpe, 90d", "1.92"),
            ("Current drawdown", "−2.1%"),
            ("Worst drawdown", "−6.8%"),
            ("Strategy correlation, avg", "0.18"),
            ("Largest allocation", "38%"),
            ("Fees paid to date", "$118.40"),
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
            ("Positions in profit", 61),
            ("Positions stopped by a drawdown limit", 9),
            ("Positions closed by the investor", 22),
            ("Median fee paid, as share of profit", 20),
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
