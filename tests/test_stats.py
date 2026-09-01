"""Stats engine pinned by hand-computed scenarios (docs/methodology.md)."""

import math
import statistics
from datetime import UTC, datetime, timedelta

import pytest

from alphamarket import stats
from alphamarket.connectors.base import Action, NormalizedFill, NormalizedSettlement, Side
from alphamarket.money import to_micros

T0 = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
ANCHOR = to_micros("1000.00")


def buy(ticker, count, price, fee, hours, side=Side.YES):
    return NormalizedFill(
        venue_fill_id=f"{ticker}-{hours}",
        ticker=ticker,
        side=side,
        action=Action.BUY,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        fee_micros=to_micros(fee),
        filled_at=T0 + timedelta(hours=hours),
    )


def sell(ticker, count, price, fee, hours, side=Side.YES):
    f = buy(ticker, count, price, fee, hours, side)
    return f.model_copy(update={"action": Action.SELL, "venue_fill_id": f"{ticker}-s{hours}"})


def settle(ticker, revenue, hours, result="yes"):
    return NormalizedSettlement(
        venue_settlement_id=f"{ticker}:{hours}",
        ticker=ticker,
        market_result=result,
        revenue_micros=to_micros(revenue),
        settled_at=T0 + timedelta(hours=hours),
    )


# Four-day scenario, every number worked by hand:
#   Aug 2 10:00  buy 10 YES KXA-1 @0.40, fee 0.07  cash 995.93 open 4.00  equity  999.93
#   Aug 3 10:00  KXA-1 settles yes, revenue 10.00  cash 1005.93 open 0    equity 1005.93  pnl +6.00
#   Aug 4 10:00  buy 20 NO  KXB-1 @0.50, fee 0.10  cash 995.83 open 10.00 equity 1005.83
#   Aug 5 10:00  KXB-1 settles yes, revenue 0      cash 995.83 open 0     equity  995.83  pnl -10.00
FILLS = [buy("KXA-1", "10", "0.40", "0.07", 34), buy("KXB-1", "20", "0.50", "0.10", 82, Side.NO)]
SETTLEMENTS = [settle("KXA-1", "10.00", 58), settle("KXB-1", "0", 106)]
AS_OF = T0 + timedelta(days=4, hours=12)
DAILY = ["1000.00", "999.93", "1005.93", "1005.83", "995.83"]
RETURNS = [999.93 / 1000 - 1, 1005.93 / 999.93 - 1, 1005.83 / 1005.93 - 1, 995.83 / 1005.83 - 1]


def test_replay_tracks_cash_open_cost_and_equity():
    rep = stats.replay(ANCHOR, T0, FILLS, SETTLEMENTS)
    assert [(p.cash_micros, p.open_cost_micros, p.equity_micros) for p in rep.points] == [
        (to_micros("995.93"), to_micros("4.00"), to_micros("999.93")),
        (to_micros("1005.93"), 0, to_micros("1005.93")),
        (to_micros("995.83"), to_micros("10.00"), to_micros("1005.83")),
        (to_micros("995.83"), 0, to_micros("995.83")),
    ]
    assert [t.pnl_micros for t in rep.realized] == [to_micros("6.00"), to_micros("-10.00")]


def test_daily_equity_is_end_of_day_utc():
    rep = stats.replay(ANCHOR, T0, FILLS, SETTLEMENTS)
    series = stats.daily_equity(rep, T0, AS_OF)
    assert [e for _, e in series] == [to_micros(v) for v in DAILY]
    assert series[0][0].isoformat() == "2026-08-01"


def test_returns_drawdown_and_total_return():
    series = [(None, to_micros(v)) for v in DAILY]
    assert stats.daily_returns(series) == pytest.approx(RETURNS)
    assert stats.total_return(series) == pytest.approx(-0.00417)
    # Peak 1005.93 -> trough 995.83 = 10.10 / 1005.93
    assert stats.max_drawdown(series) == pytest.approx(10.10 / 1005.93)


def test_sharpe_and_sortino_formulas():
    mean = statistics.mean(RETURNS)
    assert stats.sharpe(RETURNS) == pytest.approx(mean / statistics.stdev(RETURNS) * math.sqrt(365))
    downside = math.sqrt(sum(min(r, 0) ** 2 for r in RETURNS) / len(RETURNS))
    assert stats.sortino(RETURNS) == pytest.approx(mean / downside * math.sqrt(365))
    assert stats.sharpe([0.01]) is None  # too few observations
    assert stats.sharpe([0.01, 0.01]) is None  # zero variance


def test_sell_realizes_at_average_cost():
    # buy 10 @0.40 (cost 4.00), sell 4 @0.55 fee 0.03: released cost 1.60, proceeds 2.17
    fills = [buy("KXA-1", "10", "0.40", "0.07", 1), sell("KXA-1", "4", "0.55", "0.03", 2)]
    rep = stats.replay(ANCHOR, T0, fills, [])
    (trade,) = rep.realized
    assert trade.cost_micros == to_micros("1.60")
    assert trade.revenue_micros == to_micros("2.17")
    assert rep.points[-1].open_cost_micros == to_micros("2.40")


def test_compute_stats_end_to_end():
    r = stats.compute_stats(ANCHOR, T0, FILLS, SETTLEMENTS, as_of=AS_OF)
    assert r.track_days == 4
    assert (r.n_fills, r.n_settlements) == (2, 2)
    assert r.total_return == pytest.approx(-0.00417)
    assert r.win_rate == 0.5
    assert r.realized_pnl_micros == to_micros("-4.00")
    assert r.equity_micros == to_micros("995.83")
    assert r.top_ticker_share == pytest.approx(10 / 14)
    assert r.max_stake_fraction == pytest.approx(10.00 / 1005.93)
    assert r.flags == []
    # Risk score by hand: 1 + 3*(0.01004/0.30) + 3*(downside_dev*sqrt(365)/0.50)
    #   + 1.5*0.714 + 1.5*(0.00994/0.25) = 1 + 0.100 + 0.570 + 1.071 + 0.060 = 2.80 -> 3
    assert r.risk_score == 3


def test_pre_anchor_fills_are_ignored_for_positions():
    """A pre-anchor fill whose settlement never shows up in venue history must
    not sit on the books forever (this happened on the live demo account)."""
    fills = [buy("KXA-1", "10", "0.40", "0.07", -24)]
    rep = stats.replay(ANCHOR, T0, fills, [])
    assert rep.equity_at_anchor_micros == ANCHOR
    assert rep.points == []


def test_anchor_positions_seed_cost_basis():
    """Position open at connect, reported by the venue with cost 4.00, then
    settles for 10.00: realized +6.00, equity 1004.00 -> 1010.00."""
    seed = [{"ticker": "KXA-1", "position_micros": to_micros("10"), "cost_micros": to_micros("4")}]
    rep = stats.replay(ANCHOR, T0, [], [settle("KXA-1", "10.00", 5)], anchor_positions=seed)
    assert rep.equity_at_anchor_micros == to_micros("1004.00")
    assert rep.points[-1].equity_micros == to_micros("1010.00")
    assert rep.realized[0].pnl_micros == to_micros("6.00")
    # Negative position = NO side; settlement clears it too.
    seed_no = [
        {"ticker": "KXB-1", "position_micros": -to_micros("5"), "cost_micros": to_micros("2")}
    ]
    rep = stats.replay(ANCHOR, T0, [], [settle("KXB-1", "0", 5)], anchor_positions=seed_no)
    assert rep.points[-1].equity_micros == ANCHOR
    assert rep.realized[0].pnl_micros == to_micros("-2.00")


def test_martingale_flag_on_three_escalating_losses():
    fills, settlements = [], []
    for i, count in enumerate(["1", "2", "4", "8"]):
        fills.append(buy(f"KXA-{i}", count, "0.50", "0", 10 * i + 1))
        if i < 3:
            settlements.append(settle(f"KXA-{i}", "0", 10 * i + 5))
    rep = stats.replay(ANCHOR, T0, fills, settlements)
    assert stats.martingale_flag(rep) is True
    calm = stats.replay(ANCHOR, T0, fills[:2], settlements[:1])
    assert stats.martingale_flag(calm) is False


def test_stake_spike_flag():
    fills = [buy(f"KXA-{i}", "1", "0.50", "0", i + 1) for i in range(10)]
    assert stats.stake_spike_flag(stats.replay(ANCHOR, T0, fills, [])) is False
    fills.append(buy("KXA-big", "10", "0.50", "0", 20))  # 5.00 > 5 x median 0.50
    assert stats.stake_spike_flag(stats.replay(ANCHOR, T0, fills, [])) is True


def test_concentration_flag_uses_series_not_ticker():
    fills = [buy(f"KXA-{i}", "1", "0.50", "0", i + 1) for i in range(4)]
    fills.append(buy("KXB-1", "1", "0.50", "0", 9))
    rep = stats.replay(ANCHOR, T0, fills, [])
    assert stats.concentration(rep.buy_notionals) == (pytest.approx(0.2), pytest.approx(0.8))
    assert stats.concentration_flag(rep) is True


def test_risk_score_bounds():
    assert stats.risk_score(0.0, 0.0, 0.0, 0.0) == 1
    assert stats.risk_score(1.0, 5.0, 1.0, 1.0) == 10
