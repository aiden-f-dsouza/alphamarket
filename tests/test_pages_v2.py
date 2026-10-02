"""Wave 2 page surface (w2-p8-pages): asset strategy pages (extras
"valuation": "v2"), marketplace venue/asset filters, the real /portfolio,
the dashboard's account status and the public signals of both shapes.

Rows are seeded through the ORM, never through POST /venues. Expected values
are worked out in comments (USD micros; M = 1_000_000)."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import account_status, activity_flags, pages, public
from alphamarket.charts import polyline
from alphamarket.config import get_settings
from alphamarket.connectors.registry import PROFILES
from alphamarket.jobs import JobKind
from alphamarket.models import (
    AccountSchedule,
    Allocation,
    AssetFill,
    EquitySnapshot,
    Fill,
    Instrument,
    Job,
    JobStatus,
    MirrorAssetFill,
    MirrorFill,
    MirrorSettlement,
    Signal,
    Strategy,
    StrategyStats,
    User,
    VenueAccount,
    VenueAccountStatus,
    VenueActivityFlag,
)

M = 1_000_000
NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
DAYS = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]


# ---- seeding --------------------------------------------------------------------------------


def _user(db, email, name="Creator") -> User:
    user = User(email=email, password_hash="x", display_name=name)
    db.add(user)
    db.flush()
    return user


def _strategy(db, venue, slug, name=None, claimed=True, user=None, status="live") -> Strategy:
    user = user or _user(db, f"{slug}@example.com")
    account = VenueAccount(
        user_id=user.id,
        venue=venue,
        venue_env="prod",
        key_id="k",
        status=VenueAccountStatus.ACTIVE,
        anchor_balance_micros=10_000 * M,
        anchor_at=NOW - timedelta(days=120),
        claimed_at=NOW - timedelta(days=120) if claimed else None,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=user.id,
        venue_account_id=account.id,
        name=name or slug,
        slug=slug,
        status=status,
    )
    db.add(strategy)
    db.flush()
    return strategy


def v2_extras(**overrides) -> dict:
    """Plan numbers: equity 10,000 -> 10,500 -> 13,605 (a 3,000 deposit at the
    start of day 3) -> 12,244.5. Day returns +5%, (13,605 - 3,000)/10,500 =
    +1%, -10%: index 1.0, 1.05, 1.0605, 0.95445, so TWR = -4.555% and the
    worst drawdown 10% from 1.0605. Raw equity says +22.4%."""
    extras = {
        "valuation": "v2",
        "daily_equity": [
            [DAYS[0], 10_000 * M],
            [DAYS[1], 10_500 * M],
            [DAYS[2], 13_605 * M],
            [DAYS[3], 12_244_500_000],
        ],
        "daily_flows": [[DAYS[2], 3_000 * M]],
        "twr_index": [[DAYS[0], 1.0], [DAYS[1], 1.05], [DAYS[2], 1.0605], [DAYS[3], 0.95445]],
        "twr": -0.04555,
        # Last-close book: $5,000 cash + 1.5 ETH x 3,000 (4,500) + a 0.05 BTC
        # perp, entry 60,000, mark 62,000 (unrealized 100, notional 3,100).
        # Value sum = 9,600; ETH share 4,500 / 9,600 = 46.875%.
        "holdings": [
            {
                "asset": "USD",
                "kind": "cash",
                "qty": "5000.000000",
                "close_micros": M,
                "price_day": None,
                "value_micros": 5_000 * M,
                "notional_micros": 5_000 * M,
                "entry": None,
            },
            {
                "asset": "ETH",
                "kind": "spot",
                "qty": "1.5",
                "close_micros": 3_000 * M,
                "price_day": DAYS[3],
                "value_micros": 4_500 * M,
                "notional_micros": 4_500 * M,
                "entry": None,
            },
            {
                "asset": "BTC-PERP",
                "kind": "perp",
                "qty": "0.05",
                "close_micros": 62_000 * M,
                "price_day": DAYS[3],
                "value_micros": 100 * M,
                "notional_micros": 3_100 * M,
                "entry": "60000",
            },
        ],
        # gross 4,500 + 3,100 = 7,600; ETH 59.2%; perp leverage 3,100 / 9,600 = 0.3229
        "exposure": {
            "day": DAYS[3],
            "equity_micros": 9_600 * M,
            "gross_micros": 7_600 * M,
            "by_underlying_micros": {"ETH": 4_500 * M, "BTC": 3_100 * M},
            "top_asset": "ETH",
            "top_share": "0.592105",
            "perp_gross_leverage": 0.3229,
        },
        "asset_classes": ["crypto"],
        "price_sources": ["kraken"],
        "valuation_flags": {
            "unpriced": [],
            "carried_forward_days": 0,
            "perp_entry_unknown": [],
            "perp_qty_mismatch": [],
        },
        "broker_equity": None,
        "marked_equity_micros": 9_600 * M,
        "n_open_positions": 2,
    }
    extras.update(overrides)
    return extras


def _stats(db, strategy, extras, total_return=-0.04555, track_days=100, **kw) -> StrategyStats:
    row = StrategyStats(
        strategy_id=strategy.id,
        as_of=NOW,
        track_days=track_days,
        n_fills=12,
        n_settlements=0,
        total_return=total_return,
        max_drawdown=0.1,
        sharpe=0.8,
        sortino=1.1,
        win_rate=None,
        risk_score=4,
        equity_micros=9_600 * M,
        realized_pnl_micros=250 * M,
        flags=[],
        extras=extras,
    )
    for k, v in kw.items():
        setattr(row, k, v)
    db.add(row)
    db.flush()
    return row


def binary_extras() -> dict:
    return {
        "daily_equity": [[DAYS[0], 1_000 * M], [DAYS[1], 1_100 * M]],
        "top_series_share": 0.5,
        "peak_invested_micros": 0,
    }


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


# ---- strategy page: asset (v2) ---------------------------------------------------------------


def test_v2_page_shows_holdings_exposure_and_twr_not_settled_trades(client, db):
    s = _strategy(db, "kraken", "eth-swing", "ETH Swing")
    _stats(db, s, v2_extras())
    db.commit()

    html = client.get("/s/eth-swing").text
    assert "Holdings and exposure" in html
    assert "BTC-PERP" in html and "entry 60000" in html
    assert "$100.00" in html and "unrealized" in html  # perp: unrealized P&L, not a holding
    assert "$4,500.00" in html and "47%" in html  # ETH value and 46.875% share
    assert "$7,600.00" in html and "59%" in html and "0.32×" in html  # exposure panel
    assert "Return, time-weighted" in html
    # binary-only copy is gone
    for text in ("Settled trades", "Recent settlements", "Series concentration"):
        assert text not in html, text
    assert "Prediction markets" not in html.split("<footer")[0]
    assert ">Crypto<" in html  # venue-aware crumb
    assert "Open positions" in html and "Recent trades" in html
    assert "<polyline" in html and "chart.js" not in html.lower()


def test_v2_drawdown_and_months_come_from_the_twr_index_not_raw_equity(client, db):
    s = _strategy(db, "kraken", "eth-swing", "ETH Swing")
    _stats(db, s, v2_extras())
    db.commit()

    data = client.get("/api/strategies/eth-swing").json()
    st = data["stats"]
    # drawdown of the index: peak 1.0605 on day 3, trough 0.95445: exactly 10%,
    # and 0 before the fall (the deposit is not a peak)
    dd = [d for _, d in st["drawdown_curve"]]
    assert dd[:3] == [0.0, 0.0, 0.0]
    assert dd[3] == pytest.approx(0.1, abs=1e-6)
    # one month, measured from the first point: 0.95445 / 1.0 - 1 = -4.555%
    # (raw equity would say 12,244.5 / 10,000 - 1 = +22.4%)
    ((year, month, ret),) = st["monthly_returns"]
    assert (year, month) == (2026, 9)
    assert ret == pytest.approx(-0.04555, abs=1e-6)
    assert st["total_return"] == pytest.approx(-0.04555)
    # the chart plots 10,000 x index, not the raw curve with the deposit in it
    assert [v for _, v in st["chart_curve"]] == [10_000.0, 10_500.0, 10_605.0, 9_544.5]
    assert st["equity_curve"][2][1] == 13_605.0  # the raw, honest equity stays in the JSON
    assert st["valuation"] == "v2"
    assert [h["asset"] for h in st["holdings"]] == ["USD", "ETH", "BTC-PERP"]  # largest first


def test_v2_holdings_view_orders_by_notional_and_weights_non_perps():
    rows = public.holdings_view(v2_extras())
    assert [r["asset"] for r in rows] == ["USD", "ETH", "BTC-PERP"]  # 5,000 > 4,500 > 3,100
    by = {r["asset"]: r for r in rows}
    assert by["ETH"]["weight"] == pytest.approx(4_500 / 9_600)
    assert by["BTC-PERP"]["weight"] is None
    assert by["BTC-PERP"]["value_label"] == "$100.00"
    assert by["BTC-PERP"]["notional_label"] == "$3,100.00"
    # a sub-cent coin keeps six places instead of reading $0.00
    assert public._usd_label(1_234) == "$0.001234"
    assert public._usd_label(-2 * M) == "−$2.00"
    exposure = public.exposure_view(v2_extras())
    assert exposure["top_share"] == pytest.approx(0.592105)  # a string in JSON parses
    assert [e["asset"] for e in exposure["by_underlying"]] == ["ETH", "BTC"]


def test_v2_page_notes_unpriced_assets_and_broker_equity(client, db):
    s = _strategy(db, "ibkr", "etf-core", "ETF Core")
    _stats(
        db,
        s,
        v2_extras(
            valuation_flags={
                "unpriced": ["XYZ"],
                "carried_forward_days": 2,
                "perp_entry_unknown": [],
                "perp_qty_mismatch": [],
            },
            broker_equity={
                "as_of": DAYS[3],
                "venue_micros": 9_600 * M,
                "computed_micros": 9_590 * M,
                "divergence_micros": 10 * M,
                "tolerance_micros": 48 * M,
                "ok": True,
            },
            asset_classes=["equities"],
        ),
    )
    db.commit()
    html = client.get("/s/etf-core").text
    assert "Not valued (no price available): XYZ" in html
    assert "2 days valued at the last available close" in html
    assert "Broker-reported equity $9,600.00 matches the ledger $9,590.00" in html
    assert "daily verified" in html and "end of day only" in html  # IBKR is a daily venue
    assert "Reporting-only token" in html
    assert ">Stocks<" in html


def test_coingecko_attribution_only_when_it_priced_something(client, db):
    s = _strategy(db, "kraken", "with-cg", "With CG")
    _stats(db, s, v2_extras(price_sources=["coingecko", "kraken"]))
    t = _strategy(db, "hyperliquid", "no-cg", "No CG")
    _stats(db, t, v2_extras(price_sources=["hyperliquid"]))
    db.commit()
    assert "CoinGecko" in client.get("/s/with-cg").text
    assert "https://www.coingecko.com/" in client.get("/s/with-cg").text
    assert "CoinGecko" not in client.get("/s/no-cg").text


def test_unsupported_activity_badge_discloses_reasons_not_details(client, db):
    s = _strategy(db, "alpaca", "stocks-a", "Stocks A")
    _stats(db, s, v2_extras(asset_classes=["equities"]))
    account_id = s.venue_account_id
    for i, (reason, symbol) in enumerate([("short_sale", "TSLA"), ("short_sale", "NVDA")]):
        db.add(
            VenueActivityFlag(
                venue_account_id=account_id,
                venue_ref=f"act-{i}",
                kind=activity_flags.KIND_UNSUPPORTED,
                activity_type="FILL",
                reason=reason,
                symbol=symbol,
                occurred_at=NOW,
                description="sold short TSLA 10 @ 250",
            )
        )
    db.commit()
    html = client.get("/s/stocks-a").text
    assert "unsupported activity · 2 skipped" in html
    assert "2 short sale" in html
    assert (
        "TSLA" not in html and "sold short" not in html
    )  # the reason is public, the detail is not
    assert client.get("/api/strategies/stocks-a").json()["activity_flags"]["count"] == 2


def test_binary_page_keeps_its_shape(client, db):
    s = _strategy(db, "kalshi", "kx-fade", "KX Fade")
    _stats(db, s, binary_extras(), total_return=0.1, track_days=100)
    db.commit()
    html = client.get("/s/kx-fade").text
    assert "Settled trades" in html and "Recent settlements" in html
    assert "Series concentration" in html
    assert "Read-only key, scope re-checked every sync" in html
    assert "Holdings and exposure" not in html and "time-weighted" not in html
    assert "Prediction markets" in html
    data = client.get("/api/strategies/kx-fade").json()
    assert "recent_settlements" in data and "recent_signals" not in data["stats"]
    assert "chart_curve" not in data["stats"] and "valuation" not in data["stats"]


@pytest.mark.parametrize(
    ("venue", "tier", "needle"),
    [
        ("kalshi", "reconciled", "Read-only key, scope re-checked every sync"),
        ("kraken", "reconciled", "refused order or withdrawal probe"),
        ("coinbase", "reconciled", "Read-only key, scope re-checked every sync"),
        ("alpaca", "reconciled", "Read-only OAuth grant approved inside the account at Alpaca"),
        ("ibkr", "daily_verified", "Reporting-only token"),
        ("snaptrade", "daily_verified", "no independent read-only proof"),
        ("hyperliquid", "reconciled", "message signed with the wallet"),
        ("polymarket", "fills_only", "Koine code in the Polymarket profile"),
    ],
)
def test_verification_text_per_proof_method(venue, tier, needle):
    view = public.verification_view(PROFILES[venue], tier)
    assert needle in view["proof"]
    assert (view["note"] is not None) == (tier == "daily_verified")
    if tier == "daily_verified":
        assert "end of day" in view["note"]


def test_daily_venues_get_the_daily_verified_tier(db):
    ibkr = _strategy(db, "ibkr", "i1")
    kraken = _strategy(db, "kraken", "k1")
    poly = _strategy(db, "polymarket", "p1")
    unclaimed = _strategy(db, "ibkr", "i2", claimed=False)
    tier = lambda s: public.verification_tier(db.get(VenueAccount, s.venue_account_id))  # noqa: E731
    assert tier(ibkr) == "daily_verified"
    assert tier(kraken) == "reconciled"
    assert tier(poly) == "fills_only"
    assert tier(unclaimed) == "unclaimed"


# ---- marketplace -----------------------------------------------------------------------------


@pytest.fixture()
def market(db):
    """One public strategy per venue class, all past incubation."""
    for venue, slug, name in [
        ("kraken", "k-strat", "Alpha Kraken"),
        ("hyperliquid", "h-strat", "Beta Perps"),
        ("alpaca", "a-strat", "Gamma Equities"),
        ("kalshi", "x-strat", "Delta Binary"),
    ]:
        s = _strategy(db, venue, slug, name)
        extras = binary_extras() if venue == "kalshi" else v2_extras()
        _stats(db, s, extras, total_return=0.05)
    db.commit()


def _names(html: str) -> set[str]:
    return {
        n for n in ("Alpha Kraken", "Beta Perps", "Gamma Equities", "Delta Binary") if n in html
    }


def test_marketplace_venue_filter_excludes_other_venues(client, market):
    assert _names(client.get("/marketplace").text) == {
        "Alpha Kraken",
        "Beta Perps",
        "Gamma Equities",
        "Delta Binary",
    }
    assert _names(client.get("/marketplace?venue=kraken").text) == {"Alpha Kraken"}
    assert _names(client.get("/marketplace?venue=kalshi").text) == {"Delta Binary"}
    assert _names(client.get("/marketplace?venue=nowhere").text) == set()


def test_marketplace_asset_filter_and_view_table(client, market):
    assert _names(client.get("/marketplace?asset=crypto").text) == {"Alpha Kraken", "Beta Perps"}
    assert _names(client.get("/marketplace?asset=equities").text) == {"Gamma Equities"}
    assert _names(client.get("/marketplace?asset=prediction").text) == {"Delta Binary"}
    table = client.get("/marketplace?asset=crypto&view=table").text
    assert _names(table) == {"Alpha Kraken", "Beta Perps"}
    assert "Worst DD" in table and 'class="card scard"' not in table
    both = client.get("/marketplace?asset=crypto&venue=hyperliquid&view=table").text
    assert _names(both) == {"Beta Perps"}


def test_marketplace_threads_venue_and_asset_through_links_and_forms(client, market):
    html = client.get("/marketplace?asset=crypto&venue=kraken&view=table&incubating=1&q=al").text
    # the tabs are live links, not "soon" placeholders
    assert "<i>soon</i>" not in html and "<span aria-disabled" not in html
    assert 'href="/marketplace?asset=equities&amp;incubating=1&amp;q=al&amp;view=table"' in html
    # the search form, the incubating toggle and the view switch all carry the filters
    assert html.count('<input type="hidden" name="asset" value="crypto">') == 2
    assert html.count('<input type="hidden" name="venue" value="kraken">') == 2
    assert re.search(r'href="/marketplace\?[^"]*asset=crypto[^"]*view=cards"', html)
    assert re.search(r'href="/marketplace\?[^"]*venue=kraken[^"]*view=cards"', html)
    # the tag chips keep them too
    assert 'aria-current="page">Crypto</a>' in html


def test_marketplace_rows_show_the_venue_and_daily_tier(client, db):
    s = _strategy(db, "ibkr", "daily-one", "Daily One")
    _stats(db, s, v2_extras(asset_classes=["equities"]), total_return=0.02)
    db.add(
        EquitySnapshot(
            strategy_id=s.id,
            as_of=NOW,
            venue_balance_micros=M,
            computed_cash_micros=M,
            divergence_micros=0,
            tolerance_micros=M,
            reconciled=True,
        )
    )
    db.commit()
    rows = public.list_visible(db, include_incubating=True, asset="equities")
    assert [r["slug"] for r in rows] == ["daily-one"]
    assert rows[0]["verification_tier"] == "daily_verified"
    assert rows[0]["venue_label"] == "Interactive Brokers (Flex)"
    assert rows[0]["asset_classes"] == ["equities"]
    html = client.get("/marketplace?asset=equities").text
    assert "daily verified" in html
    assert "matched · end of day" in client.get("/marketplace?asset=equities&view=table").text


def test_marketplace_sparkline_is_flow_adjusted(db):
    s = _strategy(db, "kraken", "spark", "Spark")
    _stats(db, s, v2_extras(), total_return=-0.04555)
    db.commit()
    (row,) = public.list_visible(db, include_incubating=True)
    # the index, not the raw equity (whose deposit would bend the line up)
    assert row["spark"] == polyline([1.0, 1.05, 1.0605, 0.95445], 120, 32)


def test_marketplace_in_demo_mode_leaves_sample_rows_out_of_other_venues(client, market):
    settings = get_settings()
    settings.demo_mode = True
    try:
        html = client.get("/marketplace?asset=crypto").text
        assert "Sample data" not in html  # no sample row is a crypto row
        assert "Alpha Kraken" in html
        assert "Sample data" in client.get("/marketplace?asset=prediction").text
    finally:
        settings.demo_mode = False


# ---- /portfolio ------------------------------------------------------------------------------


@pytest.fixture()
def viewer(client, session_factory):
    resp = client.post(
        "/signup-form",
        data={"email": "inv@example.com", "password": "hunter2hunter2", "display_name": "Investor"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_factory() as db:
        return db.scalar(select(User).where(User.email == "inv@example.com")).id


def _tail(db, strategy, user_id, amount=1_000 * M) -> Allocation:
    allocation = Allocation(
        user_id=user_id,
        strategy_id=strategy.id,
        allocation_micros=amount,
        drawdown_limit_bps=1500,
        high_water_micros=amount,
        started_at=NOW - timedelta(days=3),
    )
    db.add(allocation)
    db.flush()
    return allocation


PERP_VIEW = {
    "positions": [
        {
            "kind": "perp",
            "symbol": "BTC-PERP",
            "side": "long",
            "qty": "0.01",
            "cost": 0.0,
            "mark": "62500.5",
            "mark_source": "quote",
            "mark_as_of": "2026-09-05T11:59:00+00:00",
            "value": 25.5,
            "unrealized": 25.5,
            "entry": "60000",
        }
    ],
    "venue": "hyperliquid",
    "asset_class": "crypto",
    "copy_lag_median_seconds": 42,
    "skipped": {"below_minimum_size": 2, "stale_signal": 1},
}


def test_portfolio_for_a_logged_in_user_shows_a_perp_with_unrealized_value(
    client, session_factory, viewer, monkeypatch
):
    monkeypatch.setattr("alphamarket.paper.asset_view_extras", lambda db, a, v: dict(PERP_VIEW))
    with session_factory() as db:
        s = _strategy(db, "hyperliquid", "perp-pro", "Perp Pro")
        _tail(db, s, viewer)
        db.commit()
    resp = client.get("/portfolio")
    assert resp.status_code == 200
    html = resp.text
    assert "Sample data" not in html
    assert "Perp Pro" in html and "BTC-PERP" in html
    assert "Entry" in html and "60000" in html and "62500.5" in html
    assert "$25.50" in html and "unrealized" in html
    assert "copy lag 42 s" in html
    assert "2 below minimum size" in html and "1 stale signal" in html
    assert "Paper only" in html
    assert 'href="#"' not in html
    # the JSON the page is built from agrees
    assert client.get("/api/portfolio").json()["allocations"][0]["positions"][0]["symbol"] == (
        "BTC-PERP"
    )


def test_portfolio_activity_lists_paper_fills_and_settlements(client, session_factory, viewer):
    with session_factory() as db:
        s = _strategy(db, "kalshi", "kx-act", "KX Act")
        allocation = _tail(db, s, viewer)
        t = _strategy(db, "hyperliquid", "hl-act", "HL Act")
        asset_alloc = _tail(db, t, viewer)
        inst = Instrument(
            venue="hyperliquid",
            symbol="BTC-PERP",
            type="perp",
            base_asset="BTC-PERP",
            quote_asset="USD",
            qty_decimals=5,
        )
        db.add(inst)
        db.flush()
        db.add(
            MirrorFill(
                allocation_id=allocation.id,
                signal_id=1,
                mode="paper",
                ticker="KXBTC-1",
                side="yes",
                action="buy",
                count_micros=5 * M,
                price_micros=420_000,
                filled_at=NOW,
            )
        )
        db.add(
            MirrorSettlement(
                allocation_id=allocation.id,
                ticker="KXBTC-1",
                market_result="yes",
                yes_count_micros=5 * M,
                revenue_micros=5 * M,
                settled_at=NOW + timedelta(hours=1),
            )
        )
        db.add(
            MirrorAssetFill(
                allocation_id=asset_alloc.id,
                signal_id=None,
                reason="liquidation",
                mode="paper",
                instrument_id=inst.id,
                action="sell",
                qty_atoms=1_000,
                qty_decimals=5,
                price="50500",
                notional_micros=505_000_000,
                price_basis="mark",
                filled_at=NOW + timedelta(hours=2),
            )
        )
        db.commit()
    html = client.get("/portfolio").text
    assert "buy yes ×5 KXBTC-1 @ $0.42" in html
    assert "KXBTC-1 → yes" in html and "+$5.00" in html
    assert "sell 0.01 BTC-PERP @ 50500" in html and "Liquidation" in html


def test_portfolio_empty_state_and_visibility(client, viewer):
    html = client.get("/portfolio").text
    assert "not tailing a strategy yet" in html and "Browse strategies" in html
    client.post("/logout-form")
    assert client.get("/portfolio").status_code == 404  # no account, no demo mode


def test_portfolio_demo_branch_survives_for_visitors(client):
    settings = get_settings()
    settings.demo_mode = True
    try:
        html = client.get("/portfolio").text
        assert "Sample data" in html and "Your tailed strategies" in html
    finally:
        settings.demo_mode = False


def test_portfolio_view_combines_curves_and_ages():
    # A: 1,000 on 09-01, 1,100 on 09-02. B starts 09-02 at 500.
    # Combined: 1,000, then 1,100 + 500 = 1,600; peak 1,600, no drawdown.
    def view(slug, curve, **kw):
        base = {
            "strategy_slug": slug,
            "strategy_name": slug.upper(),
            "status": "active",
            "stop_reason": None,
            "started_at": "2026-09-01T00:00:00+00:00",
            "allocation": 1_000.0,
            "equity": curve[-1][1],
            "return": curve[-1][1] / 1_000.0 - 1,
            "drawdown": 0.0,
            "drawdown_limit": 0.15,
            "positions": [],
            "curve": curve,
        }
        return base | kw

    a = view("a", [["2026-09-01T00:00:00+00:00", 1_000.0], ["2026-09-02T08:00:00+00:00", 1_100.0]])
    b = view(
        "b",
        [["2026-09-02T09:00:00+00:00", 500.0]],
        allocation=500.0,
        copy_lag_median_seconds=7_200,
        skipped={"below_minimum_size": 3},
    )
    assert pages._combined_curve([a, b]) == [("2026-09-01", 1_000.0), ("2026-09-02", 1_600.0)]
    summary = {
        "allocations": [a, b],
        "allocated": 1_500.0,
        "value": 1_600.0,
        "pnl": 100.0,
        "n_active": 2,
    }
    p = pages.portfolio_view(summary, {"a": "Ann"}, now=datetime(2026, 9, 4, tzinfo=UTC))
    assert p["pnl_pct"] == pytest.approx(100 / 1_500)
    assert p["current_dd"] == 0.0 and p["worst_dd"] == 0.0
    assert p["allocations"][0]["handle"] == "Ann" and p["allocations"][0]["days"] == 3
    assert p["allocations"][1]["copy_lag"] == "2.0 h"  # 7,200 s
    assert ("Signals skipped", "3") in p["risk"]
    assert ("Median copy lag", "2.0 h") in p["risk"]
    assert [pages._age(x) for x in (None, 42, 600, 7_200, 200_000)] == [
        "—",
        "42 s",
        "10 min",
        "2.0 h",
        "2.3 d",
    ]


def test_portfolio_view_drawdown_is_measured_on_the_combined_curve():
    # 1,000 -> 1,200 -> 900: current drawdown (1,200 - 900) / 1,200 = 25%
    curve = [
        ["2026-09-01T00:00:00+00:00", 1_000.0],
        ["2026-09-02T00:00:00+00:00", 1_200.0],
        ["2026-09-03T00:00:00+00:00", 900.0],
    ]
    v = {
        "strategy_slug": "s",
        "strategy_name": "S",
        "status": "active",
        "stop_reason": None,
        "started_at": "2026-09-01T00:00:00+00:00",
        "allocation": 1_000.0,
        "equity": 900.0,
        "return": -0.1,
        "drawdown": 0.25,
        "drawdown_limit": 0.15,
        "positions": [{"ticker": "KX-1", "side": "yes", "count": 10.0, "cost": 4.2}],
        "curve": curve,
    }
    p = pages.portfolio_view(
        {"allocations": [v], "allocated": 1_000.0, "value": 900.0, "pnl": -100.0, "n_active": 1},
        {},
        now=datetime(2026, 9, 4, tzinfo=UTC),
    )
    assert p["current_dd"] == pytest.approx(0.25) and p["worst_dd"] == pytest.approx(0.25)
    # a binary position is carried at cost with no mark
    (pos,) = p["positions"]
    assert pos["symbol"] == "KX-1" and pos["mark"] is None and pos["value"] == 4.2


# ---- dashboard -------------------------------------------------------------------------------


def _own_account(db, user_id, venue, status=VenueAccountStatus.ACTIVE, **kw) -> VenueAccount:
    account = VenueAccount(
        user_id=user_id,
        venue=venue,
        venue_env="prod",
        key_id="k",
        status=status,
        claimed_at=NOW,
        **kw,
    )
    db.add(account)
    db.flush()
    return account


def test_dashboard_shows_importing_flags_and_both_actions(client, session_factory, viewer):
    with session_factory() as db:
        hl = _own_account(db, viewer, "hyperliquid", anchor_at=NOW, last_sync_error=None)
        db.add(AccountSchedule(venue_account_id=hl.id, resume_cursors={}))
        db.add(
            Job(
                kind=JobKind.BACKFILL,
                venue_account_id=hl.id,
                dedupe_key=f"backfill:{hl.id}",
                payload={"streams": ["fills"], "cursor": "c", "pages": 7},
                priority=40,
                status=JobStatus.RUNNING,
            )
        )
        db.add(
            VenueActivityFlag(
                venue_account_id=hl.id,
                venue_ref="x1",
                kind=activity_flags.KIND_UNSUPPORTED,
                activity_type="OPT",
                reason="option",
                occurred_at=NOW,
            )
        )
        suspended = _own_account(db, viewer, "kraken", status=VenueAccountStatus.SUSPENDED)
        errored = _own_account(
            db, viewer, "coinbase", status=VenueAccountStatus.ERROR, last_sync_error="HTTP 401"
        )
        pending = _own_account(db, viewer, "snaptrade", status=VenueAccountStatus.PENDING)
        db.commit()
        ids = (hl.id, suspended.id, errored.id, pending.id)
    html = client.get("/dashboard").text
    assert "Importing history" in html and "7 pages so far" in html
    assert "1 unsupported activity" in html and "1 option trade" in html
    # replace key: suspended and errored key accounts; finish connecting: pending
    assert f'href="/dashboard/accounts/{ids[1]}/rotate"' in html
    assert f'href="/dashboard/accounts/{ids[2]}/rotate"' in html
    assert f'href="/dashboard/connect/snaptrade/{ids[3]}"' in html
    assert f'href="/dashboard/accounts/{ids[0]}/rotate"' not in html  # healthy: nothing to replace
    assert "Replace key" in html and "Finish connecting" in html
    assert "connection not finished" in html and "suspended" in html
    assert "HTTP 401" in html
    assert 'href="#"' not in html


def test_dashboard_claim_block_follows_the_ownership_proof(client, session_factory, viewer):
    with session_factory() as db:
        for venue in ("hyperliquid", "polymarket"):
            account = _own_account(db, viewer, venue, claim_code=f"koine-{venue[:4]}")
            account.claimed_at = None
        db.commit()
    html = client.get("/dashboard").text
    assert "Prove this wallet is yours" in html and "koine-hype" in html  # signed message
    assert "Prove this account is yours" in html and "koine-poly" in html  # profile bio code
    assert html.count("/claim") == 1  # only the profile-code venue has the plain claim form


def test_account_status_importing_needs_an_active_backfill_and_no_done_marker(db):
    user = _user(db, "u@example.com")
    acct = _own_account(db, user.id, "hyperliquid")
    assert account_status.account_status(db, acct).importing is False  # no job at all
    job = Job(
        kind=JobKind.BACKFILL,
        venue_account_id=acct.id,
        dedupe_key="b",
        payload={},
        priority=40,
        status=JobStatus.PENDING,
    )
    db.add(job)
    db.flush()
    status = account_status.account_status(db, acct)
    assert status.importing is True and status.pages is None  # queued, no page yet
    job.payload = {"pages": 3}
    db.flush()
    assert account_status.account_status(db, acct).pages == 3
    job.status = JobStatus.DONE
    db.flush()
    assert account_status.account_status(db, acct).importing is False
    job.status = JobStatus.RUNNING
    db.add(AccountSchedule(venue_account_id=acct.id, resume_cursors={}, backfill_done_at=NOW))
    db.flush()
    assert account_status.account_status(db, acct).importing is False  # already backfilled
    acct.status = VenueAccountStatus.SUSPENDED
    db.flush()
    assert account_status.account_status(db, acct).importing is False


def test_account_status_links_follow_status_and_credential_kind(db):
    user = _user(db, "u2@example.com")
    suspended = _own_account(db, user.id, "kraken", status=VenueAccountStatus.SUSPENDED)
    address = _own_account(db, user.id, "hyperliquid", status=VenueAccountStatus.ERROR)
    oauth = _own_account(db, user.id, "alpaca", status=VenueAccountStatus.ERROR)
    pending = _own_account(db, user.id, "snaptrade", status=VenueAccountStatus.PENDING)
    active = _own_account(db, user.id, "kraken")
    st = account_status.account_status
    assert st(db, suspended).rotate_url == f"/dashboard/accounts/{suspended.id}/rotate"
    assert st(db, address).rotate_url is None  # nothing to replace: an address
    assert st(db, oauth).rotate_url is None  # an OAuth grant is renewed by consenting again
    assert st(db, pending).finish_url == f"/dashboard/connect/snaptrade/{pending.id}"
    assert st(db, pending).rotate_url is None
    assert st(db, active).finish_url is None and st(db, active).rotate_url is None


def test_activity_flag_summary_orders_reasons_by_count(db):
    user = _user(db, "u3@example.com")
    acct = _own_account(db, user.id, "alpaca")
    for i, reason in enumerate(["option", "short_sale", "short_sale", "weird_new_reason"]):
        db.add(
            VenueActivityFlag(
                venue_account_id=acct.id,
                venue_ref=f"r{i}",
                kind=activity_flags.KIND_UNSUPPORTED,
                activity_type="X",
                reason=reason,
                occurred_at=NOW + timedelta(hours=i),
            )
        )
    db.flush()
    summary = activity_flags.summary_for(db, acct.id)
    assert summary["count"] == 4
    assert summary["by_reason"] == [
        ("short sale", 2),
        ("option trade", 1),
        ("weird new reason", 1),
    ]
    assert summary["latest"] == (NOW + timedelta(hours=3)).isoformat()
    assert activity_flags.summary_for(db, 10_000) == {"count": 0, "by_reason": [], "latest": None}


# ---- public signals: both shapes -------------------------------------------------------------


def _asset_signal(db, strategy) -> Signal:
    inst = Instrument(
        venue="kraken",
        symbol="ETH/USD",
        type="spot",
        base_asset="ETH",
        quote_asset="USD",
        qty_decimals=8,
    )
    db.add(inst)
    db.flush()
    fill = AssetFill(
        venue_account_id=strategy.venue_account_id,
        instrument_id=inst.id,
        venue_fill_id="f1",
        action="buy",
        qty_atoms=50_000_000,
        qty_decimals=8,
        price="3100.5",
        filled_at=NOW,
    )
    db.add(fill)
    db.flush()
    signal = Signal(
        strategy_id=strategy.id,
        asset_fill_id=fill.id,
        instrument_id=inst.id,
        ticker="ETH/USD",
        side="long",
        action="buy",
        creator_equity_micros=8_000 * M,
        qty_atoms=50_000_000,
        qty_decimals=8,
        price="3100.5",
        notional_micros=1_550_250_000,
        filled_at=NOW,
    )
    db.add(signal)
    db.flush()
    return signal


def test_public_signals_handle_asset_and_binary_rows(client, db):
    asset = _strategy(db, "kraken", "sig-asset", "Sig Asset")
    _asset_signal(db, asset)
    binary = _strategy(db, "kalshi", "sig-binary", "Sig Binary")
    fill = Fill(
        strategy_id=binary.id,
        venue_fill_id="kf1",
        ticker="KXBTC-1",
        side="yes",
        action="buy",
        count_micros=10 * M,
        price_micros=420_000,
        filled_at=NOW,
    )
    db.add(fill)
    db.flush()
    db.add(
        Signal(
            strategy_id=binary.id,
            fill_id=fill.id,
            ticker="KXBTC-1",
            side="yes",
            action="buy",
            count_micros=10 * M,
            price_micros=420_000,
            creator_equity_micros=1_000 * M,
            filled_at=NOW,
        )
    )
    db.commit()

    (a,) = client.get("/api/strategies/sig-asset/signals").json()["signals"]
    # qty 0.5 ETH at 3,100.5 = 1,550.25 notional of 8,000 equity = 19.378%
    assert a["ticker"] == "ETH/USD" and a["side"] == "long" and a["action"] == "buy"
    assert a["qty"] == "0.50000000" and a["count"] == 0.5 and a["price"] == 3100.5
    assert a["stake_fraction"] == pytest.approx(1_550.25 / 8_000)
    (b,) = client.get("/api/strategies/sig-binary/signals").json()["signals"]
    assert b["ticker"] == "KXBTC-1" and b["count"] == 10.0 and b["price"] == 0.42
    assert b["stake_fraction"] == pytest.approx(10 * 0.42 / 1_000)

    # the asset page lists the trade, as a share of equity rather than dollars
    _stats(db, asset, v2_extras())
    db.commit()
    html = client.get("/s/sig-asset").text
    assert "ETH/USD" in html and "0.50000000" in html and "19.4%" in html
