"""Ledger math pinned by hand-computed scenarios (CLAUDE.md conventions)."""

from datetime import UTC, datetime, timedelta

from alphamarket.connectors.base import Action, NormalizedFill, NormalizedSettlement, Side
from alphamarket.ledger import build_cash_curve, cash_at, fill_cash_flow
from alphamarket.money import to_micros

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
ANCHOR = to_micros("1000.000000")


def fill(action, count, price, fee, minutes, side=Side.YES, fill_id=None):
    return NormalizedFill(
        venue_fill_id=fill_id or f"f{minutes}",
        ticker="T",
        side=side,
        action=action,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        fee_micros=to_micros(fee),
        filled_at=T0 + timedelta(minutes=minutes),
    )


def settlement(revenue, minutes):
    return NormalizedSettlement(
        venue_settlement_id=f"s{minutes}",
        ticker="T",
        market_result="yes",
        revenue_micros=to_micros(revenue),
        settled_at=T0 + timedelta(minutes=minutes),
    )


def test_buy_debits_notional_plus_fee():
    # 10 contracts at $0.40 = $4.00, plus $0.07 fee -> -$4.07
    assert fill_cash_flow(fill(Action.BUY, "10", "0.40", "0.07", 1)) == to_micros("-4.07")


def test_sell_credits_notional_minus_fee():
    # 4 contracts at $0.55 = $2.20, minus $0.03 fee -> +$2.17
    assert fill_cash_flow(fill(Action.SELL, "4", "0.55", "0.03", 2)) == to_micros("2.17")


def test_fractional_contracts_and_subcent_fees_are_exact():
    """The real demo fill: 101.89 x $0.98 + $0.1398 = $99.992 exactly."""
    assert fill_cash_flow(fill(Action.BUY, "101.89", "0.9800", "0.139800", 1)) == to_micros(
        "-99.992"
    )


def test_cash_curve_hand_computed():
    fills = [
        fill(Action.BUY, "10", "0.40", "0.07", 1),  # 1000.00 - 4.07 = 995.93
        fill(Action.SELL, "4", "0.55", "0.03", 2),  # 995.93 + 2.17 = 998.10
    ]
    settlements = [settlement("6.00", 60)]  # 6 remaining YES win $1 each -> 1004.10

    curve = build_cash_curve(ANCHOR, T0, fills, settlements)
    assert [e.cash_after_micros for e in curve] == [
        to_micros("995.93"),
        to_micros("998.10"),
        to_micros("1004.10"),
    ]
    assert [e.kind for e in curve] == ["fill", "fill", "settlement"]
    assert cash_at(ANCHOR, T0, fills, settlements) == to_micros("1004.10")
    assert cash_at(ANCHOR, T0, fills, settlements, as_of=T0 + timedelta(minutes=30)) == to_micros(
        "998.10"
    )


def test_events_at_or_before_anchor_are_excluded():
    """The anchor balance already reflects them; counting again double-books."""
    fills = [fill(Action.BUY, "1", "0.50", "0", -5), fill(Action.BUY, "1", "0.50", "0", 0)]
    assert build_cash_curve(ANCHOR, T0, fills, []) == []
    assert cash_at(ANCHOR, T0, fills, []) == ANCHOR


def test_settlement_of_pre_anchor_position_is_real_cash():
    """Position opened before connect, settled after: only the revenue counts."""
    fills = [fill(Action.BUY, "10", "0.40", "0.07", -60)]
    settlements = [settlement("10.00", 30)]
    assert cash_at(ANCHOR, T0, fills, settlements) == to_micros("1010.00")


def test_same_timestamp_orders_fill_before_settlement():
    fills = [fill(Action.BUY, "1", "0.50", "0", 5)]
    settlements = [settlement("1.00", 5)]
    curve = build_cash_curve(ANCHOR, T0, fills, settlements)
    assert [e.kind for e in curve] == ["fill", "settlement"]


def test_live_demo_account_reconciles():
    """Real numbers from the demo account, 2026-09-01: balance 1101.8980 before
    a 0.84 NO @ 0.57 buy (fee 0.0145) that lost at settlement -> 1101.4047."""
    anchor = to_micros("1101.8980")
    fills = [fill(Action.BUY, "0.84", "0.5700", "0.014500", 10, side=Side.NO)]
    settlements = [settlement("0", 16)]
    assert cash_at(anchor, T0, fills, settlements) == to_micros("1101.4047")


def test_reported_notional_overrides_price_times_count():
    """Polymarket: 21.294467 tokens at a 10-dp average price. The venue says
    $10.265189 left the wallet; price x count in micros is 9.999999 and the
    fee is the remainder, so cash matches the venue to the micro."""
    f = NormalizedFill(
        venue_fill_id="p",
        ticker="0xabc",
        side=Side.YES,
        action=Action.BUY,
        count_micros=21_294_467,
        price_micros=469_606,
        fee_micros=265_190,
        filled_at=datetime(2026, 9, 5, tzinfo=UTC),
        notional_micros=9_999_999,
    )
    assert fill_cash_flow(f) == -10_265_189
    plain = f.model_copy(update={"notional_micros": None, "count_micros": 21_000_000})
    assert fill_cash_flow(plain) == -(21_000_000 * 469_606 // 1_000_000 + 265_190)
