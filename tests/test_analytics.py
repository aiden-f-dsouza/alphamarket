"""strategy_stats persistence (M3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket.analytics import compute_strategy_stats, latest_stats
from alphamarket.models import Fill, Settlement, Strategy, StrategyStats, User, VenueAccount
from alphamarket.money import to_micros

T0 = datetime(2026, 8, 1, tzinfo=UTC)


@pytest.fixture()
def db(session_factory):
    with session_factory() as s:
        yield s


@pytest.fixture()
def strategy(db):
    user = User(email="t@example.com", password_hash="x", display_name="T")
    db.add(user)
    db.flush()
    acct = VenueAccount(
        user_id=user.id,
        venue="kalshi",
        venue_env="demo",
        key_id="k",
        anchor_balance_micros=to_micros("1000.00"),
        anchor_at=T0,
    )
    db.add(acct)
    db.flush()
    strat = Strategy(user_id=user.id, venue_account_id=acct.id, name="S", slug="s")
    db.add(strat)
    db.flush()
    db.add(
        Fill(
            strategy_id=strat.id,
            venue_fill_id="f1",
            ticker="KXA-1",
            side="yes",
            action="buy",
            count_micros=to_micros("10"),
            price_micros=to_micros("0.40"),
            fee_micros=to_micros("0.07"),
            filled_at=T0 + timedelta(hours=34),
        )
    )
    db.add(
        Settlement(
            strategy_id=strat.id,
            venue_settlement_id="s1",
            ticker="KXA-1",
            market_result="yes",
            revenue_micros=to_micros("10.00"),
            settled_at=T0 + timedelta(hours=58),
        )
    )
    db.commit()
    return strat


def test_compute_and_read_latest(db, strategy):
    row = compute_strategy_stats(db, strategy, as_of=T0 + timedelta(days=3))
    assert row.track_days == 3
    assert row.realized_pnl_micros == to_micros("6.00")
    assert row.equity_micros == to_micros("1005.93")
    assert row.win_rate == 1.0
    assert row.flags == []
    assert row.extras["daily_equity"][0] == ["2026-08-01", to_micros("1000.00")]

    later = compute_strategy_stats(db, strategy, as_of=T0 + timedelta(days=4))
    assert latest_stats(db, strategy.id).id == later.id
    assert len(db.scalars(select(StrategyStats)).all()) == 2


def test_requires_anchor(db, strategy):
    acct = db.get(VenueAccount, strategy.venue_account_id)
    acct.anchor_balance_micros = None
    db.commit()
    with pytest.raises(ValueError, match="anchored"):
        compute_strategy_stats(db, strategy)
