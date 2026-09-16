"""Signals and the paper mirroring engine (M6). Every expected number below
is hand-computed; the formulas are on the methodology page."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import mirror, portfolio, signals
from alphamarket.execution import LiveMirroringUnavailable, get_executor
from alphamarket.models import (
    Allocation,
    AllocationSnapshot,
    AppendOnlyViolation,
    Fill,
    MirrorFill,
    MirrorSettlement,
    Settlement,
    Signal,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.money import to_micros

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
H = timedelta(hours=1)
TICKER = "KXTEST-26AUG01-A"


# ---- fee model -------------------------------------------------------------------


def test_paper_fee_matches_recorded_live_fill():
    # tests/fixtures/kalshi_fills.json: 101.89 contracts at 0.98, fee_cost "0.139800"
    assert mirror.paper_fee_micros(to_micros("101.89"), to_micros("0.98")) == 139_800


def test_paper_fee_hand_computed():
    # 0.07 × 1 × 0.5 × 0.5 = 0.0175
    assert mirror.paper_fee_micros(1_000_000, 500_000) == 17_500
    # 0.07 × 10 × 0.4 × 0.6 = 0.168
    assert mirror.paper_fee_micros(10_000_000, 400_000) == 168_000
    # 0.07 × 0.4 × 0.5 × 0.5 = 0.007
    assert mirror.paper_fee_micros(400_000, 500_000) == 7_000
    assert mirror.paper_fee_micros(0, 500_000) == 0


def test_affordable_count_caps_at_cash_including_fee():
    # $1 of cash, price 0.50: each contract costs 0.50 + fee 0.0175 = 0.5175,
    # so at most 1.9323 -> 1.93 contracts (0.965 + 0.0338 = 0.9988 fits;
    # 1.94 would cost 0.970 + 0.0339 = 1.0039).
    assert mirror.affordable_count(1_000_000, 500_000, 5_000_000) == 1_930_000
    assert mirror.affordable_count(0, 500_000, 5_000_000) == 0


# ---- fixtures ------------------------------------------------------------------


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def creator(db):
    user = User(email="c@example.com", password_hash="x", display_name="C")
    db.add(user)
    db.flush()
    acct = VenueAccount(
        user_id=user.id,
        venue="kalshi",
        venue_env="prod",
        key_id="k",
        anchor_balance_micros=to_micros("1000.00"),
        anchor_at=T0,
        claimed_at=T0,
    )
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=acct.id, name="S", slug="s")
    db.add(strategy)
    db.commit()
    return strategy


def _fill(strategy, fid, at, action, count, price, fee):
    return Fill(
        strategy_id=strategy.id,
        venue_fill_id=fid,
        ticker=TICKER,
        side="yes",
        action=action,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        fee_micros=to_micros(fee),
        filled_at=at,
    )


def _allocation(db, strategy, amount="100.00", started_at=T0 + timedelta(minutes=30), dd=1500):
    follower = User(email=f"f{amount}@example.com", password_hash="x", display_name="F")
    db.add(follower)
    db.flush()
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=to_micros(amount),
        drawdown_limit_bps=dd,
        high_water_micros=to_micros(amount),
        started_at=started_at,
    )
    db.add(alloc)
    db.commit()
    return alloc


# ---- signals ---------------------------------------------------------------------


def test_signals_carry_equity_before_and_position_after(db, creator):
    db.add(_fill(creator, "pre", T0 - H, "buy", "5", "0.30", "0"))  # pre-anchor: ignored
    db.add(_fill(creator, "a", T0 + H, "buy", "10", "0.40", "0.168"))
    db.add(_fill(creator, "b", T0 + 2 * H, "sell", "4", "0.50", "0.07"))
    db.commit()
    assert signals.emit_signals(db, creator) == 2
    assert signals.emit_signals(db, creator) == 0  # idempotent
    a, b = db.scalars(select(Signal).order_by(Signal.id)).all()
    assert a.creator_equity_micros == to_micros("1000.00") and a.position_after_micros == 10_000_000
    # After fill a: cash 1000 - 4.168 = 995.832, open cost 4.00 -> equity 999.832
    assert b.creator_equity_micros == to_micros("999.832") and b.position_after_micros == 6_000_000
    assert b.action == "sell"


# ---- sizing --------------------------------------------------------------------


def _signal(action, count, price, equity, after):
    return Signal(
        strategy_id=1,
        fill_id=1,
        ticker=TICKER,
        side="yes",
        action=action,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        creator_equity_micros=to_micros(equity),
        position_after_micros=to_micros(after),
        filled_at=T0,
    )


def test_size_buy_is_proportional_to_equity():
    s = _signal("buy", "10", "0.40", "1000", "10")
    # 10 × 100 / 1000 = 1 contract
    assert mirror.size_buy(s, to_micros("100"), to_micros("100")).count_micros == 1_000_000
    # 10 × 37.5 / 1000 = 0.375 -> floored to 0.37
    assert mirror.size_buy(s, to_micros("37.5"), to_micros("37.5")).count_micros == 370_000
    assert mirror.size_buy(s, to_micros("0.5"), to_micros("0.5")).reason == "below_minimum_size"
    assert mirror.size_buy(s, to_micros("100"), 0).reason == "insufficient_cash"
    assert mirror.size_buy(_signal("buy", "1", "0.5", "0", "1"), 1, 1).reason == (
        "creator_equity_unknown"
    )


def test_size_sell_uses_creator_fraction_and_sells_out():
    # creator sold 4 of 10 -> follower sells 40% of 1.0 = 0.4
    s = _signal("sell", "4", "0.50", "999.832", "6")
    assert mirror.size_sell(s, 1_000_000).count_micros == 400_000
    # creator sold out -> follower sells everything, whatever the ratio
    s = _signal("sell", "6", "0.50", "999", "0")
    assert mirror.size_sell(s, 730_000).count_micros == 730_000
    assert mirror.size_sell(s, 0).reason == "no_position"


# ---- end to end ------------------------------------------------------------------


def test_paper_mirror_end_to_end(db, creator):
    db.add(_fill(creator, "a", T0 + H, "buy", "10", "0.40", "0.168"))
    db.add(_fill(creator, "b", T0 + 2 * H, "sell", "4", "0.50", "0.07"))
    db.commit()
    signals.emit_signals(db, creator)
    alloc = _allocation(db, creator, "100.00")

    assert mirror.mirror_pending(db, alloc) == 2
    fa, fb = db.scalars(select(MirrorFill).order_by(MirrorFill.id)).all()
    assert (fa.action, fa.count_micros, fa.price_micros, fa.fee_micros) == (
        "buy",
        1_000_000,
        400_000,
        16_800,
    )
    assert (fb.action, fb.count_micros, fb.price_micros, fb.fee_micros) == (
        "sell",
        400_000,
        500_000,
        7_000,
    )
    assert fa.mode == "paper" and fa.venue_order_id is None
    assert mirror.mirror_pending(db, alloc) == 0  # nothing new

    # cash = 100 - (0.40 + 0.0168) + (0.20 - 0.007) = 99.7762; open 0.6 @ cost 0.24
    v = portfolio.value_allocation(db, alloc, marks={})
    assert v.cash_micros == to_micros("99.7762")
    assert v.open_cost_micros == 240_000 and v.unmarked_cost_micros == 240_000
    assert [(p.side, p.count_micros) for p in v.positions] == [("yes", 600_000)]
    # marked at a YES mid of 0.70: 0.6 × 0.70 = 0.42
    v = portfolio.value_allocation(db, alloc, marks={TICKER: 700_000})
    assert v.open_value_micros == 420_000 and v.unmarked_cost_micros == 0
    assert v.equity_micros == to_micros("100.1962")

    # creator settlement (yes) -> follower's 0.6 contracts pay 0.60
    db.add(
        Settlement(
            strategy_id=creator.id,
            venue_settlement_id=f"{TICKER}:x",
            ticker=TICKER,
            market_result="yes",
            yes_count_micros=6_000_000,
            revenue_micros=6_000_000,
            settled_at=T0 + 3 * H,
        )
    )
    db.commit()
    assert mirror.settle_pending(db, alloc) == 1
    assert mirror.settle_pending(db, alloc) == 0
    ms = db.scalars(select(MirrorSettlement)).one()
    assert ms.revenue_micros == 600_000 and ms.market_result == "yes"
    v = portfolio.value_allocation(db, alloc, marks={})
    assert v.cash_micros == to_micros("100.3762") and v.positions == []
    view = portfolio.allocation_view(db, alloc)
    assert view["equity"] == pytest.approx(100.3762)
    assert view["return"] == pytest.approx(0.003762)
    assert view["n_fills"] == 2 and view["n_settlements"] == 1 and view["paper"]


def test_signals_before_start_are_skipped(db, creator):
    db.add(_fill(creator, "a", T0 + H, "buy", "10", "0.40", "0.168"))
    db.commit()
    signals.emit_signals(db, creator)
    alloc = _allocation(db, creator, "100.00", started_at=T0 + 2 * H)
    assert mirror.mirror_pending(db, alloc) == 0
    assert alloc.last_signal_id == db.scalar(select(Signal.id))
    assert db.scalars(select(MirrorFill)).all() == []


def test_settles_from_public_market_when_creator_has_no_settlement(db, creator):
    db.add(_fill(creator, "a", T0 + H, "buy", "10", "0.40", "0.168"))
    db.commit()
    signals.emit_signals(db, creator)
    alloc = _allocation(db, creator, "100.00")
    mirror.mirror_pending(db, alloc)
    from alphamarket.models import MarketPrice

    db.add(
        MarketPrice(
            venue="kalshi",
            venue_env="prod",
            ticker=TICKER,
            status="finalized",
            yes_bid_micros=0,
            yes_ask_micros=0,
            last_price_micros=0,
            result="no",
            as_of=T0 + 5 * H,
        )
    )
    db.commit()
    assert mirror.settle_pending(db, alloc) == 1
    ms = db.scalars(select(MirrorSettlement)).one()
    assert ms.revenue_micros == 0 and ms.yes_count_micros == 1_000_000


def test_drawdown_limit_stops_allocation(db, creator):
    db.add(_fill(creator, "a", T0 + H, "buy", "500", "0.40", "8.4"))
    db.commit()
    signals.emit_signals(db, creator)
    alloc = _allocation(db, creator, "100.00", dd=1000)
    mirror.mirror_pending(db, alloc)
    (mf,) = db.scalars(select(MirrorFill)).all()
    assert mf.count_micros == 50_000_000  # 500 × 100 / 1000
    # cash 100 - 20 - 0.84 = 79.16; marked at 0.05: 50 × 0.05 = 2.50 -> equity 81.66
    snap = portfolio.snapshot_allocation(db, alloc, marks={TICKER: 50_000})
    assert snap.equity_micros == to_micros("81.66")
    assert alloc.status == "stopped" and alloc.stop_reason == "drawdown_limit"
    assert alloc.high_water_micros == to_micros("100.00")
    # Stopped: no new mirrors, but valuation continues.
    db.add(_fill(creator, "b", T0 + 2 * H, "buy", "10", "0.40", "0.168"))
    db.commit()
    signals.emit_signals(db, creator)
    assert mirror.mirror_pending(db, alloc) == 0
    portfolio.snapshot_allocation(db, alloc, marks={TICKER: 900_000})
    assert len(db.scalars(select(AllocationSnapshot)).all()) == 2


def test_live_execution_is_refused():
    with pytest.raises(LiveMirroringUnavailable):
        get_executor("live")
    assert get_executor("paper").mode == "paper"


def test_signals_and_mirror_rows_are_append_only(db, creator):
    db.add(_fill(creator, "a", T0 + H, "buy", "10", "0.40", "0.168"))
    db.commit()
    signals.emit_signals(db, creator)
    sig = db.scalars(select(Signal)).one()
    sig.count_micros = 1
    with pytest.raises(AppendOnlyViolation):
        db.commit()
    db.rollback()
    alloc = _allocation(db, creator, "100.00")
    mirror.mirror_pending(db, alloc)
    mf = db.scalars(select(MirrorFill)).one()
    db.delete(mf)
    with pytest.raises(AppendOnlyViolation):
        db.commit()
    db.rollback()


def test_paper_fee_uses_the_venue_rate_and_tick():
    """Polymarket charges 5% x size x p x (1-p): the captured buy of 10 tokens
    at $0.15 cost $1.56375, i.e. a $0.06375 fee. Kalshi's rate is 7%."""
    from alphamarket.connectors.registry import get_profile

    poly = get_profile("polymarket")
    assert mirror.paper_fee_micros(10_000_000, 150_000, 500, poly.fee_rounding_micros) == 63_750
    assert mirror.paper_fee_micros(10_000_000, 150_000) == 89_300  # Kalshi: 0.08925 -> $0.0893
    # Paper fills snap to the venue's tick so price x count is exact.
    assert mirror.round_to_tick(469_606, poly.tick_micros) == 470_000
    assert mirror.round_to_tick(469_606, get_profile("kalshi").tick_micros) == 470_000
    assert mirror.round_to_tick(464_999, 1_000) == 465_000
    executed = get_executor("paper").execute(
        None, _signal("buy", "10", "0.469606", "100", "10"), 10_000_000, poly
    )
    assert executed.price_micros == 470_000
    assert executed.fee_micros == mirror.paper_fee_micros(10_000_000, 470_000, 500, 1)
    assert executed.fee_micros == 124_550  # 5% x 10 x 0.47 x 0.53
