"""The seeded demo's Koine-run strategy and private watch (scripts/seed_demo.py),
smoke-run end to end at the `small` scale on in-memory SQLite (the Postgres
twin is in test_seed_demo_pg.py). Invariant 7: every seeded user is `is_demo`,
the system user included."""

import importlib.util
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alphamarket.analytics import latest_stats
from alphamarket.config import get_settings
from alphamarket.connectors import registry
from alphamarket.db import Base
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    EquitySnapshot,
    Follow,
    MirrorAssetFill,
    Strategy,
    StrategyKind,
    User,
    VenueAccount,
    Watch,
)
from alphamarket.public import list_visible

SCRIPTS = Path(__file__).parent.parent / "scripts"
spec = importlib.util.spec_from_file_location("seed_demo", SCRIPTS / "seed_demo.py")
seed_demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seed_demo)
pf_spec = importlib.util.spec_from_file_location("preflight", SCRIPTS / "preflight.py")
preflight = importlib.util.module_from_spec(pf_spec)
pf_spec.loader.exec_module(preflight)


def test_watch_address_is_well_formed_and_synthetic():
    assert re.fullmatch(r"0x[0-9a-f]{40}", seed_demo.WATCH_ADDRESS)
    assert seed_demo.WATCH_ADDRESS == "0x" + "de30" * 10  # not a generated or real address
    watch = [c for c in seed_demo.CREATORS if c.watch]
    # Hyperliquid's is first; each on-chain venue adds one in its marked block.
    assert [c.venue for c in watch][0] == "hyperliquid"
    assert {c.venue for c in watch} <= set(registry.PROFILES)
    assert [c.handle for c in seed_demo.CREATORS if c.house] == ["koinelab"]
    # Every watch and tail names a seeded handle.
    handles = {c.handle for c in seed_demo.CREATORS}
    assert {h for _, h, _ in seed_demo.WATCHES} <= handles
    assert {h for _, h, *_ in seed_demo.ALLOCATIONS} <= handles
    assert len({label for _, _, label in seed_demo.WATCHES}) == len(seed_demo.WATCHES)
    # No one follows a watch: it has no public page to follow.
    assert not {h for _, hs in seed_demo.FOLLOWS for h in hs} & {c.handle for c in watch}


@pytest.fixture()
def seeded():
    settings = get_settings()
    settings.demo_mode = True
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sf = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    end = datetime(2026, 10, 4, 12, tzinfo=UTC)
    summary = seed_demo.seed(sf, scale=seed_demo.SMALL, end=end, log=lambda _line: None)
    yield sf, summary
    settings.demo_mode = False
    engine.dispose()


def test_small_seed_has_a_house_strategy_and_a_private_watch(seeded):
    sf, summary = seeded
    assert summary.failed_jobs == []
    with sf() as db:
        system = db.scalars(select(User).where(User.is_system.is_(True))).all()
        assert [u.email for u in system] == [f"system@{seed_demo.EMAIL_DOMAIN}"]
        assert system[0].is_demo and system[0].password_hash == "!"

        house = db.scalars(select(User).where(User.is_house.is_(True))).all()
        assert [u.email for u in house] == [f"koinelab@{seed_demo.EMAIL_DOMAIN}"]
        assert house[0].is_demo
        # Preflight counts the system user as a demo user: all of them are.
        total = db.scalar(select(func.count(User.id)))
        assert db.scalar(select(func.count(User.id)).where(User.is_demo.is_(True))) == total
        assert f"{total} demo user(s)" in preflight.demo_problems(db.connection())[0]
        rows = {r["slug"]: r for r in list_visible(db, include_incubating=True)}
        lab = rows[seed_demo.slugify("Systematic BTC/ETH trend filter")]
        assert lab["operator"] == "koine"
        assert {r["operator"] for slug, r in rows.items() if slug != lab["slug"]} == {"creator"}

        watched = db.scalars(
            select(Strategy)
            .join(VenueAccount, VenueAccount.id == Strategy.venue_account_id)
            .where(Strategy.kind == StrategyKind.WATCH, VenueAccount.venue == "hyperliquid")
        ).all()
        assert len(watched) == 1
        strategy = watched[0]
        assert strategy.user_id == system[0].id and strategy.public is False
        assert strategy.slug == f"watch-{strategy.id}"
        assert strategy.name == "Hyperliquid 0xde30…de30"
        assert strategy.slug not in rows  # never on a public surface
        account = db.get(VenueAccount, strategy.venue_account_id)
        assert account.is_watch and account.key_id == seed_demo.WATCH_ADDRESS
        assert account.claimed_at is None and account.claim_code is None
        stats = latest_stats(db, strategy.id)
        assert stats is not None and stats.track_days >= seed_demo.SMALL.min_history - 1
        snap = db.scalar(
            select(EquitySnapshot)
            .where(EquitySnapshot.strategy_id == strategy.id)
            .order_by(EquitySnapshot.as_of.desc())
            .limit(1)
        )
        assert snap is not None and snap.reconciled
        assert not db.scalars(select(Follow).where(Follow.strategy_id == strategy.id)).all()

        investor = db.scalar(select(User).where(User.email == "investor@demo.koine"))
        analyst = db.scalar(select(User).where(User.email == "analyst@demo.koine"))
        labels = {
            w.user_id: w.label
            for w in db.scalars(select(Watch).where(Watch.strategy_id == strategy.id)).all()
        }
        assert labels == {
            investor.id: "Funding-aware perp whale",
            analyst.id: "BTC momentum book to study",
        }
        tail = db.scalar(
            select(Allocation).where(
                Allocation.user_id == investor.id, Allocation.strategy_id == strategy.id
            )
        )
        assert tail is not None and tail.status == AllocationStatus.ACTIVE
        assert db.scalars(
            select(MirrorAssetFill).where(MirrorAssetFill.allocation_id == tail.id)
        ).all()


def test_seed_stores_leaderboard_snapshots_with_the_watched_address(seeded):
    from alphamarket import discover
    from alphamarket.models import LeaderboardSnapshot

    sf, _ = seeded
    with sf() as db:
        snaps = db.scalars(
            select(LeaderboardSnapshot).where(LeaderboardSnapshot.venue == "hyperliquid")
        ).all()
        assert {(s.venue, s.period) for s in snaps} == {
            (v, p) for v in ("hyperliquid", "polymarket") for p in seed_demo.LEADERBOARD_PERIODS
        }
        for s in snaps:
            assert len(s.rows) == 24
            assert all(re.fullmatch(r"0x[0-9a-f]{40}", r["address"]) for r in s.rows)
            if s.venue == "hyperliquid":
                assert seed_demo.WATCH_ADDRESS in {r["address"] for r in s.rows}
            else:  # wallet stats merged in, no roi
                assert s.rows[0]["roi"] is None and s.rows[0]["markets_traded"] is not None
        investor = db.scalar(select(User).where(User.email == f"investor@{seed_demo.EMAIL_DOMAIN}"))
        settings = get_settings()
        settings.watch_discover_enabled = True
        try:
            view = discover.discover_view(db, investor, "hyperliquid", "week", "pnl")
        finally:
            settings.watch_discover_enabled = False
        states = {r.address: r.state for r in view.rows}
        assert states[seed_demo.WATCH_ADDRESS] == discover.RowState.WATCHING
        assert sum(s == discover.RowState.ADD for s in states.values()) == 23
