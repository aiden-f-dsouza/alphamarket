"""Asset strategy statistics (analytics_v2) and the exact close (PriceIn.close).

Every expected value is worked out by hand in the comments (USD micros;
1 USD = M). Methodology: docs/methodology.md "Asset strategy statistics"."""

import math
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction

import pytest
from sqlalchemy import select

from alphamarket import analytics, analytics_v2, prices, stats
from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.models import (
    AnchorBalance,
    EquitySnapshot,
    Instrument,
    LedgerEntry,
    PriceDaily,
    Strategy,
    StrategyStats,
    User,
    VenueAccount,
)
from alphamarket.money import to_atoms
from alphamarket.valuation import (
    AnchorIn,
    EquityPoint,
    LegIn,
    PerpPosition,
    PriceIn,
    Qty,
    daily_equity_series,
    time_weighted_return,
    unrealized_pnl_micros,
    valuation_on,
)

M = 1_000_000
D1 = date(2026, 9, 1)
D2, D3, D4, D5 = (D1 + timedelta(days=n) for n in range(1, 5))


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


# ---- the plan's exit series, pure ------------------------------------------------------
#
# E: 10,000 -> 10,500 -> 13,605 (3,000 deposited that day) -> 12,244.5
# r_2 = 10,500 / 10,000 - 1                  = +5%
# r_3 = (13,605 - 3,000) / 10,500 - 1        = +1%
# r_4 = 12,244.5 / 13,605 - 1                = -10%
# TWR = 1.05 x 1.01 x 0.90 - 1 = 0.95445 - 1 = -4.555%   (naive 12,244.5 / 10,000 - 1 = +22.445%)
# index: 1, 1.05, 1.0605, 0.95445; drawdown from the 1.0605 peak = 0.10605 / 1.0605 = 10%

EXIT_SERIES = [
    EquityPoint(D1, 10_000 * M),
    EquityPoint(D2, 10_500 * M),
    EquityPoint(D3, 13_605 * M, 3_000 * M),
    EquityPoint(D4, 12_244_500_000),
]


def test_twr_index_and_drawdown_of_the_exit_series():
    index = analytics_v2.twr_index(EXIT_SERIES)
    assert index == [
        (D1, Fraction(1)),
        (D2, Fraction(105, 100)),
        (D3, Fraction(10605, 10000)),
        (D4, Fraction(95445, 100000)),
    ]
    assert index[-1][1] - 1 == time_weighted_return(EXIT_SERIES) == Fraction(-4555, 100000)
    assert analytics_v2.index_drawdown(index) == Fraction(1, 10)
    assert analytics_v2.index_returns(EXIT_SERIES) == [0.05, 0.01, -0.1]


def test_index_skips_days_with_no_capital_at_risk():
    # 0 -> 100 (deposit) -> 110: day 2 has no prior capital (undefined, index stays 1),
    # day 3 is +10%.
    series = [EquityPoint(D1, 0), EquityPoint(D2, 100 * M, 100 * M), EquityPoint(D3, 110 * M)]
    assert [level for _, level in analytics_v2.twr_index(series)] == [1, 1, Fraction(11, 10)]
    assert analytics_v2.index_returns(series) == [0.1]


def test_equity_tolerance_is_one_dollar_or_half_a_percent():
    assert analytics_v2.equity_tolerance_micros(10_040 * M) == 50_200_000  # 0.5% of 10,040
    assert analytics_v2.equity_tolerance_micros(150 * M) == 1 * M  # 0.75 -> the $1 floor
    assert analytics_v2.equity_tolerance_micros(-10_000 * M) == 50 * M


def test_fraction_str_truncates_for_display():
    assert analytics_v2.fraction_str(Fraction(60_000)) == "60000"
    assert analytics_v2.fraction_str(Fraction(32, 3)) == "10.666666666666"
    assert analytics_v2.fraction_str(Fraction(-1, 8)) == "-0.125"


# ---- D14: the exact close ----------------------------------------------------------------


def test_a_sub_cent_coin_keeps_its_exact_close():
    # 1,000,000,000 PEPE (0 dp) at $0.00001234 = $12,340 exactly. The integer
    # micros alone (12 per unit, truncated from 12.34) would say $12,000.
    anchor = [AnchorIn("PEPE", 1_000_000_000, 0)]
    exact = valuation_on(anchor, [], [PriceIn("PEPE", D1, 12, close="0.00001234")], [], D1)
    assert exact.equity_micros == 12_340 * M
    [position] = exact.positions
    assert position.close_micros == 12  # display: truncated toward zero
    assert position.value_micros == position.notional_micros == 12_340 * M
    rounded = valuation_on(anchor, [], [PriceIn("PEPE", D1, 12)], [], D1)
    assert rounded.equity_micros == 12_000 * M


def test_exact_close_flows_into_daily_series_and_perps():
    # Long 1,000 SHIB-PERP from $0.000010 (0 dp); close $0.0000125 = 12.5 micros:
    # unrealized = 1,000 x (12.5 - 10) = 2,500 micros exactly (12 would give 2,000).
    position = PerpPosition("SHIB-PERP", Qty(1_000, 0), Fraction(1, 100_000))
    assert unrealized_pnl_micros(position, Fraction(25, 2)) == 2_500
    anchor = [
        AnchorIn("USDC", 100 * M, 6),
        AnchorIn("SHIB-PERP", 1_000, 0, entry_price="0.000010"),
    ]
    [day] = daily_equity_series(
        anchor, [], [PriceIn("SHIB-PERP", D1, 12, close="0.0000125")], [], [D1]
    )
    assert day.equity_micros == 100 * M + 2_500


def test_close_must_agree_with_the_integer_micros():
    with pytest.raises(ValueError, match="disagrees"):
        PriceIn("PEPE", D1, 13, close="0.00001234")
    with pytest.raises(ValueError):
        PriceIn("PEPE", D1, 0, close="-0.0000001")
    assert PriceIn("PEPE", D1, 12) == PriceIn("PEPE", D1, 12)  # close defaults to None


# ---- DB fixtures ---------------------------------------------------------------------------


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


def _account(db, venue: str, anchor_at: datetime, **anchor) -> tuple[VenueAccount, Strategy]:
    user = User(email=f"{venue}@example.com", password_hash="x", display_name=venue)
    db.add(user)
    db.flush()
    acct = VenueAccount(user_id=user.id, venue=venue, venue_env="prod", key_id="k")
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=acct.id, name=venue, slug=venue)
    db.add(strategy)
    for asset, spec in anchor.items():
        atoms, decimals, *entry = spec
        db.add(
            AnchorBalance(
                venue_account_id=acct.id,
                asset=asset.replace("_", "-"),
                atoms=atoms,
                decimals=decimals,
                entry_price=entry[0] if entry else None,
                as_of=anchor_at,
            )
        )
    db.commit()
    return acct, strategy


def _closes(db, venue: str, symbol: str, base: str, type_, closes: dict[date, str]) -> Instrument:
    inst = Instrument(
        venue=venue, symbol=symbol, type=type_, base_asset=base, quote_asset="USD", qty_decimals=8
    )
    db.add(inst)
    db.flush()
    for day, close in closes.items():
        micros = int(Fraction(close) * M)
        db.add(
            PriceDaily(
                instrument_id=inst.id,
                day=day,
                close=close,
                usd_micros_per_unit=micros,
                source=venue,
            )
        )
    db.commit()
    return inst


def _leg(db, acct, asset, atoms, decimals, kind, when, ref):
    db.add(
        LedgerEntry(
            venue_account_id=acct.id,
            asset=asset,
            delta_atoms=atoms,
            decimals=decimals,
            kind=kind,
            venue_ref=ref,
            group_ref=f"event:{kind}:{ref}",
            occurred_at=when,
        )
    )
    db.commit()


@pytest.fixture()
def exit_case(db):
    """The exit series from a real Kraken ledger: 0.2 BTC anchored on D1 10:00,
    BTC closes 50,000 / 52,500 / 53,025 / 46,222.5, a $3,000 deposit on D3.
    E_1 = 0.2 x 50,000 = 10,000;  E_2 = 0.2 x 52,500 = 10,500;
    E_3 = 0.2 x 53,025 + 3,000 = 13,605;  E_4 = 0.2 x 46,222.5 + 3,000 = 12,244.5."""
    acct, strategy = _account(db, "kraken", at(D1, 10), BTC=(to_atoms("0.2", 10), 10))
    _closes(
        db,
        "kraken",
        "XXBTZUSD",
        "BTC",
        InstrumentType.SPOT,
        {D1: "50000", D2: "52500", D3: "53025", D4: "46222.5", D5: "40000"},
    )
    _leg(db, acct, "USD", to_atoms("3000", 4), 4, LegKind.DEPOSIT, at(D3, 15), "dep1")
    return acct, strategy


def test_exit_series_from_the_ledger(db, exit_case):
    _, strategy = exit_case
    # as_of D5 06:00: the last complete UTC day is D4 (D5's close is not used).
    row = analytics_v2.compute_asset_stats(db, strategy, as_of=at(D5, 6))
    x = row.extras
    assert x["valuation"] == "v2"
    assert x["daily_equity"] == [
        [D1.isoformat(), 10_000 * M],
        [D2.isoformat(), 10_500 * M],
        [D3.isoformat(), 13_605 * M],
        [D4.isoformat(), 12_244_500_000],
    ]
    assert [f for _, f in x["daily_flows"]] == [0, 0, 3_000 * M, 0]
    assert [v for _, v in x["twr_index"]] == [1.0, 1.05, 1.0605, 0.95445]
    assert row.total_return == x["twr"] == float(Fraction(-4555, 100000))  # -4.555%
    naive = 12_244.5 / 10_000 - 1  # +22.445%, what the raw curve would claim
    assert naive == pytest.approx(0.22445) and row.total_return < 0
    assert row.max_drawdown == 0.1
    assert row.track_days == 4 and row.win_rate is None and row.n_settlements == 0
    assert row.equity_micros == 12_244_500_000
    assert x["net_flows_micros"] == 3_000 * M
    assert x["pnl_micros"] == 12_244_500_000 - 10_000 * M - 3_000 * M == -755_500_000
    # MD = (12,244.5 - 10,000 - 3,000) / (10,000 + 1/3 x 3,000) = -755.5 / 11,000
    assert x["modified_dietz"] == float(Fraction(-7555, 110000))
    # Sharpe/Sortino on r = [0.05, 0.01, -0.10]: mean -0.04/3; sample stdev
    # sqrt(((0.19)^2 + (0.07)^2 + (0.26)^2) / 9 / 2) = sqrt(0.1086/18);
    # downside sqrt(0.01 / 3).
    mean = -0.04 / 3
    assert row.sharpe == pytest.approx(mean / math.sqrt(0.1086 / 18) * math.sqrt(365))
    assert row.sharpe == pytest.approx(-3.2795, abs=1e-4)
    assert row.sortino == pytest.approx(mean / math.sqrt(0.01 / 3) * math.sqrt(365))
    assert row.sortino == pytest.approx(-4.4121, abs=1e-4)
    # Risk: drawdown 0.1, downside dev sqrt(0.01/3) x sqrt(365) = 1.103 (capped),
    # top share 1.0 (BTC is all the exposure), no stake term.
    downside = stats.downside_deviation_annual([0.05, 0.01, -0.1])
    assert downside == pytest.approx(1.1030, abs=1e-4)
    assert row.risk_score == stats.risk_score(0.1, downside, 1.0, 0.0)
    assert x["holdings"] == [
        {
            "asset": "BTC",
            "kind": "spot",
            "qty": "0.2000000000",
            "close_micros": 46_222_500_000,
            "price_day": D4.isoformat(),
            "value_micros": 9_244_500_000,
            "notional_micros": 9_244_500_000,
            "entry": None,
        },
        {
            "asset": "USD",
            "kind": "cash",
            "qty": "3000.0000",
            "close_micros": M,
            "price_day": None,
            "value_micros": 3_000 * M,
            "notional_micros": 3_000 * M,
            "entry": None,
        },
    ]
    assert x["exposure"] == {
        "day": D4.isoformat(),
        "equity_micros": 12_244_500_000,
        "gross_micros": 9_244_500_000,
        "by_underlying_micros": {"BTC": 9_244_500_000},
        "top_asset": "BTC",
        "top_share": 1.0,
        "perp_gross_leverage": 0.0,
    }
    assert row.flags == ["single_asset_concentration"]
    assert x["valuation_flags"] == {
        "unpriced": [],
        "carried_forward_days": 0,
        "perp_entry_unknown": [],
        "perp_qty_mismatch": [],
    }
    assert x["asset_classes"] == ["crypto"] and x["price_sources"] == ["kraken"]
    assert x["broker_equity"] is None
    # No fresh mark (prices.latest_mark is None): marked equity = holdings now at
    # the last close of a COMPLETE day, D4's 46,222.5: 0.2 x 46,222.5 + 3,000.
    # D5's stored 40,000 is not known at D5 06:00 and is never looked ahead to.
    assert x["marked_equity_micros"] == 12_244_500_000
    assert x["n_open_positions"] == 1
    assert db.scalars(select(StrategyStats)).one().id == row.id


def test_dispatch_never_reaches_the_binary_path(db, exit_case, monkeypatch):
    _, strategy = exit_case

    def boom(*a, **k):
        raise AssertionError("binary stats must not run for an asset venue")

    monkeypatch.setattr(analytics, "compute_binary_stats", boom)
    row = analytics.compute_strategy_stats(db, strategy, as_of=at(D5, 6))
    assert row.extras["valuation"] == "v2"


def test_marked_equity_prefers_a_fresh_mark(db, exit_case, monkeypatch):
    acct, strategy = exit_case
    btc = db.scalars(select(Instrument).where(Instrument.base_asset == "BTC")).one()
    seen = []

    def mark(db_, instrument_id, now):
        seen.append((instrument_id, now))
        return prices.Mark(instrument_id, "47000.5", now, "kraken", "mid")

    monkeypatch.setattr(prices, "latest_mark", mark)
    row = analytics_v2.compute_asset_stats(db, strategy, as_of=at(D5, 6))
    # 0.2 x 47,000.5 + 3,000 = 12,400.1; the daily stats still use closes.
    assert row.extras["marked_equity_micros"] == 12_400_100_000
    assert row.equity_micros == 12_244_500_000
    assert seen == [(btc.id, at(D5, 6))]


def test_connected_today_has_no_returns_yet(db, exit_case):
    _, strategy = exit_case
    btc = db.scalars(select(Instrument).where(Instrument.base_asset == "BTC")).one()
    d0 = D1 - timedelta(days=1)
    db.add(
        PriceDaily(
            instrument_id=btc.id, day=d0, close="49000", usd_micros_per_unit=49_000 * M, source="x"
        )
    )
    db.commit()
    row = analytics_v2.compute_asset_stats(db, strategy, as_of=at(D1, 20))
    assert (row.track_days, row.total_return, row.max_drawdown, row.sharpe) == (0, None, 0.0, None)
    assert row.extras["daily_equity"] == [] and row.extras["twr_index"] == []
    # Holdings still show, valued at the last COMPLETE day's close (D0's 49,000;
    # D1's 50,000 is not known at D1 20:00): 0.2 x 49,000.
    assert row.equity_micros == 9_800 * M
    assert row.extras["holdings"][0]["value_micros"] == 9_800 * M
    assert row.extras["holdings"][0]["price_day"] == d0.isoformat()
    # Carrying D0's close into today is expected, not stale: no flag.
    assert row.extras["valuation_flags"]["carried_forward_days"] == 1
    assert row.flags == ["single_asset_concentration"]


def test_realized_pnl_prices_each_leg_and_truncates_once():
    # USDC +10.5 (at par) + BTC -0.00000001 at D1's 50,000.5 (-500.005 micros)
    # + ETH 0.000001 on D2, carried from D1's 3,000.0000005 (+3,000.0000005 micros)
    # + a XYZ leg with no close (left out); funding is not realized.
    # Sum = 10,500,000 - 500.005 + 3,000.0000005 = 10,502,499.9950005 -> 10,502,499.
    t1, t2 = at(D1), at(D2)
    legs = [
        LegIn("USDC", 10_500_000, 6, LegKind.REALIZED_PNL, t1),
        LegIn("BTC", -1, 8, LegKind.LIQUIDATION, t1),
        LegIn("ETH", 1, 6, LegKind.REALIZED_PNL, t2),
        LegIn("XYZ", 5, 0, LegKind.REALIZED_PNL, t2),
        LegIn("USDC", -7 * M, 6, LegKind.FUNDING, t2),
    ]
    closes = [
        PriceIn("BTC", D1, 50_000_500_000, close="50000.5"),
        PriceIn("ETH", D1, 3_000_000_000, close="3000.0000005"),
    ]
    assert analytics_v2.realized_pnl_micros(legs, closes) == 10_502_499
    # A net loss truncates toward zero: -500.005 -> -500.
    assert analytics_v2.realized_pnl_micros(legs[1:2], closes) == -500
    assert analytics_v2.realized_pnl_micros([], closes) == 0


def test_unpriced_and_carried_forward_flags(db):
    # 10 SOL + 1,000 XYZ + $50. SOL closes D1 150, D3 160 (D2 missing, carried
    # forward); XYZ has no close at all (unpriced, left out of equity).
    # E: D1 1,550; D2 1,550 (carried); D3 1,650.
    acct, strategy = _account(
        db, "kraken", at(D1, 1), SOL=(10 * 10**8, 8), XYZ=(1_000, 0), USD=(50 * M, 6)
    )
    _closes(db, "kraken", "SOLUSD", "SOL", InstrumentType.SPOT, {D1: "150", D3: "160"})
    row = analytics_v2.compute_asset_stats(db, strategy, as_of=at(D4, 0))
    assert [e for _, e in row.extras["daily_equity"]] == [1_550 * M, 1_550 * M, 1_650 * M]
    assert row.extras["valuation_flags"]["unpriced"] == ["XYZ"]
    assert row.extras["valuation_flags"]["carried_forward_days"] == 1
    assert row.flags == ["unpriced_assets", "carried_forward_prices", "single_asset_concentration"]
    assert row.extras["n_open_positions"] == 2  # SOL priced, XYZ held but unpriced


def test_an_equity_venue_tolerates_weekend_gaps(db):
    # Alpaca: AAPL closes Fri 2026-09-04 and Mon 2026-09-07 only. Sat and Sun
    # carry Friday's close (counted) but within the 4-day allowance (not flagged).
    fri = date(2026, 9, 4)
    mon = fri + timedelta(days=3)
    acct, strategy = _account(db, "alpaca", at(fri, 14), AAPL=(10 * 10**9, 9), USD=(500 * M, 6))
    _closes(db, "alpaca", "AAPL", "AAPL", InstrumentType.EQUITY, {fri: "200", mon: "210"})
    # E: Fri 500 + 10 x 200 = 2,500; Sat, Sun 2,500 (Friday's close); Mon 2,600.
    row = analytics_v2.compute_asset_stats(db, strategy, as_of=at(mon + timedelta(days=1), 0))
    equity = [e for _, e in row.extras["daily_equity"]]
    assert equity == [2_500 * M, 2_500 * M, 2_500 * M, 2_600 * M]
    assert row.extras["valuation_flags"]["carried_forward_days"] == 2
    assert "carried_forward_prices" not in row.flags
    assert row.extras["holdings"][0]["kind"] == "equity"
    assert row.extras["asset_classes"] == ["equities"]


def test_perp_leverage_realized_pnl_and_broker_flag(db):
    # Hyperliquid: $1,000 USDC margin, long 0.1 BTC-PERP from 60,000 (anchor).
    # BTC closes D1 60,000, D2 61,000. Legs on D2: realized +50, liquidation
    # -20, funding -3 (funding is not realized P&L).
    # E_1 = 1,000 + 0.1 x 0 = 1,000;  E_2 = 1,027 + 0.1 x 1,000 = 1,127.
    # Perp gross leverage: D1 6,000 / 1,000 = 6x (> 5x: high_leverage);
    # D2 6,100 / 1,127 = 5.41x.
    acct, strategy = _account(
        db,
        "hyperliquid",
        at(D1, 1),
        USDC=(1_000 * M, 6),
        BTC_PERP=(to_atoms("0.1", 5), 5, "60000"),
    )
    _closes(db, "hyperliquid", "BTC", "BTC", InstrumentType.PERP, {D1: "60000", D2: "61000"})
    _leg(db, acct, "USDC", 50 * M, 6, LegKind.REALIZED_PNL, at(D2, 9), "r1")
    _leg(db, acct, "USDC", -20 * M, 6, LegKind.LIQUIDATION, at(D2, 10), "l1")
    _leg(db, acct, "USDC", -3 * M, 6, LegKind.FUNDING, at(D2, 11), "f1")
    db.add(
        EquitySnapshot(
            strategy_id=strategy.id,
            as_of=at(D2, 23),
            venue_balance_micros=0,
            computed_cash_micros=0,
            divergence_micros=0,
            tolerance_micros=0,
            reconciled=True,
            venue_equity_micros=1_140 * M,
            computed_equity_micros=1_127 * M,
            equity_tolerance_micros=5_700_000,
            equity_reconciled=False,
        )
    )
    db.commit()
    row = analytics_v2.compute_asset_stats(db, strategy, as_of=at(D3, 0))
    assert [e for _, e in row.extras["daily_equity"]] == [1_000 * M, 1_127 * M]
    assert row.realized_pnl_micros == 30 * M
    assert row.extras["max_perp_gross_leverage"] == 6.0
    assert row.extras["exposure"]["perp_gross_leverage"] == pytest.approx(6_100 / 1_127)
    perp = next(h for h in row.extras["holdings"] if h["kind"] == "perp")
    assert (perp["asset"], perp["qty"], perp["entry"], perp["value_micros"]) == (
        "BTC-PERP",
        "0.10000",
        "60000",
        100 * M,
    )
    assert perp["notional_micros"] == 6_100 * M
    assert row.extras["broker_equity"] == {
        "as_of": at(D2, 23).isoformat(),
        "venue_micros": 1_140 * M,
        "computed_micros": 1_127 * M,
        "divergence_micros": 13 * M,
        "tolerance_micros": 5_700_000,
        "ok": False,
    }
    assert row.flags == ["single_asset_concentration", "high_leverage", "broker_equity_divergence"]
    assert db.get(Strategy, strategy.id).status == "incubating"  # flagged, never quarantined


def test_an_unanchored_account_is_refused(db):
    user = User(email="u@example.com", password_hash="x", display_name="U")
    db.add(user)
    db.flush()
    acct = VenueAccount(user_id=user.id, venue="kraken", venue_env="prod", key_id="k")
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=acct.id, name="U", slug="u")
    db.add(strategy)
    db.commit()
    with pytest.raises(ValueError, match="no anchored venue account"):
        analytics_v2.compute_asset_stats(db, strategy, as_of=at(D2))
