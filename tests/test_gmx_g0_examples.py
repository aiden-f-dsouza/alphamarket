"""Pins the worked examples of the GMX G0 design note (docs/venues/gmx_arbitrum.md).

No connector exists yet (G1). This file holds a minimal REFERENCE of the
note's rules (cash legs per trade action, the synthetic account in both
candidate forms, the quantity rule) and runs the note's fixture sequences
through the real valuation engine, so every number the note quotes is
checked by code. G1 replaces the reference with the connector and keeps the
expected values.

Fixture rows: tests/fixtures/gmx/g0_examples_2026-10-22.json and
g0_research_rows_2026-10-22.json (README there).
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_EVEN
from fractions import Fraction
from pathlib import Path

import pytest

from alphamarket import analytics_v2, valuation
from alphamarket.connectors.base import LegKind
from alphamarket.money import fixed_to_atoms

FIXTURES = Path(__file__).parent / "fixtures" / "gmx"
ROWS = json.loads((FIXTURES / "g0_examples_2026-10-22.json").read_text())
RESEARCH = json.loads((FIXTURES / "g0_research_rows_2026-10-22.json").read_text())

USDC = "USDC"
USDC_DECIMALS = 6
ETH_S = "ETH.70d95587-S-PERP"  # <INDEX>.<first 8 hex of the market>-<L|S>-PERP
ARB_S = "ARB.c25cef60-S-PERP"
XPL_S = "XPL.4b67aa8f-S-PERP"


# ---- reference rules (note sections 3 and 4) ----------------------------------


def usd30(raw: int) -> int:
    """A 30-dp GMX USD integer as USDC micros, half to even (note 4.1)."""
    return fixed_to_atoms(str(raw), 30, USDC_DECIMALS, rounding=ROUND_HALF_EVEN)


def trunc12(raw18: int) -> int:
    """An 18-dp token amount as 12-dp atoms, toward zero (note 4.2)."""
    return fixed_to_atoms(str(raw18), 18, 12, rounding=ROUND_DOWN)


def fill_price(size_delta_usd: int, size_delta_tokens: int, token_decimals: int) -> str:
    """Increase fill price = sizeDeltaUsd / sizeDeltaInTokens (GMX's own entry),
    half to even at the venue's price scale, 30 - token decimals dp (note 3.2)."""
    places = 30 - token_decimals
    exact = Fraction(size_delta_usd, size_delta_tokens * 10 ** (30 - token_decimals))
    scaled = exact * 10**places
    n, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest > scaled.denominator or (2 * rest == scaled.denominator and n % 2):
        n += 1
    whole, frac = divmod(n, 10**places)
    return f"{whole}.{frac:0{places}d}"


def raw_price(raw: str, token_decimals: int) -> str:
    places = 30 - token_decimals
    whole, frac = divmod(int(raw), 10**places)
    return f"{whole}.{frac:0{places}d}"


def cash_legs(row: dict) -> dict[LegKind, int]:
    """USDC legs of one OrderExecuted trade action with USDC collateral (note 3.1).

    REALIZED_PNL = basePnlUsd + impact + swapImpactUsd, rounded once, where
    impact = totalImpactUsd (v2.2 rows) else priceImpactUsd; only on decreases.
    FEE = -(positionFee - traderDiscount + uiFee) in USDC atoms (exact)
          - swapFeeUsd (rounded); INTEREST = -borrowingFee; FUNDING = -fundingFee;
    LIQUIDATION = -liquidationFee. All token amounts are USDC atoms, exact."""
    legs: dict[LegKind, int] = {}
    if row["orderType"] in (4, 5, 6, 7):
        impact = row.get("totalImpactUsd") or row["priceImpactUsd"]
        gross = int(row["basePnlUsd"]) + int(impact) + int(row.get("swapImpactUsd") or 0)
        legs[LegKind.REALIZED_PNL] = usd30(gross)
    fee = int(row["positionFeeAmount"]) - int(row["traderDiscountAmount"])
    fee += int(row.get("uiFeeAmount") or 0)
    legs[LegKind.FEE] = -fee - usd30(int(row.get("swapFeeUsd") or 0))
    legs[LegKind.INTEREST] = -int(row["borrowingFeeAmount"])
    legs[LegKind.FUNDING] = -int(row["fundingFeeAmount"])
    legs[LegKind.LIQUIDATION] = -int(row.get("liquidationFeeAmount") or 0)
    return {kind: v for kind, v in legs.items() if v}


@dataclass
class Action:
    at: datetime
    asset: str
    d_tokens: int  # signed change of the exact 18-dp venue size (short: negative)
    price: str
    d_collateral: int  # USDC atoms, K_after - K_before
    cash: dict[LegKind, int]


@dataclass
class Synthetic:
    """The synthetic account (note section 2). retained=True is model A,
    False is model B (floor = most collateral posted at once since the anchor)."""

    retained: bool
    anchor_at: datetime
    dated_at_day_start: bool = True  # T1 (note 2.4)
    cash: int = 0
    collateral: dict[str, int] = field(default_factory=dict)
    size: dict[str, int] = field(default_factory=dict)
    floor: int = 0
    legs: list[valuation.LegIn] = field(default_factory=list)
    fills: list[valuation.PerpFillIn] = field(default_factory=list)

    def apply(self, a: Action) -> None:
        locked = sum(self.collateral.values())
        free = self.cash - locked
        delta_cash = sum(a.cash.values())
        posted = a.d_collateral - delta_cash  # > 0 the wallet posted, < 0 it was paid
        for kind, value in a.cash.items():
            self.legs.append(valuation.LegIn(USDC, value, USDC_DECIMALS, kind, a.at))
        deposit = max(0, posted - free) if posted > 0 else 0
        if deposit:
            when = a.at
            if self.dated_at_day_start:
                start = datetime.combine(a.at.date(), time(0), tzinfo=UTC)
                one = timedelta(microseconds=1)
                when = max(start - one, self.anchor_at + one)
            self.legs.append(valuation.LegIn(USDC, deposit, USDC_DECIMALS, LegKind.DEPOSIT, when))
        self.cash += delta_cash + deposit
        k = self.collateral.get(a.asset, 0) + a.d_collateral
        if k:
            self.collateral[a.asset] = k
        else:
            self.collateral.pop(a.asset, None)
        self.floor = max(self.floor, sum(self.collateral.values()))
        # B's invariant (note 2.2): after every action, C <= M. Only a payout can
        # break it (an increase's deposit leaves F = 0, so C = L <= M).
        if not self.retained and self.cash > self.floor:
            out = self.cash - self.floor
            self.legs.append(valuation.LegIn(USDC, -out, USDC_DECIMALS, LegKind.WITHDRAWAL, a.at))
            self.cash -= out
        if a.d_tokens:
            before = self.size.get(a.asset, 0)
            after = before + a.d_tokens
            qty = trunc12(after) - trunc12(before)  # running-total rule (note 4.2)
            self.size[a.asset] = after
            self.fills.append(valuation.PerpFillIn(a.asset, a.at, qty, 12, a.price))
            self.legs.append(valuation.LegIn(a.asset, qty, 12, LegKind.FILL_BASE, a.at))


def at(unix: int) -> datetime:
    return datetime.fromtimestamp(unix, UTC)


def action(row: dict, asset: str, d_collateral: int) -> Action:
    tokens = int(row["sizeDeltaInTokens"])
    increase = row["orderType"] in (2, 3, 8)
    signed = tokens if increase == row["isLong"] else -tokens  # long +, short -
    if increase:
        price = fill_price(int(row["sizeDeltaUsd"]), tokens, 18) if tokens else "0"
    else:
        price = raw_price(row["executionPrice"], 18)
    return Action(at(row["timestamp"]), asset, signed, price, d_collateral, cash_legs(row))


def constructed_open(close: dict, asset: str, opened: datetime) -> Action:
    """Note 2.3: the opens of the ARB and XPL positions are not in the fixtures.
    Constructed on the morning of the close: the same size, entry and
    collateral, and a 6 bps position fee (GMX's imbalancing rate)."""
    size_usd, tokens = int(close["sizeDeltaUsd"]), int(close["sizeDeltaInTokens"])
    fee = usd30_fraction(Fraction(size_usd * 6, 10_000))
    return Action(
        opened,
        asset,
        -tokens if not close["isLong"] else tokens,
        fill_price(size_usd, tokens, 18),
        int(close["initialCollateralDeltaAmount"]),
        {LegKind.FEE: -fee},
    )


def usd30_fraction(value: Fraction) -> int:
    scaled = value / 10**24
    n, rest = divmod(scaled.numerator, scaled.denominator)
    if 2 * rest > scaled.denominator or (2 * rest == scaled.denominator and n % 2):
        n += 1
    return n


ETH = ROWS["eth_short_lifecycle"]
# Collateral changes (USDC atoms). REST /v1/trades rows carry no collateral-after,
# so dK is each row's initialCollateralDeltaAmount (+ on increases, - on decreases),
# hand-typed here. That this is the change of positionChanges.collateralAmount is
# an INFERENCE (note 2.5), supported by address B's 20 joined position changes
# (test_research_collateral_delta_equals_initial_collateral_delta) and by t5: the
# full close releases 42,010.954459 - 16,127.372685 = 25,883.581774 to the atom.
# G1 pins it on recorded positionChanges rows of a USDC-collateral address.
SEQ1 = [
    action(ETH[0], ETH_S, 22_196_954_461),
    action(ETH[1], ETH_S, 9_906_999_999),
    action(ETH[2], ETH_S, 9_906_999_999),
    action(ETH[3], ETH_S, -16_127_372_685),
    action(ETH[4], ETH_S, -25_883_581_774),
]
ARB = ROWS["arb_short_liquidation"]
XPL = ROWS["xpl_short_close"]
SEQ2 = [
    constructed_open(ARB, ARB_S, datetime(2026, 9, 2, 0, 0, 1, tzinfo=UTC)),
    action(ARB, ARB_S, -26_480_734_267),
    constructed_open(XPL, XPL_S, datetime(2026, 10, 13, 0, 0, 30, tzinfo=UTC)),
    action(XPL, XPL_S, -4_693_985_368),
]
ANCHOR_1 = datetime(2026, 7, 6, 12, tzinfo=UTC)  # watched while flat
ANCHOR_2 = datetime(2026, 9, 1, 12, tzinfo=UTC)
DAYS_1 = [date(2026, 7, 6) + timedelta(days=n) for n in range(6)]
DAYS_12 = [*DAYS_1, date(2026, 9, 1), date(2026, 9, 2), date(2026, 10, 12), date(2026, 10, 13)]
DAYS_2 = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 10, 12), date(2026, 10, 13)]


def closes() -> list[valuation.PriceIn]:
    out = []
    for day, close in ROWS["eth_daily_closes"].items():
        micros = int(Fraction(close) * 1_000_000)
        out.append(valuation.PriceIn(ETH_S, date.fromisoformat(day), micros, close=close))
    return out


def run(actions, retained, anchor, days, *, t1=True):
    model = Synthetic(retained, anchor, dated_at_day_start=t1)
    for a in actions:
        model.apply(a)
    legs = [leg for leg in model.legs if leg.occurred_at > anchor]
    series = valuation.daily_equity_series([], legs, closes(), model.fills, days)
    return model, series


def equity(series):
    return [p.equity_micros for p in series]


def flows(series):
    return [p.flow_micros for p in series]


def drawdown(series) -> Fraction:
    return analytics_v2.index_drawdown(analytics_v2.twr_index(series))


def pnl(series) -> int:
    """P&L net of deposits as analytics_v2.compute_asset_stats takes it: the first
    point (the anchor day) is the base, so its own flow is not netted again."""
    return series[-1].equity_micros - series[0].equity_micros - sum(flows(series)[1:])


def run_peak(actions, anchor, days):
    """Option C (note 2.8), Polymarket's convention: the curve shifted by the peak
    capital invested. For GMX the peak of the cumulative net posting since the
    anchor is exactly A's total deposits (A deposits only when it is exceeded), so
    C is A with one deposit of that total placed right after the anchor."""
    model = Synthetic(True, anchor)
    for a in actions:
        model.apply(a)
    peak = sum(leg.delta_atoms for leg in model.legs if leg.kind is LegKind.DEPOSIT)
    legs = [leg for leg in model.legs if leg.kind is not LegKind.DEPOSIT]
    after_anchor = anchor + timedelta(microseconds=1)
    legs.append(valuation.LegIn(USDC, peak, USDC_DECIMALS, LegKind.DEPOSIT, after_anchor))
    return valuation.daily_equity_series([], legs, closes(), model.fills, days)


# ---- 3.1 cash legs ----------------------------------------------------------------


def _fees_usd30(row: dict) -> int:
    tokens = int(row["positionFeeAmount"]) - int(row["traderDiscountAmount"])
    tokens += int(row["borrowingFeeAmount"]) + int(row["fundingFeeAmount"])
    tokens += int(row.get("liquidationFeeAmount") or 0)
    return tokens * int(row["collateralTokenPriceMin"])


@pytest.mark.parametrize(
    "row",
    [ETH[3], ETH[4], ARB, XPL, *ROWS["pre_v22_decreases_with_swap"]],
    ids=["eth-partial", "eth-close", "arb-liq", "xpl-close", "swap-1", "swap-2"],
)
def test_realized_pnl_definition_reproduces_gmx_net_pnl(row):
    # GMX's own pnlUsd (30 dp) = basePnlUsd + impact + swapImpactUsd - swapFeeUsd
    #                            - (token fees x collateral token price), exactly,
    # with impact = totalImpactUsd on v2.2 rows, priceImpactUsd before it.
    impact = row.get("totalImpactUsd") or row["priceImpactUsd"]
    rebuilt = (
        int(row["basePnlUsd"])
        + int(impact)
        + int(row.get("swapImpactUsd") or 0)
        - int(row.get("swapFeeUsd") or 0)
        - _fees_usd30(row)
    )
    assert rebuilt == int(row["pnlUsd"])


def test_eth_short_cash_legs_by_hand():
    # t1 open: position fee 360.341330 - 10% referral discount 36.034133.
    assert cash_legs(ETH[0]) == {LegKind.FEE: -324_307_197}
    assert cash_legs(ETH[1]) == {} and cash_legs(ETH[2]) == {}  # pure top-ups
    # t4 partial: 1,937.068096358089... + 391.577631489888... = 2,328.645727847977...
    #   -> 2,328.645728 (half-even); fee 120.034572 - 12.003457 = 108.031115.
    assert cash_legs(ETH[3]) == {
        LegKind.REALIZED_PNL: 2_328_645_728,
        LegKind.FEE: -108_031_115,
        LegKind.INTEREST: -345_523_541,
        LegKind.FUNDING: -694_422_380,
    }
    # t5 close: 3,281.082473144732... + 700.666565017784... = 3,981.749038162516... -> 3,981.749038
    assert cash_legs(ETH[4]) == {
        LegKind.REALIZED_PNL: 3_981_749_038,
        LegKind.FEE: -216_307_953,
        LegKind.INTEREST: -50_217_964,
        LegKind.FUNDING: -51_365_482,
    }
    # Liquidation: -25,338.356027019290... - 145.884817537476...
    #   = -25,484.240844556766... -> -25,484.240845
    assert cash_legs(ARB) == {LegKind.REALIZED_PNL: -25_484_240_845}
    # 51,033.091798973216... + 82.324299717257... = 51,115.416098690473... -> 51,115.416099
    assert cash_legs(XPL) == {
        LegKind.REALIZED_PNL: 51_115_416_099,
        LegKind.FEE: -63_684_669,
        LegKind.INTEREST: -3_646_511_290,
        LegKind.FUNDING: -1_594_952_122,
    }


def test_increase_fill_price_is_gmx_entry():
    # 900,675.166653533961919887914643226624 / 500.039352125288774211 = 1,801.2085705363896...
    assert SEQ1[0].price == "1801.208570536390"
    assert raw_price(ETH[0]["indexTokenPriceMin"], 18) == "1801.208570536390"  # the oracle price
    # executionPrice is not used for increases: v2.2 defers the impact to the close.
    assert raw_price(ETH[0]["executionPrice"], 18) == "1802.533269877269"
    assert SEQ2[0].price == "0.088878084931" and SEQ2[2].price == "0.153664038929"


# ---- 2.3 worked example 1: the ETH short ---------------------------------------


@pytest.mark.parametrize("retained", [True, False], ids=["A-retained", "B-floor"])
def test_example_1_eth_short(retained):
    model, series = run(SEQ1, retained, ANCHOR_1, DAYS_1)
    # Deposits: 22,521.261658 + 9,906.999999 + 9,906.999999 = 42,335.261656, dated
    # 2026-07-06 23:59:59.999999 (T1). Day 2026-07-07: cash 42,010.954459 plus
    # 500.039352125288 x (1,801.208570536390 - 1,770.09) = 15,560.509850 (truncated).
    head = [42_335_261_656, 57_571_464_309, 71_612_569_316, 70_717_498_876]
    if retained:
        assert equity(series) == [*head, 46_855_480_790, 46_855_480_790]
        assert flows(series) == [42_335_261_656, 0, 0, 0, 0, 0]
        assert model.cash == 46_855_480_790
    else:
        # B: 1,180.668692 and 3,663.857639 above the 42,010.954459 floor are withdrawn.
        assert equity(series) == [*head, 42_010_954_459, 42_010_954_459]
        assert flows(series) == [42_335_261_656, 0, 0, 0, -4_844_526_331, 0]
        assert model.cash == model.floor == 42_010_954_459
    returns = [r.r for r in valuation.daily_returns(series)]
    assert returns[1] == Fraction(57_571_464_309, 42_335_261_656) - 1  # +35.99 %
    assert returns[4] == Fraction(46_855_480_790, 70_717_498_876) - 1  # -33.74 %
    # Identical in A and B: B's withdrawals are end-of-day flows.
    assert valuation.time_weighted_return(series) == Fraction(46_855_480_790, 42_335_261_656) - 1
    assert drawdown(series) == 1 - Fraction(46_855_480_790, 71_612_569_316)  # 34.57 %
    # The anchor day's point (07-06) is the base: its 42,335.261656 flow is not netted
    # again. A: 46,855.480790 - 42,335.261656 = 4,520.219134; B: 42,010.954459
    # - 42,335.261656 + 4,844.526331 = 4,520.219134 (= 1,180.668692 + 3,663.857639
    # - 324.307197, the trade's realized net).
    assert pnl(series) == 4_520_219_134


def test_example_1_without_day_start_dating_loses_the_first_day():
    _, series = run(SEQ1, True, ANCHOR_1, DAYS_1, t1=False)
    assert equity(series)[0] == 0
    assert valuation.daily_returns(series)[1].r is None  # E(07-06) = 0: undefined
    twr = valuation.time_weighted_return(series)
    assert twr == Fraction(46_855_480_790, 57_571_464_309) - 1  # -18.61 % instead of +10.68 %


# ---- 2.3 worked example 2: ARB liquidation, XPL close ------------------------------


def test_example_2_a_and_b_diverge():
    a_model, a = run(SEQ1 + SEQ2, True, ANCHOR_1, DAYS_12)
    b_model, b = run(SEQ1 + SEQ2, False, ANCHOR_1, DAYS_12)
    # A: 46,855.480790 - 42.853625 - 25,484.240845 = 21,328.386320 after the liquidation;
    #    then - 63.675138 + 45,810.268018 = 67,074.979200.
    assert equity(a)[-4:] == [46_855_480_790, 21_328_386_320, 21_328_386_320, 67_074_979_200]
    assert flows(a)[-4:] == [0, 0, 0, 0]
    # B: 42,010.954459 - 42.853625 - 25,484.240845 = 16,483.859989 (below the floor);
    #    then 16,420.184851 + 45,810.268018 = 62,230.452869, of which 20,219.498410
    #    above the 42,010.954459 floor is withdrawn.
    assert equity(b)[-4:] == [42_010_954_459, 16_483_859_989, 16_483_859_989, 42_010_954_459]
    assert flows(b)[-4:] == [0, 0, 0, -20_219_498_410]
    ra, rb = valuation.daily_returns(a), valuation.daily_returns(b)
    assert ra[7].r == Fraction(21_328_386_320, 46_855_480_790) - 1  # -54.48 %
    assert rb[7].r == Fraction(16_483_859_989, 42_010_954_459) - 1  # -60.76 %
    assert ra[9].r == Fraction(67_074_979_200, 21_328_386_320) - 1  # +214.49 %
    assert rb[9].r == Fraction(62_230_452_869, 16_483_859_989) - 1  # +277.52 %
    seq1 = Fraction(46_855_480_790, 42_335_261_656)
    assert valuation.time_weighted_return(a) == Fraction(67_074_979_200, 42_335_261_656) - 1
    assert valuation.time_weighted_return(b) == seq1 * Fraction(62_230_452_869, 42_010_954_459) - 1
    assert drawdown(a) == 1 - Fraction(21_328_386_320, 71_612_569_316)  # 70.22 %
    trough_b = Fraction(46_855_480_790 * 16_483_859_989, 42_010_954_459 * 71_612_569_316)
    assert drawdown(b) == 1 - trough_b  # 74.33 %
    assert a_model.cash == 67_074_979_200 and b_model.cash == 42_010_954_459
    assert b_model.floor == 42_010_954_459  # the ETH short's 42,010.954459 stays the floor
    # The models change the returns, never the dollar P&L:
    # A: 67,074.979200 - 42,335.261656 = 24,739.717544;
    # B: 42,010.954459 - 42,335.261656 + 4,844.526331 + 20,219.498410 = 24,739.717544.
    assert pnl(a) == pnl(b) == 24_739_717_544


def test_example_3_day_start_dating_keeps_a_funded_loss():
    # Sequence 2 alone, watched flat: the ARB short is funded by a 26,523.587892 deposit
    # and liquidated the same day, leaving 996.493422.
    for retained in (True, False):
        _, series = run(SEQ2, retained, ANCHOR_2, DAYS_2)
        assert equity(series)[:2] == [26_523_587_892, 996_493_422]
        twr = valuation.time_weighted_return(series)
        assert (
            twr
            == Fraction(996_493_422, 26_523_587_892) * Fraction(50_504_253_386, 4_757_660_506) - 1
        )  # -60.12 %
        assert drawdown(series) == 1 - Fraction(996_493_422, 26_523_587_892)  # 96.24 %
        # TWR -60.12 % on a P&L of +20,219.498410 = 50,504.253386 - 26,523.587892
        # - 3,761.167084: TWR and dollar P&L disagree in sign under A and B alike.
        assert pnl(series) == 20_219_498_410
        _, late = run(SEQ2, retained, ANCHOR_2, DAYS_2, t1=False)
        assert valuation.daily_returns(late)[1].r is None  # the liquidation day vanishes
        assert (
            valuation.time_weighted_return(late)
            == Fraction(50_504_253_386 - 3_761_167_084, 996_493_422) - 1
        )  # +4,590.76 %
        assert drawdown(late) == 0


# ---- 2.8 comparison: B's withdrawals, option C ----------------------------------------


def test_b_win_then_loss_at_zero_pnl_shows_a_negative_twr():
    # Floor 100: one trade wins 50, the next (same 100 posted) loses 50. Dollar P&L 0.
    # A: r = 150/100 - 1 = +50 %, then 100/150 - 1 = -33.33 %; TWR 1.5 x 2/3 - 1 = 0.
    # B: the 50 above the floor is a synthetic WITHDRAWAL, so r = (100 + 50)/100 - 1
    #    = +50 %, then 50/100 - 1 = -50 %; TWR 1.5 x 0.5 - 1 = -25 %. The trader never
    #    withdrew anything: the wallet still holds the 50.
    usd = 1_000_000
    k = "X.00000000-S-PERP"
    seq = [
        Action(datetime(2026, 1, 2, 10, tzinfo=UTC), k, 0, "0", 100 * usd, {}),
        Action(
            datetime(2026, 1, 2, 14, tzinfo=UTC),
            k,
            0,
            "0",
            -100 * usd,
            {LegKind.REALIZED_PNL: 50 * usd},
        ),
        Action(datetime(2026, 1, 3, 10, tzinfo=UTC), k, 0, "0", 100 * usd, {}),
        Action(
            datetime(2026, 1, 3, 14, tzinfo=UTC),
            k,
            0,
            "0",
            -100 * usd,
            {LegKind.REALIZED_PNL: -50 * usd},
        ),
    ]
    anchor = datetime(2026, 1, 1, 12, tzinfo=UTC)
    days = [date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)]
    _, a = run(seq, True, anchor, days)
    _, b = run(seq, False, anchor, days)
    assert equity(a) == [100 * usd, 150 * usd, 100 * usd]
    assert equity(b) == [100 * usd, 100 * usd, 50 * usd]
    assert flows(b) == [100 * usd, -50 * usd, 0]
    assert valuation.time_weighted_return(a) == 0
    assert valuation.time_weighted_return(b) == Fraction(-1, 4)
    assert pnl(a) == pnl(b) == 0
    # Option C: the peak posting is 100 (all on day 1), so C = A here: TWR 0.
    assert equity(run_peak(seq, anchor, days)) == equity(a)


def test_option_c_peak_invested_capital():
    # Examples 1 and 2: every deposit is needed on 07-07, so C = A
    # (+10.68 %, then +58.44 % and a 70.22 % drawdown).
    c1 = run_peak(SEQ1, ANCHOR_1, DAYS_1)
    assert equity(c1) == equity(run(SEQ1, True, ANCHOR_1, DAYS_1)[1])
    assert valuation.time_weighted_return(c1) == Fraction(46_855_480_790, 42_335_261_656) - 1
    c12 = run_peak(SEQ1 + SEQ2, ANCHOR_1, DAYS_12)
    assert equity(c12) == equity(run(SEQ1 + SEQ2, True, ANCHOR_1, DAYS_12)[1])
    assert valuation.time_weighted_return(c12) == Fraction(67_074_979_200, 42_335_261_656) - 1
    assert drawdown(c12) == 1 - Fraction(21_328_386_320, 71_612_569_316)
    # Example 3: the peak is 26,523.587892 + 3,761.167084 = 30,284.754976, counted from
    # the anchor. 09-02: 30,284.754976 - 42.853625 - 25,484.240845 = 4,757.660506, so
    # r = 4,757.660506 / 30,284.754976 - 1 = -84.29 %; 10-13: 4,757.660506 - 63.675138
    # + 45,810.268018 = 50,504.253386, r = +961.54 %. TWR = P&L / peak = +66.77 %.
    c3 = run_peak(SEQ2, ANCHOR_2, DAYS_2)
    assert equity(c3) == [30_284_754_976, 4_757_660_506, 4_757_660_506, 50_504_253_386]
    assert valuation.time_weighted_return(c3) == Fraction(50_504_253_386, 30_284_754_976) - 1
    assert drawdown(c3) == 1 - Fraction(4_757_660_506, 30_284_754_976)  # 84.29 %
    assert pnl(c3) == 20_219_498_410


# ---- dK provenance (note 2.5) and research figures --------------------------------------


def test_research_position_change_chain_is_exact():
    # Address B (WETH collateral, 18 dp), 20 positionChanges over two positions: each
    # change's collateral after = the previous after +/- collateralDeltaAmount
    # (+ increase, - decrease), to the wei. 18 links (the first change of each
    # position has no predecessor in the window).
    changes = RESEARCH["address_b_position_changes"]
    assert len(changes) == 20
    last: dict[str, int] = {}
    links = 0
    for c in changes:
        after, delta = int(c["collateralAmount"]), int(c["collateralDeltaAmount"])
        key = c["positionKey"]
        if key in last:
            sign = 1 if c["type"] == "increase" else -1
            assert last[key] + sign * delta == after
            links += 1
        last[key] = after
    assert links == 18


def test_research_collateral_delta_equals_initial_collateral_delta():
    # Joined by orderKey, every change's collateralDeltaAmount equals its trade
    # action's initialCollateralDeltaAmount (6 increases, 14 decreases), while the
    # increases report non-zero fees beside it: K rose by the order's collateral, the
    # fees were cash legs, so w = dK - dc = initialCollateralDeltaAmount + fees.
    changes = RESEARCH["address_b_position_changes"]
    trades = {t["orderKey"]: t for t in RESEARCH["address_b_trades_by_order_key"]}
    kinds = []
    for c in changes:
        t = trades[c["orderKey"]]
        assert int(t["initialCollateralDeltaAmount"]) == int(c["collateralDeltaAmount"])
        kinds.append(c["type"])
        if c["type"] == "increase":
            assert int(t["positionFeeAmount"]) - int(t["traderDiscountAmount"]) > 0
    assert kinds.count("increase") == 6 and kinds.count("decrease") == 14


# ---- 4 numeric policy ---------------------------------------------------------------


def test_usd_rounding_examples():
    assert usd30(51033091798973216164946606156791229) == 51_033_091_799
    assert fixed_to_atoms("2500000000000000000000000", 30, 6, rounding=ROUND_HALF_EVEN) == 2
    assert fixed_to_atoms("3500000000000000000000000", 30, 6, rounding=ROUND_HALF_EVEN) == 4
    # Per-leg error <= 0.5 micro: 51,115.416098690473... -> 51,115.416099 (+0.309527 micro).
    exact = Fraction(51115416098690473538100540298940615, 10**24)
    assert abs(Fraction(51_115_416_099) - exact) <= Fraction(1, 2)


def test_quantity_drift_and_full_close_correction():
    deltas = [int(r["sizeDeltaInTokens"]) for r in ROWS["eth_short_increases_first4"]]
    assert deltas == [
        30401281604480431397,
        30427654947719200293,
        30458743914442201290,
        30410271210872919522,
    ]
    # Remainders below 12 dp: 431397 + 200293 + 201290 + 919522 = 1,752,502 -> one carry.
    per_fill = [trunc12(d) for d in deltas]
    exact = sum(deltas)
    assert trunc12(exact) - sum(per_fill) == 1  # drift: 1 atom (1e-12 ETH)
    # Closing the whole short with one decrease of the exact size, per-fill truncation
    # leaves the ledger at -sum(per_fill) + trunc12(exact) = +1 atom: a phantom long
    # on a -S instrument, whose size must stay <= 0.
    ledger = -sum(per_fill)
    assert ledger + trunc12(exact) == 1
    # Full-close correction: the closing FILL_BASE is -(running ledger size).
    assert ledger + (-ledger) == 0
    # Running-total rule: qty_i = trunc(S_i) - trunc(S_(i-1)); fill 4 gets one more atom
    # and the ledger always equals the venue size truncated to 12 dp.
    running, previous, qtys = 0, 0, []
    for d in deltas:
        running += d
        qtys.append(trunc12(running) - previous)
        previous = trunc12(running)
    assert qtys == [30_401_281_604_480, 30_427_654_947_719, 30_458_743_914_442, 30_410_271_210_873]
    assert sum(qtys) == trunc12(exact) == 121_697_951_677_514
    # The XPL close: 690,631.526897078958132301 -> 690,631.526897078958 (132301 dropped).
    assert trunc12(690631526897078958132301) == 690_631_526_897_078_958


def test_per_fill_truncation_drift_over_the_research_page():
    # Address A's ETH/USD [ETH-USDC] short increases of 2026-07-02 in one 300-row
    # /v1/trades page: 118 rows, 109 with a non-zero size (the others are
    # collateral-only). Per-fill truncation falls 52 atoms (52e-12 ETH) short of the
    # truncated sum; the running-position rule falls 0 short.
    rows = RESEARCH["eth_short_increases_2026_07_02"]
    assert len(rows) == 118
    sizes = [int(r["sizeDeltaInTokens"]) for r in rows if int(r["sizeDeltaInTokens"])]
    assert len(sizes) == 109
    first4 = [int(r["sizeDeltaInTokens"]) for r in ROWS["eth_short_increases_first4"]]
    assert sizes[:4] == first4
    assert trunc12(sum(sizes)) - sum(trunc12(s) for s in sizes) == 52
    running = previous = booked = 0
    for s in sizes:
        running += s
        booked += trunc12(running) - previous
        previous = trunc12(running)
    assert booked == trunc12(sum(sizes))


def test_example_1_quantities_have_no_drift():
    # 500.039352125288774211 = 166.554837072900248044 + 333.484515052388526167; the
    # dropped remainders 248044 + 526167 = 774211 do not carry.
    model, _ = run(SEQ1, True, ANCHOR_1, DAYS_1)
    assert [f.qty_delta_atoms for f in model.fills] == [
        -500_039_352_125_288,
        166_554_837_072_900,
        333_484_515_052_388,
    ]


# ---- 5 funding claims ---------------------------------------------------------------


def test_weth_claim_valued_at_the_claim_price():
    claim = ROWS["claim_funding"]
    i = 2  # WETH on ETH/USD [ETH-USDC]
    assert claim["tokenAddresses"][i] == "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    amount, price = int(claim["amounts"][i]), int(claim["tokenPrices"][i])
    # 5.728900499289178355 WETH x 1,754.326858477229 = 10,050.364015446612953... -> 10,050.364015
    assert amount * price == 10050364015446612953304033887178295
    assert usd30(amount * price) == 10_050_364_015


def test_claim_total_books_usdc_at_par_and_rounds_other_tokens_per_entry():
    claim = ROWS["claim_funding"]
    usdc = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"
    total = rounded = 0
    for token, amount, price in zip(
        claim["tokenAddresses"], claim["amounts"], claim["tokenPrices"], strict=True
    ):
        if token == usdc:
            total += int(amount)  # USDC at par: the claim's 0.99977 oracle price is ignored
        else:
            total += usd30(int(amount) * int(price))
            rounded += 1
    assert total == 46_144_662_412
    assert rounded == 8  # error bound 8 x 0.5 micro = 4 micros
