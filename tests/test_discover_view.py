"""discover.discover_view and snapshot_row (copy strategies step 7): what
/watch/discover shows from a stored leaderboard snapshot. No venue calls."""

from datetime import UTC, datetime

from alphamarket import discover
from alphamarket.config import get_settings
from alphamarket.connectors.base import LeaderboardEntry
from alphamarket.models import (
    Strategy,
    StrategyKind,
    User,
    VenueAccount,
    Watch,
    WatchOptout,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


def addr(n: int) -> str:
    return "0x" + f"{n:040x}"


def row(n: int, pnl: str, roi: str | None, rank: int) -> dict:
    return discover.snapshot_row(
        LeaderboardEntry(address=addr(n).upper().replace("0X", "0x"), rank=rank, pnl=pnl, roi=roi)
    )


def _user(db, email: str, **kw) -> User:
    u = User(email=email, password_hash="!", display_name=email.split("@")[0], **kw)
    db.add(u)
    db.flush()
    return u


def _watch(db, system: User, user: User, n: int) -> Strategy:
    account = VenueAccount(
        user_id=system.id, venue="hyperliquid", venue_env="prod", key_id=addr(n), is_watch=True
    )
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=system.id,
        venue_account_id=account.id,
        name="w",
        slug=f"watch-{n}",
        kind=StrategyKind.WATCH,
        public=False,
    )
    db.add(strategy)
    db.flush()
    db.add(Watch(user_id=user.id, strategy_id=strategy.id, label=""))
    db.flush()
    return strategy


def test_snapshot_row_is_integer_micros_and_lowercased():
    r = discover.snapshot_row(
        LeaderboardEntry(
            address="0xABCdef0000000000000000000000000000000001",
            rank=3,
            pnl="1234.567891",
            roi="0.25",
            volume="10",
            account_value=None,
        )
    )
    assert r == {
        "address": "0xabcdef0000000000000000000000000000000001",
        "rank": 3,
        "pnl_micros": 1_234_567_891,
        "roi": "0.25",
        "volume_micros": 10_000_000,
        "account_value_micros": None,
    }


def test_view_sorts_filters_and_marks_states(session_factory):
    with session_factory() as db:
        system = _user(db, "system@koine.invalid", is_system=True)
        me = _user(db, "me@example.com")
        watched = _watch(db, system, me, 2)
        db.add(WatchOptout(venue="hyperliquid", address=addr(4), proved_by_user_id=me.id))
        discover.store_snapshot(
            db,
            "hyperliquid",
            "week",
            [
                row(1, "100", "0.10", 3),
                row(2, "300", "0.05", 1),
                row(3, "200", "0.50", 2),
                row(4, "999", "9.99", 4),  # opted out: never shown
                row(5, "50", None, 5),
            ],
            NOW,
        )
        db.commit()

        view = discover.discover_view(db, me, "hyperliquid", "week", "pnl")
        assert [r.address for r in view.rows] == [addr(2), addr(3), addr(1), addr(5)]
        assert view.rows[0].state == discover.RowState.WATCHING
        assert view.rows[0].watch_strategy_id == watched.id
        assert view.rows[1].state == discover.RowState.ADD
        assert view.rows[0].pnl_micros == 300_000_000
        assert view.fetched_at == NOW
        assert view.watch_count == 1

        by_roi = discover.discover_view(db, me, "hyperliquid", "week", "roi")
        assert [r.address for r in by_roi.rows] == [addr(3), addr(1), addr(2), addr(5)]
        assert by_roi.rows[0].roi == 0.5
        assert by_roi.rows[-1].roi is None


def test_view_full_watchlist_and_bad_query_fallbacks(session_factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "watch_max_per_user", 1)
    with session_factory() as db:
        system = _user(db, "system@koine.invalid", is_system=True)
        me = _user(db, "me@example.com")
        _watch(db, system, me, 9)
        discover.store_snapshot(db, "hyperliquid", "day", [row(1, "1", "0.01", 1)], NOW)
        db.commit()

        view = discover.discover_view(db, me, "nope", "decade", "vibes")
        assert (view.venue, view.period, view.sort) == ("hyperliquid", "day", "pnl")
        assert view.rows[0].state == discover.RowState.FULL


def test_view_with_no_snapshot_and_no_venues(session_factory, monkeypatch):
    with session_factory() as db:
        me = _user(db, "me@example.com")
        db.commit()
        view = discover.discover_view(db, me, "hyperliquid", "month")
        assert view.venue == "hyperliquid" and view.fetched_at is None and view.rows == []

        monkeypatch.setattr(get_settings(), "watch_venues", "")
        empty = discover.discover_view(db, me)
        assert empty.venue is None and empty.venues == () and empty.rows == []
