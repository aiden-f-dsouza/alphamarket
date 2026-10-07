"""Invariant 4 for the private watchlist (docs/copy_strategies_plan.md): a
watched address's strategy (kind "watch") never reaches a public surface,
even when every other visibility field says "public" and it has stats,
settlements and a follower. The system user that owns watch accounts never
logs in."""

from datetime import UTC, datetime, timedelta

import pytest

from alphamarket.follows import feed_items
from alphamarket.models import (
    Follow,
    Settlement,
    Strategy,
    StrategyKind,
    StrategyStats,
    StrategyStatus,
    User,
    VenueAccount,
)
from alphamarket.public import is_visible, list_visible, recent_public_settlements

M = 1_000_000
NOW = datetime.now(UTC)
NAME = "Hyperliquid 0x12ab...89cd"


def _watched(db, *, status: str = StrategyStatus.LIVE) -> Strategy:
    """A watched strategy with every other field set the public way: public,
    claimed, 120 days of stats, a settlement. Only `kind` keeps it private."""
    system = User(
        email="system@koine.invalid", password_hash="!", display_name="Koine", is_system=True
    )
    db.add(system)
    db.flush()
    anchor = NOW - timedelta(days=120)
    account = VenueAccount(
        user_id=system.id,
        venue="polymarket",
        venue_env="prod",
        key_id="0x12ab000000000000000000000000000000089cd",
        anchor_balance_micros=1_000 * M,
        anchor_at=anchor,
        claimed_at=anchor,  # never set for a real watch; here to prove `kind` alone hides it
        is_watch=True,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=system.id,
        venue_account_id=account.id,
        name=NAME,
        slug=f"watch-{account.id}",
        kind=StrategyKind.WATCH,
        public=True,
        status=status,
    )
    db.add(strategy)
    db.flush()
    days = [(anchor + timedelta(days=n)).date().isoformat() for n in range(120)]
    db.add(
        StrategyStats(
            strategy_id=strategy.id,
            as_of=NOW,
            track_days=120,
            n_fills=4,
            n_settlements=1,
            total_return=0.12,
            max_drawdown=0.05,
            risk_score=3,
            equity_micros=1_120 * M,
            realized_pnl_micros=120 * M,
            flags=[],
            extras={"daily_equity": [[d, (1_000 + n) * M] for n, d in enumerate(days)]},
        )
    )
    db.add(
        Settlement(
            strategy_id=strategy.id,
            venue_settlement_id="set-watch",
            ticker="WATCHED-MARKET",
            market_result="yes",
            yes_count_micros=10 * M,
            revenue_micros=10 * M,
            settled_at=NOW - timedelta(days=1),
        )
    )
    fan = User(email="fan@example.com", password_hash="x", display_name="Fan")
    db.add(fan)
    db.flush()
    db.add(Follow(user_id=fan.id, strategy_id=strategy.id))
    db.commit()
    return strategy


@pytest.fixture()
def watched(session_factory):
    with session_factory() as db:
        return _watched(db)


def test_watch_kind_is_never_visible(session_factory, watched):
    with session_factory() as db:
        strategy = db.get(Strategy, watched.id)
        account = db.get(VenueAccount, strategy.venue_account_id)
        assert is_visible(strategy, account) is False
        assert list_visible(db, include_incubating=True) == []
        assert recent_public_settlements(db) == []
        assert feed_items(db, [strategy]) == []


@pytest.mark.parametrize(
    "path",
    [
        "/s/{slug}",
        "/s/{slug}/tail",
        "/api/strategies/{slug}",
        "/api/strategies/{slug}/signals",
    ],
)
def test_watched_strategy_routes_are_404(client, watched, path):
    assert client.get(path.format(slug=watched.slug)).status_code == 404


@pytest.mark.parametrize(
    "path", ["/", "/marketplace", "/marketplace?view=table&incubating=1", "/transparency"]
)
def test_watched_strategy_is_absent_from_listings(client, watched, path):
    html = client.get(path).text
    assert NAME not in html and watched.slug not in html and "WATCHED-MARKET" not in html


def test_a_quarantined_watch_is_not_counted_as_removed(client, session_factory):
    with session_factory() as db:
        _watched(db, status=StrategyStatus.QUARANTINED)
    from alphamarket.pages import _transparency

    with session_factory() as db:
        assert _transparency(db)["removed"] == 0


def test_cannot_follow_a_watched_strategy(client, watched):
    client.post(
        "/signup-form",
        data={"email": "u@example.com", "password": "hunter2hunter2", "display_name": "U"},
        follow_redirects=False,
    )
    assert client.post(f"/api/follows/{watched.slug}").status_code == 404


def test_the_system_user_never_logs_in(client, session_factory):
    """Its real address (system@koine.invalid) fails email validation outright;
    the is_system guard is tested here with an address that would pass."""
    from alphamarket.auth import hash_password

    with session_factory() as db:
        # Even with a usable hash (it never has one), login refuses it.
        db.add(
            User(
                email="system@example.com",
                password_hash=hash_password("hunter2hunter2"),
                display_name="Koine",
                is_system=True,
            )
        )
        db.commit()
    resp = client.post(
        "/auth/login", json={"email": "system@example.com", "password": "hunter2hunter2"}
    )
    assert resp.status_code == 401
    resp = client.post(
        "/login-form",
        data={"email": "system@example.com", "password": "hunter2hunter2"},
        follow_redirects=False,
    )
    assert resp.status_code != 303
