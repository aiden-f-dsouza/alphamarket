"""Lighter in the static demo (demo.discover_sample) and in the seeded demo
(scripts/seed_demo.py's venue-lighter blocks): synthetic addresses only,
written through the real money and address paths (invariant 7)."""

import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alphamarket import demo
from alphamarket.analytics import latest_stats
from alphamarket.config import get_settings
from alphamarket.db import Base
from alphamarket.models import (
    EquitySnapshot,
    LeaderboardSnapshot,
    Strategy,
    StrategyKind,
    VenueAccount,
    Watch,
)

SCRIPTS = Path(__file__).parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("seed_demo_lighter", SCRIPTS / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_demo)

LIGHTER_WATCH = "0x" + "11de" * 10


def test_static_demo_has_a_lighter_discover_tab():
    for period in ("day", "week", "month", "all"):
        view = demo.discover_sample("lighter", period, "pnl")
        assert view.venue == "lighter" and "lighter" in view.venues
        assert len(view.rows) == 15
        assert all(re.fullmatch(r"0x[0-9a-f]{40}", r.address) for r in view.rows)
        pnl = [r.pnl_micros for r in view.rows]
        assert pnl == sorted(pnl, reverse=True)


def test_seed_declares_one_synthetic_lighter_watch():
    (spec_,) = (c for c in seed_demo.CREATORS if c.venue == "lighter")
    assert spec_.watch and spec_.kind == "perp" and spec_.key_id == LIGHTER_WATCH
    assert "lighter" in seed_demo.PERP_VENUES
    assert ("investor", spec_.handle, "Lighter BTC/ETH desk") in seed_demo.WATCHES
    board = seed_demo.LEADERBOARD_SEEDS["lighter"]
    addresses = [board(i) for i in range(24)]
    assert addresses[2] == LIGHTER_WATCH and len(set(addresses)) == 24
    assert all(re.fullmatch(r"0x[0-9a-f]{40}", a) for a in addresses)


def test_small_seed_watches_the_lighter_address_through_the_pipeline():
    settings = get_settings()
    settings.demo_mode = True
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        end = datetime(2026, 10, 4, 12, tzinfo=UTC)
        summary = seed_demo.seed(sf, scale=seed_demo.SMALL, end=end, log=lambda _line: None)
        assert summary.failed_jobs == []
        with sf() as db:
            account = db.scalar(select(VenueAccount).where(VenueAccount.venue == "lighter"))
            assert account.is_watch and account.key_id == LIGHTER_WATCH
            strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
            assert strategy.kind == StrategyKind.WATCH and strategy.public is False
            assert strategy.name == "Lighter 0x11de…11de"
            assert latest_stats(db, strategy.id) is not None
            snap = db.scalar(
                select(EquitySnapshot)
                .where(EquitySnapshot.strategy_id == strategy.id)
                .order_by(EquitySnapshot.as_of.desc())
                .limit(1)
            )
            assert snap is not None and snap.reconciled
            labels = [
                w.label for w in db.scalars(select(Watch).where(Watch.strategy_id == strategy.id))
            ]
            assert labels == ["Lighter BTC/ETH desk"]
            boards = db.scalars(
                select(LeaderboardSnapshot).where(LeaderboardSnapshot.venue == "lighter")
            ).all()
            assert {b.period for b in boards} == set(seed_demo.LEADERBOARD_PERIODS)
            assert all(LIGHTER_WATCH in {r["address"] for r in b.rows} for b in boards)
    finally:
        settings.demo_mode = False
        engine.dispose()


def test_preflight_keeps_lighter_out_of_prod_until_counsel_answers():
    """docs/venues/lighter.md carries "Position: PENDING COUNSEL" in its terms
    section (counsel Q1), so WATCH_VENUES with lighter is refused (S9)."""
    from types import SimpleNamespace

    pf_spec = importlib.util.spec_from_file_location("preflight_l", SCRIPTS / "preflight.py")
    preflight = importlib.util.module_from_spec(pf_spec)
    pf_spec.loader.exec_module(preflight)
    doc = (SCRIPTS.parent / "docs" / "venues" / "lighter.md").read_text(encoding="utf-8")
    assert "Position: PENDING COUNSEL" in preflight.tos_position(doc)
    problems = preflight.watch_venue_problems(SimpleNamespace(watch_venues="hyperliquid,lighter"))
    assert len(problems) == 1 and "lighter" in problems[0]
    assert preflight.watch_venue_problems(SimpleNamespace(watch_venues="hyperliquid")) == []
