"""Ledger v2 legs, balance vectors and running-balance chains, pinned by
hand-computed values (methodology: "Ledger legs", "Precision by asset")."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    LegKind,
    NormalizedFill,
    NormalizedLeg,
    NormalizedSettlement,
    Side,
)
from alphamarket.ledger import (
    LegGroup,
    balance_chain_breaks,
    balance_vector,
    binary_asset,
    binary_is_tokenised,
    build_cash_curve,
    cash_curve_from_groups,
    fill_cash_flow,
    legacy_build_cash_curve,
    legs_for_fill,
    legs_for_settlement,
    nonzero_legs,
    rescale,
)
from alphamarket.money import to_atoms, to_micros

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def fill(action, side, count, price, fee, minutes=1, ticker="T"):
    return NormalizedFill(
        venue_fill_id=f"f{minutes}{action}{side}",
        ticker=ticker,
        side=side,
        action=action,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        fee_micros=to_micros(fee),
        filled_at=T0 + timedelta(minutes=minutes),
    )


def settlement(yes, no, revenue, minutes=60, ticker="T", result="yes"):
    return NormalizedSettlement(
        venue_settlement_id=f"s{minutes}",
        ticker=ticker,
        market_result=result,
        yes_count_micros=to_micros(yes),
        no_count_micros=to_micros(no),
        revenue_micros=to_micros(revenue),
        settled_at=T0 + timedelta(minutes=minutes),
    )


def as_tuples(legs):
    return [(leg.kind, leg.asset, leg.delta_atoms) for leg in legs]


# ---- binary legs: netting (Kalshi) ---------------------------------------------


def test_kalshi_buy_yes_is_plus_count_minus_notional_minus_fee():
    # 10 x $0.40 = $4.00 notional; fee $0.07.
    legs = legs_for_fill(fill(Action.BUY, Side.YES, "10", "0.40", "0.07"), "kalshi")
    assert as_tuples(legs) == [
        (LegKind.FILL_BASE, "T", 10_000_000),
        (LegKind.FILL_QUOTE, "USD", -4_000_000),
        (LegKind.FEE, "USD", -70_000),
    ]
    assert all(leg.decimals == 6 for leg in legs)


def test_kalshi_buy_no_is_minus_count():
    # The real demo fill: 0.84 NO @ $0.57 = $0.4788; fee $0.0145.
    legs = legs_for_fill(fill(Action.BUY, Side.NO, "0.84", "0.57", "0.0145"), "kalshi")
    assert as_tuples(legs) == [
        (LegKind.FILL_BASE, "T", -840_000),
        (LegKind.FILL_QUOTE, "USD", -478_800),
        (LegKind.FEE, "USD", -14_500),
    ]


def test_kalshi_sells_reverse_the_sign():
    # Sell YES 4 @ $0.55 = +$2.20, fee $0.03; sell NO 2 @ $0.30 = +$0.60, no fee.
    sell_yes = legs_for_fill(fill(Action.SELL, Side.YES, "4", "0.55", "0.03"), "kalshi")
    assert as_tuples(sell_yes) == [
        (LegKind.FILL_BASE, "T", -4_000_000),
        (LegKind.FILL_QUOTE, "USD", 2_200_000),
        (LegKind.FEE, "USD", -30_000),
    ]
    sell_no = legs_for_fill(fill(Action.SELL, Side.NO, "2", "0.30", "0"), "kalshi")
    assert as_tuples(sell_no)[0] == (LegKind.FILL_BASE, "T", 2_000_000)
    # Zero fee leg is produced but never written.
    assert as_tuples(nonzero_legs(sell_no)) == [
        (LegKind.FILL_BASE, "T", 2_000_000),
        (LegKind.FILL_QUOTE, "USD", 600_000),
    ]


def test_cash_legs_equal_the_m2_cash_flow():
    for f in (
        fill(Action.BUY, Side.YES, "101.89", "0.9800", "0.139800"),  # -99.992
        fill(Action.SELL, Side.NO, "4", "0.55", "0.03"),  # +2.17
    ):
        legs = legs_for_fill(f, "kalshi")
        assert sum(leg.delta_atoms for leg in legs if leg.asset == "USD") == fill_cash_flow(f)
    assert fill_cash_flow(fill(Action.BUY, Side.YES, "101.89", "0.9800", "0.139800")) == to_micros(
        "-99.992"
    )


def test_kalshi_settlement_removes_the_signed_position():
    # Demo account: 0.84 NO held, market resolved YES, revenue $0.
    # Position after the buy: -0.84; settlement leg -(0 - 0.84) = +0.84 -> 0.
    buy = legs_for_fill(fill(Action.BUY, Side.NO, "0.84", "0.57", "0.0145"), "kalshi")
    stl = legs_for_settlement(settlement("0", "0.84", "0"), "kalshi")
    assert as_tuples(stl) == [(LegKind.SETTLEMENT, "T", 840_000), (LegKind.SETTLEMENT, "USD", 0)]
    vector = balance_vector(
        nonzero_legs(buy + stl), start=[AssetAmount(asset="USD", atoms=1_101_898_000, decimals=6)]
    )
    # 1101.8980 - 0.4788 - 0.0145 + 0 = 1101.4047; contract position 0.
    assert vector["USD"].atoms == to_micros("1101.4047")
    assert vector["T"].atoms == 0


def test_kalshi_round_trip_vector_hand_computed():
    # anchor $1000; buy 10 YES @0.40 fee 0.07; sell 4 @0.55 fee 0.03; 6 YES win $6.
    # USD: 1000 - 4.07 + 2.17 + 6.00 = 1004.10; T: +10 - 4 - 6 = 0.
    legs = (
        legs_for_fill(fill(Action.BUY, Side.YES, "10", "0.40", "0.07", 1), "kalshi")
        + legs_for_fill(fill(Action.SELL, Side.YES, "4", "0.55", "0.03", 2), "kalshi")
        + legs_for_settlement(settlement("6", "0", "6.00"), "kalshi")
    )
    vector = balance_vector(legs, start=[AssetAmount(asset="USD", atoms=1_000_000_000, decimals=6)])
    assert vector["USD"].atoms == to_micros("1004.10")
    assert vector["T"].atoms == 0


# ---- binary legs: tokenised (Polymarket) ---------------------------------------


def test_polymarket_holds_each_outcome_token_unsigned():
    assert binary_is_tokenised("polymarket") and not binary_is_tokenised("kalshi")
    assert binary_asset("polymarket", "0xc", Side.NO) == "0xc:no"
    assert binary_asset("kalshi", "KX", Side.NO) == "KX"
    buy_no = legs_for_fill(fill(Action.BUY, Side.NO, "5", "0.20", "0", ticker="0xc"), "polymarket")
    assert as_tuples(buy_no)[:2] == [
        (LegKind.FILL_BASE, "0xc:no", 5_000_000),
        (LegKind.FILL_QUOTE, "USD", -1_000_000),
    ]
    sell_yes = legs_for_fill(
        fill(Action.SELL, Side.YES, "3", "0.70", "0.01", ticker="0xc"), "polymarket"
    )
    assert as_tuples(sell_yes)[0] == (LegKind.FILL_BASE, "0xc:yes", -3_000_000)


def test_polymarket_redeem_burns_each_token():
    # REDEEM of 19.607842 losing YES tokens pays nothing: only the burn is written.
    legs = legs_for_settlement(
        settlement("19.607842", "0", "0", ticker="0xd", result="no"), "polymarket"
    )
    assert as_tuples(nonzero_legs(legs)) == [(LegKind.SETTLEMENT, "0xd:yes", -19_607_842)]
    # A winning 0.17 YES redeem pays $0.17.
    win = legs_for_settlement(settlement("0.17", "0", "0.17", ticker="0xe"), "polymarket")
    assert as_tuples(nonzero_legs(win)) == [
        (LegKind.SETTLEMENT, "0xe:yes", -170_000),
        (LegKind.SETTLEMENT, "USD", 170_000),
    ]


# ---- cash curve over legs ------------------------------------------------------


def test_build_cash_curve_is_the_legacy_curve_hand_computed():
    fills = [
        fill(Action.BUY, Side.YES, "10", "0.40", "0.07", 1),  # 1000.00 - 4.07 = 995.93
        fill(Action.SELL, Side.YES, "4", "0.55", "0.03", 2),  # + 2.17 = 998.10
    ]
    settlements = [settlement("6", "0", "6.00", 60)]  # + 6.00 = 1004.10
    anchor = to_micros("1000")
    curve = build_cash_curve(anchor, T0, fills, settlements)
    assert [e.cash_after_micros for e in curve] == [
        to_micros("995.93"),
        to_micros("998.10"),
        to_micros("1004.10"),
    ]
    assert curve == legacy_build_cash_curve(anchor, T0, fills, settlements)


def test_curve_orders_fill_settlement_event_at_one_timestamp():
    at = T0 + timedelta(minutes=5)
    usd = NormalizedLeg(asset="USD", delta_atoms=1_000_000, decimals=6, kind=LegKind.DEPOSIT)
    groups = [
        LegGroup(at, "event", "", (usd,)),
        LegGroup(at, "settlement", "T", ()),
        LegGroup(at, "fill", "T", ()),
    ]
    curve = cash_curve_from_groups(0, T0, groups)
    assert [e.kind for e in curve] == ["fill", "settlement", "event"]
    assert curve[-1].cash_after_micros == 1_000_000


def test_cash_legs_at_coarser_decimals_scale_up_exactly():
    # $12.3400 at 4 dp = 123_400 atoms -> 12_340_000 micros.
    leg = NormalizedLeg(asset="USD", delta_atoms=123_400, decimals=4, kind=LegKind.DEPOSIT)
    curve = cash_curve_from_groups(
        0, T0, [LegGroup(T0 + timedelta(seconds=1), "event", "", (leg,))]
    )
    assert curve[0].cash_flow_micros == 12_340_000


# ---- precision by asset --------------------------------------------------------


@dataclass
class Row:
    asset: str
    delta_atoms: int
    decimals: int
    id: int = 0
    balance_after_atoms: int | None = None


def test_balance_vector_rescales_to_the_max_decimals_per_asset():
    # BTC +0.5 at 8 dp and -0.1234567891 at 10 dp -> at 10 dp:
    # 5_000_000_000 - 1_234_567_891 = 3_765_432_109 (0.3765432109 BTC).
    # USD 100.25 at 2 dp plus -0.0001 at 4 dp -> 1_002_500 - 1 = 1_002_499 at 4 dp.
    rows = [
        Row("BTC", to_atoms("0.5", 8), 8),
        Row("BTC", to_atoms("-0.1234567891", 10), 10),
        Row("USD", to_atoms("100.25", 2), 2),
        Row("USD", to_atoms("-0.0001", 4), 4),
    ]
    vector = balance_vector(rows)
    assert vector["BTC"] == AssetAmount(asset="BTC", atoms=3_765_432_109, decimals=10)
    assert vector["USD"] == AssetAmount(asset="USD", atoms=1_002_499, decimals=4)


def test_balance_vector_adds_the_anchor_at_the_finest_scale():
    # anchor ETH 1.5 at 6 dp; +0.000000000001 ETH at 12 dp
    # -> 1_500_000_000_000 + 1 = 1_500_000_000_001 at 12 dp.
    vector = balance_vector(
        [Row("ETH", 1, 12)], start=[AssetAmount(asset="ETH", atoms=1_500_000, decimals=6)]
    )
    assert vector["ETH"].atoms == 1_500_000_000_001 and vector["ETH"].decimals == 12


def test_rescale_never_rounds():
    assert rescale(50_000_000, 8, 10) == 5_000_000_000
    assert rescale(5_000_000_000, 10, 8) == 50_000_000
    with pytest.raises(ValueError):
        rescale(1, 10, 8)


# ---- venue running balances ----------------------------------------------------


def test_balance_chain_hand_computed():
    # ZUSD at 4 dp: +100 bal 100; -30 (no balance); -20 bal 50: 100 - 30 - 20 = 50.
    ok = [
        Row("USD", 1_000_000, 4, id=1, balance_after_atoms=1_000_000),
        Row("USD", -300_000, 4, id=2),
        Row("USD", -200_000, 4, id=3, balance_after_atoms=500_000),
    ]
    assert balance_chain_breaks(ok) == []
    bad = ok[:2] + [Row("USD", -200_000, 4, id=3, balance_after_atoms=490_000)]
    (brk,) = balance_chain_breaks(bad)
    assert (brk.asset, brk.entry_id, brk.expected_atoms, brk.reported_atoms) == (
        "USD",
        3,
        500_000,
        490_000,
    )


def test_balance_chain_mixed_decimals_and_assets():
    # BTC reported at 8 dp then 10 dp: 0.5 bal 0.5 (8 dp); +0.0000000001 bal
    # 0.5000000001 (10 dp): 5_000_000_000 + 1 = 5_000_000_001 at 10 dp. An
    # interleaved ETH row does not touch the BTC chain.
    rows = [
        Row("BTC", 50_000_000, 8, id=1, balance_after_atoms=50_000_000),
        Row("ETH", 7, 2, id=2, balance_after_atoms=999),
        Row("BTC", 1, 10, id=3, balance_after_atoms=5_000_000_001),
    ]
    assert balance_chain_breaks(rows) == []
