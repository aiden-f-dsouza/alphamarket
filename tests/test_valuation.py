"""Asset valuation pinned by hand-computed scenarios (docs/methodology.md,
"Asset valuation" and "External flows (time-weighted return)").

Every expected value is worked out in the comment beside it. Units: USD
micros (1 USD = 1_000_000), quantities in atoms at the stated decimals."""

from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction

import pytest

from alphamarket.connectors.base import LegKind
from alphamarket.money import to_atoms, to_micros
from alphamarket.valuation import (
    AnchorIn,
    EquityPoint,
    FlagReason,
    LegIn,
    PerpFillIn,
    PerpPosition,
    PositionKind,
    PriceIn,
    Qty,
    ValuationFlag,
    daily_equity_series,
    daily_returns,
    exposure,
    holdings_at,
    modified_dietz_return,
    perp_positions_at,
    time_weighted_return,
    unrealized_pnl_micros,
    valuation_on,
)

D0 = date(2026, 9, 1)
D1, D2, D3, D4 = (D0 + timedelta(days=n) for n in range(1, 5))
M = 1_000_000  # micros per USD


def at(day: date, hour: int = 12) -> datetime:
    return datetime(day.year, day.month, day.day, hour, tzinfo=UTC)


def leg(asset: str, amount: str, decimals: int, kind: LegKind, day: date, hour: int = 12) -> LegIn:
    return LegIn(asset, to_atoms(amount, decimals), decimals, kind, at(day, hour))


def anc(asset: str, amount: str, decimals: int, entry: str | None = None) -> AnchorIn:
    return AnchorIn(asset, to_atoms(amount, decimals), decimals, entry)


def px(asset: str, day: date, close: str) -> PriceIn:
    return PriceIn(asset, day, to_micros(close))


def pfill(asset: str, qty: str, decimals: int, price: str, day: date, hour: int = 12) -> PerpFillIn:
    return PerpFillIn(asset, at(day, hour), to_atoms(qty, decimals), decimals, price)


def equities(series) -> list[int]:
    return [p.equity_micros for p in series]


def flows(series) -> list[int]:
    return [p.flow_micros for p in series]


# ---- holdings -------------------------------------------------------------------


def test_holdings_rescale_to_max_decimals_never_sum_raw_atoms():
    # Kraken-style: ZUSD reported at 4 dp in one row, 6 dp in another.
    anchor = [anc("USD", "12.3456", 4)]  # 123_456 atoms at 4 dp
    legs = [
        leg("USD", "0.000001", 6, LegKind.INTEREST, D1),  # 1 atom at 6 dp
        leg("USD", "5", 6, LegKind.DEPOSIT, D3),  # after t: excluded
    ]
    held = holdings_at(anchor, legs, at(D2))
    # 12.3456 -> 12_345_600 atoms at 6 dp, + 1 = 12_345_601 (12.345601 USD).
    # Summing raw atoms would give 123_457, i.e. nonsense at either scale.
    assert held == {"USD": Qty(12_345_601, 6)}
    assert held["USD"].units == Fraction(12_345_601, 10**6)


def test_holdings_inclusive_at_t_and_zero_holdings_omitted():
    anchor = [anc("ETH", "1.5", 8)]
    legs = [
        leg("ETH", "-1.5", 8, LegKind.FILL_BASE, D1),
        leg("USD", "4500", 6, LegKind.FILL_QUOTE, D1),
    ]
    # The leg at exactly t counts; ETH nets to zero and is omitted.
    assert holdings_at(anchor, legs, at(D1)) == {"USD": Qty(4_500_000_000, 6)}
    assert holdings_at(anchor, legs, at(D1) - timedelta(seconds=1)) == {"ETH": Qty(150_000_000, 8)}


def test_leg_day_is_its_utc_day():
    # 22:00 at UTC-5 on D1 is 03:00 UTC on D2: it belongs to D2's close.
    est = timezone(timedelta(hours=-5))
    local_d1 = datetime(D1.year, D1.month, D1.day, 22, tzinfo=est)
    late = LegIn("USD", 100 * M, 6, LegKind.DEPOSIT, local_d1)
    series = daily_equity_series([anc("USD", "0", 6)], [late], [], [], [D1, D2, D3])
    assert equities(series) == [0, 100 * M, 100 * M]
    assert flows(series) == [0, 100 * M, 0]


# ---- spot equity, flows, TWR --------------------------------------------------------


def _btc_prices():
    return [px("BTC", D1, "50000"), px("BTC", D2, "55000"), px("BTC", D3, "49500")]


def test_spot_mid_series_deposit_twr_equals_no_deposit_case():
    anchor = [anc("BTC", "1", 8)]
    days = [D1, D2, D3]
    plain = daily_equity_series(anchor, [], _btc_prices(), [], days)
    # E = 1 x 50000, 1 x 55000, 1 x 49500
    assert equities(plain) == [50_000 * M, 55_000 * M, 49_500 * M]

    deposit = [leg("BTC", "0.5", 8, LegKind.DEPOSIT, D2)]
    funded = daily_equity_series(anchor, deposit, _btc_prices(), [], days)
    # D2: 1.5 x 55000 = 82500, of which F = 0.5 x 55000 = 27500 is the deposit
    # D3: 1.5 x 49500 = 74250
    assert equities(funded) == [50_000 * M, 82_500 * M, 74_250 * M]
    assert flows(funded) == [0, 27_500 * M, 0]

    # r2 = (82500 - 27500) / 50000 - 1 = 1/10;  r3 = (74250 - 0) / 82500 - 1 = -1/10
    assert [d.r for d in daily_returns(funded)] == [None, Fraction(1, 10), Fraction(-1, 10)]
    # TWR = 1.1 x 0.9 - 1 = -1/100, same as holding 1 BTC: 49500 / 50000 - 1 = -1/100
    assert time_weighted_return(funded) == Fraction(-1, 100)
    assert time_weighted_return(plain) == Fraction(-1, 100)

    # Modified Dietz: N = 2 days, the deposit at end of D2 has weight (D3 - D2) / 2 = 1/2
    # (74250 - 50000 - 27500) / (50000 + 1/2 x 27500) = -3250 / 63750 = -13/255
    assert modified_dietz_return(funded) == Fraction(-13, 255)
    # No flows: MD collapses to E_N / E_0 - 1 = -1/100, the TWR
    assert modified_dietz_return(plain) == Fraction(-1, 100)

    # Window: TWR over [D2, D3] chains only r3 = -1/10
    assert time_weighted_return(funded, start=D2) == Fraction(-1, 10)


def test_cash_deposit_and_withdrawal_are_not_returns():
    anchor = [anc("USD", "1000", 6)]
    legs = [
        leg("USD", "500", 6, LegKind.DEPOSIT, D2),
        leg("USD", "-200", 6, LegKind.WITHDRAWAL, D3),
    ]
    series = daily_equity_series(anchor, legs, [], [], [D1, D2, D3])
    # E = 1000, 1500, 1300; F = 0, +500, -200
    assert equities(series) == [1000 * M, 1500 * M, 1300 * M]
    assert flows(series) == [0, 500 * M, -200 * M]
    # r2 = (1500 - 500) / 1000 - 1 = 0;  r3 = (1300 + 200) / 1500 - 1 = 0
    assert [d.r for d in daily_returns(series)] == [None, 0, 0]
    assert time_weighted_return(series) == 0
    # MD numerator: 1300 - 1000 - (500 - 200) = 0
    assert modified_dietz_return(series) == 0


def test_flows_on_unlisted_days_go_to_the_next_listed_day():
    legs = [leg("USD", "300", 6, LegKind.TRANSFER_IN, D2)]
    series = daily_equity_series([anc("USD", "1000", 6)], legs, [], [], [D1, D3])
    # D2 is not listed: its transfer belongs to the period ending D3.
    assert flows(series) == [0, 300 * M]
    # r3 = (1300 - 300) / 1000 - 1 = 0
    assert time_weighted_return(series) == 0


def test_first_funded_day_starts_the_series():
    series = [
        EquityPoint(D1, 0),
        EquityPoint(D2, 1000 * M, 1000 * M),  # funded at end of D2
        EquityPoint(D3, 1100 * M),
    ]
    # r1: no previous point; r2: E1 = 0, undefined; r3 = 1100 / 1000 - 1 = 1/10
    assert [d.r for d in daily_returns(series)] == [None, None, Fraction(1, 10)]
    assert time_weighted_return(series) == Fraction(1, 10)
    # MD: (1100 - 0 - 1000) / (0 + (D3 - D2)/2 x 1000) = 100 / 500 = 1/5
    assert modified_dietz_return(series) == Fraction(1, 5)
    # Only unfunded days: nothing defined
    assert time_weighted_return(series[:2]) is None


def test_emptied_account_skips_the_zero_day_and_resumes():
    series = [
        EquityPoint(D1, 1000 * M),
        EquityPoint(D2, 0, -1100 * M),  # grew 10% then withdrew everything
        EquityPoint(D3, 500 * M, 500 * M),  # refunded
        EquityPoint(D4, 450 * M),
    ]
    # r2 = (0 + 1100) / 1000 - 1 = 1/10;  r3: E2 = 0, undefined;  r4 = 450 / 500 - 1 = -1/10
    assert [d.r for d in daily_returns(series)] == [
        None,
        Fraction(1, 10),
        None,
        Fraction(-1, 10),
    ]
    # 1.1 x 0.9 - 1 = -1/100
    assert time_weighted_return(series) == Fraction(-1, 100)


def test_eth_btc_fill_valued_through_two_closes():
    anchor = [anc("BTC", "1", 8)]
    # Buy 10 ETH at 0.05 BTC on the ETH/BTC book: +10 ETH, -0.5 BTC
    legs = [
        leg("ETH", "10", 8, LegKind.FILL_BASE, D1),
        leg("BTC", "-0.5", 8, LegKind.FILL_QUOTE, D1),
    ]
    prices = [
        px("BTC", D0, "60000"),
        px("BTC", D1, "60000"),
        px("ETH", D1, "3100"),
        px("BTC", D2, "62000"),
        px("ETH", D2, "3050"),
    ]
    series = daily_equity_series(anchor, legs, prices, [], [D0, D1, D2])
    # D0: 1 x 60000                         = 60000
    # D1: 0.5 x 60000 + 10 x 3100           = 30000 + 31000 = 61000
    # D2: 0.5 x 62000 + 10 x 3050           = 31000 + 30500 = 61500
    assert equities(series) == [60_000 * M, 61_000 * M, 61_500 * M]
    d2 = {p.asset: p.value_micros for p in series[2].positions}
    assert d2 == {"BTC": 31_000 * M, "ETH": 30_500 * M}
    # r2 = 61500 / 61000 - 1 = 500 / 61000 = 1/122
    assert daily_returns(series)[2].r == Fraction(1, 122)


def test_bnb_fee_is_valued_at_the_bnb_close():
    anchor = [anc("USDT", "1000", 8), anc("BNB", "1", 8)]
    # Buy 0.01 BTC for 600 USDT, fee 0.0075 BNB
    legs = [
        leg("BTC", "0.01", 8, LegKind.FILL_BASE, D1),
        leg("USDT", "-600", 8, LegKind.FILL_QUOTE, D1),
        leg("BNB", "-0.0075", 8, LegKind.FEE, D1),
    ]
    prices = [px("BNB", D0, "600"), px("BNB", D1, "600"), px("BTC", D1, "60000")]
    series = daily_equity_series(anchor, legs, prices, [], [D0, D1])
    # D0: 1000 (USDT at par) + 1 x 600                        = 1600
    # D1: 400 + 0.01 x 60000 + 0.9925 x 600 = 400 + 600 + 595.5 = 1595.5
    assert equities(series) == [1600 * M, 1_595_500_000]
    kinds = {p.asset: p.kind for p in series[1].positions}
    asset, cash = PositionKind.ASSET, PositionKind.CASH
    assert kinds == {"BNB": asset, "BTC": asset, "USDT": cash}
    # The fee is a cost, not a flow: r = 1595.5 / 1600 - 1 = -4.5 / 1600 = -9/3200
    assert flows(series) == [0, 0]
    assert time_weighted_return(series) == Fraction(-9, 3200)


def test_split_two_for_one_leaves_equity_unchanged_and_dividend_is_return():
    anchor = [anc("AAPL", "10", 9), anc("USD", "0", 6)]
    legs = [
        leg("AAPL", "10", 9, LegKind.SPLIT, D2),  # 2:1 split: 10 -> 20 shares
        leg("USD", "5", 6, LegKind.DIVIDEND, D3),  # 20 x 0.25
        leg("USD", "-0.75", 6, LegKind.TAX, D3),  # 15% withholding
    ]
    # Closes as traded: 200 before the split, 100 after it.
    prices = [px("AAPL", D1, "200"), px("AAPL", D2, "100"), px("AAPL", D3, "100")]
    series = daily_equity_series(anchor, legs, prices, [], [D1, D2, D3])
    # D1: 10 x 200 = 2000;  D2: 20 x 100 = 2000;  D3: 20 x 100 + 5 - 0.75 = 2004.25
    assert equities(series) == [2000 * M, 2000 * M, 2_004_250_000]
    # Neither a split nor a dividend is an external flow.
    assert flows(series) == [0, 0, 0]
    # r2 = 0;  r3 = 4.25 / 2000 = 17/8000
    assert [d.r for d in daily_returns(series)] == [None, 0, Fraction(17, 8000)]


def test_missing_close_carries_forward_and_never_priced_is_excluded():
    anchor = [anc("SOL", "10", 9), anc("DOGE", "100", 8), anc("USD", "50", 6)]
    prices = [px("SOL", D1, "150"), px("SOL", D3, "160")]  # no SOL close on D2
    series = daily_equity_series(anchor, [], prices, [], [D1, D2, D3])
    # D1: 10 x 150 + 50 = 1550;  D2: carried D1 close -> 1550;  D3: 10 x 160 + 50 = 1650
    # DOGE is never priced: excluded every day.
    assert equities(series) == [1550 * M, 1550 * M, 1650 * M]
    assert series[0].flags == (ValuationFlag(D1, "DOGE", FlagReason.UNPRICED),)
    assert set(series[1].flags) == {
        ValuationFlag(D2, "DOGE", FlagReason.UNPRICED),
        ValuationFlag(D2, "SOL", FlagReason.CARRIED_FORWARD, price_day=D1),
    }
    sol = next(p for p in series[1].positions if p.asset == "SOL")
    assert (sol.close_micros, sol.price_day) == (150 * M, D1)


def test_asset_first_priced_later_is_unpriced_before_that():
    prices = [px("SOL", D2, "150")]
    series = daily_equity_series([anc("SOL", "2", 9)], [], prices, [], [D1, D2])
    assert equities(series) == [0, 300 * M]
    assert series[0].flags == (ValuationFlag(D1, "SOL", FlagReason.UNPRICED),)
    assert series[1].flags == ()


def test_unpriced_flow_is_excluded_from_flows_like_from_equity():
    legs = [leg("XYZ", "5", 8, LegKind.DEPOSIT, D2)]
    series = daily_equity_series([anc("USD", "100", 6)], legs, [], [], [D1, D2])
    # XYZ has no close: E2 = 100 (XYZ excluded), F2 = 0 (excluded too) -> r2 = 0
    assert equities(series) == [100 * M, 100 * M]
    assert flows(series) == [0, 0]
    assert ValuationFlag(D2, "XYZ", FlagReason.UNPRICED) in series[1].flags


def test_rounding_truncates_toward_zero_per_position():
    prices = [px("BTC", D1, "50000.123456")]
    # 1 sat x 50000.123456 = 500.00123456 micros -> 500
    assert valuation_on([anc("BTC", "0.00000001", 8)], [], prices, [], D1).equity_micros == 500
    # -1 sat (borrowed) -> -500.00123456 -> -500 (toward zero, not -501)
    assert valuation_on([anc("BTC", "-0.00000001", 8)], [], prices, [], D1).equity_micros == -500
    # Cash at 8 dp: 0.00000099 USDT = 0.99 micros -> 0
    assert valuation_on([anc("USDT", "0.00000099", 8)], [], [], [], D1).equity_micros == 0


# ---- perps --------------------------------------------------------------------------


def _perp_scenario():
    """BTC-PERP at 5 dp, USDC margin at 6 dp. Open, add, partial close, flip."""
    anchor = [anc("USDC", "10000", 6)]
    fills = [
        pfill("BTC-PERP", "0.1", 5, "60000", D1),  # open long 0.1
        pfill("BTC-PERP", "0.1", 5, "62000", D2),  # add 0.1
        pfill("BTC-PERP", "-0.15", 5, "63000", D3),  # partial close 0.15
        pfill("BTC-PERP", "-0.15", 5, "60000", D4),  # close 0.05, open short 0.1
    ]
    legs = [
        leg("BTC-PERP", "0.1", 5, LegKind.FILL_BASE, D1),
        leg("USDC", "-2.4", 6, LegKind.FEE, D1),
        leg("BTC-PERP", "0.1", 5, LegKind.FILL_BASE, D2),
        leg("USDC", "-1.5", 6, LegKind.FUNDING, D2, hour=16),
        leg("BTC-PERP", "-0.15", 5, LegKind.FILL_BASE, D3),
        # venue's realized P&L: 0.15 x (63000 - 61000) = +300
        leg("USDC", "300", 6, LegKind.REALIZED_PNL, D3),
        leg("BTC-PERP", "-0.15", 5, LegKind.FILL_BASE, D4),
        # closing 0.05 of the long: 0.05 x (60000 - 61000) = -50
        leg("USDC", "-50", 6, LegKind.REALIZED_PNL, D4),
        leg("USDC", "0.8", 6, LegKind.FUNDING, D4, hour=16),  # shorts receive
    ]
    prices = [
        px("BTC-PERP", D1, "61000"),
        px("BTC-PERP", D2, "61500"),
        px("BTC-PERP", D3, "62000"),
        px("BTC-PERP", D4, "59000"),
        # Spot BTC closes exist but a perp with its own closes uses those.
        px("BTC", D1, "70000"),
        px("BTC", D4, "70000"),
    ]
    return anchor, legs, prices, fills


def test_perp_average_entry_through_open_add_reduce_flip():
    anchor, _, _, fills = _perp_scenario()

    def pos(day):
        return perp_positions_at(anchor, fills, at(day, 23))["BTC-PERP"]

    # open: 0.1 @ 60000
    assert pos(D1) == PerpPosition("BTC-PERP", Qty(10_000, 5), Fraction(60000))
    # add: (0.1 x 60000 + 0.1 x 62000) / 0.2 = 61000
    assert pos(D2) == PerpPosition("BTC-PERP", Qty(20_000, 5), Fraction(61000))
    # reduce by 0.15: entry kept at 61000, 0.05 left
    assert pos(D3) == PerpPosition("BTC-PERP", Qty(5_000, 5), Fraction(61000))
    # flip through zero: -0.1 at the flip price 60000
    assert pos(D4) == PerpPosition("BTC-PERP", Qty(-10_000, 5), Fraction(60000))
    assert perp_positions_at(anchor, fills, at(D0)) == {}


def test_perp_equity_with_funding_and_realized_legs():
    anchor, legs, prices, fills = _perp_scenario()
    series = daily_equity_series(anchor, legs, prices, fills, [D1, D2, D3, D4])
    # D1: cash 10000 - 2.4 = 9997.6;   uPnL  0.1 x (61000 - 60000) = 100   -> 10097.6
    # D2: cash 9997.6 - 1.5 = 9996.1;  uPnL  0.2 x (61500 - 61000) = 100   -> 10096.1
    # D3: cash 9996.1 + 300 = 10296.1; uPnL 0.05 x (62000 - 61000) = 50    -> 10346.1
    # D4: cash 10296.1 - 50 + 0.8 = 10246.9; uPnL -0.1 x (59000 - 60000) = 100 -> 10346.9
    assert equities(series) == [
        10_097_600_000,
        10_096_100_000,
        10_346_100_000,
        10_346_900_000,
    ]
    assert all(p.flags == () for p in series)  # ledger perp legs agree with the fills
    perp = next(p for p in series[3].positions if p.kind is PositionKind.PERP)
    # notional -0.1 x 59000 = -5900; value = uPnL = +100
    assert (perp.notional_micros, perp.value_micros, perp.close_micros) == (
        -5900 * M,
        100 * M,
        59000 * M,
    )
    assert perp.entry == 60000
    # Funding and realized P&L are returns, not flows.
    assert flows(series) == [0, 0, 0, 0]


def test_perp_unrealized_truncates_toward_zero():
    fills = [
        pfill("X-PERP", "1", 0, "10", D1),
        pfill("X-PERP", "2", 0, "11", D1, hour=13),
        pfill("X-PERP", "-1", 0, "12", D1, hour=14),
    ]
    position = perp_positions_at([], fills, at(D1, 23))["X-PERP"]
    # entry = (1 x 10 + 2 x 11) / 3 = 32/3, kept through the reduction; qty 2
    assert position == PerpPosition("X-PERP", Qty(2, 0), Fraction(32, 3))
    # at 10.5: 2 x (10.5 - 32/3) = 21 - 64/3 = -1/3 USD = -333333.33 micros -> -333333
    assert unrealized_pnl_micros(position, 10_500_000) == -333_333
    # at 11: 2 x (11 - 32/3) = 2/3 USD = 666666.67 micros -> 666666
    assert unrealized_pnl_micros(position, 11 * M) == 666_666


def test_anchor_perp_starts_at_its_entry_price():
    anchor = [anc("USDC", "5000", 6), anc("ETH-PERP", "-2", 4, entry="3000")]
    fills = [pfill("ETH-PERP", "1", 4, "2900", D1)]  # buy back 1 of the 2 short
    legs = [
        leg("ETH-PERP", "1", 4, LegKind.FILL_BASE, D1),
        leg("USDC", "100", 6, LegKind.REALIZED_PNL, D1),  # 1 x (3000 - 2900)
    ]
    prices = [px("ETH-PERP", D0, "2950"), px("ETH-PERP", D1, "2800")]
    series = daily_equity_series(anchor, legs, prices, fills, [D0, D1])
    # D0: 5000 + (-2) x (2950 - 3000) = 5000 + 100 = 5100
    # D1: 5100 (cash incl. +100 realized) + (-1) x (2800 - 3000) = 5100 + 200 = 5300
    assert equities(series) == [5100 * M, 5300 * M]


def test_perp_without_anchor_entry_is_excluded_until_known():
    anchor = [anc("USDC", "1000", 6), anc("SOL-PERP", "5", 2)]  # no entry_price
    fills = [pfill("SOL-PERP", "-8", 2, "150", D2)]  # flip to short 3 @ 150
    legs = [leg("SOL-PERP", "-8", 2, LegKind.FILL_BASE, D2)]
    prices = [px("SOL-PERP", D1, "140"), px("SOL-PERP", D2, "145")]
    series = daily_equity_series(anchor, legs, prices, fills, [D1, D2])
    # D1: entry unknown -> the perp is excluded; E = 1000
    assert series[0].equity_micros == 1000 * M
    assert series[0].flags == (ValuationFlag(D1, "SOL-PERP", FlagReason.PERP_ENTRY_UNKNOWN),)
    # D2: flipped, entry 150: -3 x (145 - 150) = +15 -> 1015 (no realized leg in this toy)
    assert series[1].equity_micros == 1015 * M
    assert series[1].flags == ()


def test_perp_qty_mismatch_is_flagged():
    fills = [pfill("BTC-PERP", "0.1", 5, "60000", D1)]  # the ledger has no FILL_BASE leg
    prices = [px("BTC-PERP", D1, "60000")]
    v = valuation_on([anc("USDC", "100", 6)], [], prices, fills, D1)
    assert ValuationFlag(D1, "BTC-PERP", FlagReason.PERP_QTY_MISMATCH) in v.flags


# ---- exposure -----------------------------------------------------------------------


def test_exposure_shares_concentration_and_perp_leverage():
    anchor = [
        anc("USDC", "10000", 6),
        anc("BTC", "0.1", 8),
        anc("ETH", "1", 8),
        anc("BTC-PERP", "-0.2", 5, entry="51000"),
    ]
    # No BTC-PERP closes: the perp is valued at the underlying's close.
    prices = [px("BTC", D1, "50000"), px("ETH", D1, "3000")]
    v = valuation_on(anchor, [], prices, [], D1)
    # equity = 10000 + 0.1 x 50000 + 1 x 3000 + (-0.2) x (50000 - 51000)
    #        = 10000 + 5000 + 3000 + 200 = 18200
    assert v.equity_micros == 18_200 * M
    assert v.flags == ()
    e = exposure(v)
    # gross = |5000| (BTC) + |3000| (ETH) + |-0.2 x 50000| (BTC-PERP) = 18000; cash excluded
    assert e.gross_micros == 18_000 * M
    assert e.by_underlying_micros == {"BTC": 15_000 * M, "ETH": 3_000 * M}
    # BTC = (5000 + 10000) / 18000 = 5/6;  ETH = 3000 / 18000 = 1/6
    assert e.shares == {"BTC": Fraction(5, 6), "ETH": Fraction(1, 6)}
    assert (e.top_asset, e.top_share) == ("BTC", Fraction(5, 6))
    # perp gross leverage = 10000 / 18200 = 50/91
    assert e.perp_gross_micros == 10_000 * M
    assert e.perp_gross_leverage == Fraction(50, 91)


def test_exposure_of_cash_only_account():
    e = exposure(valuation_on([anc("USD", "100", 6)], [], [], [], D1))
    assert (e.gross_micros, e.shares, e.top_asset, e.top_share) == (0, {}, None, None)
    assert e.perp_gross_leverage == 0


# ---- input validation -------------------------------------------------------------


def test_inputs_reject_floats_naive_times_and_bad_order():
    with pytest.raises(ValueError, match="timezone-aware"):
        LegIn("USD", 1, 6, LegKind.DEPOSIT, datetime(2026, 9, 1))
    with pytest.raises(TypeError):
        LegIn("USD", 1.0, 6, LegKind.DEPOSIT, at(D1))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PriceIn("BTC", D1, 5.0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        PerpFillIn("BTC-PERP", at(D1), 1, 5, 60000.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="-PERP"):
        PerpFillIn("BTC", at(D1), 1, 5, "60000")
    with pytest.raises(ValueError, match="decimals"):
        AnchorIn("BTC", 1, 13)
    with pytest.raises(ValueError, match="strictly increasing"):
        daily_equity_series([], [], [], [], [D2, D1])
    with pytest.raises(ValueError, match="one row per asset"):
        holdings_at([anc("USD", "1", 6), anc("USD", "2", 6)], [], at(D1))
    with pytest.raises(ValueError, match="two closes"):
        daily_equity_series([], [], [px("BTC", D1, "1"), px("BTC", D1, "2")], [], [D1])
    with pytest.raises(ValueError, match="rescale"):
        Qty(1, 8).rescale(6)
