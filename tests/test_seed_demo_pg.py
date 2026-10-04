"""scripts/seed_demo.py end to end on real Postgres, at the reduced `small`
scale: synthetic venues through the real pipeline (job runner, ingest,
signals v2, paper engine, prices, reconcile, stats), then invariant 7 on the
result.

The seed gets a database of its own (named like a demo database, as the
script insists) rather than the shared session database: its demo users and
eight strategies would otherwise leak into every later pg test's global
sweeps (stats, mirror, prices)."""

import importlib.util
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from alphamarket.analytics import latest_stats
from alphamarket.config import get_settings
from alphamarket.models import Allocation, EquitySnapshot, MirrorAssetFill, Strategy, User
from alphamarket.portfolio import portfolio_summary
from alphamarket.public import list_visible, visible_strategy_or_404
from conftest import PG_ADMIN_URL

SCRIPTS = Path(__file__).parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_demo = _load("seed_demo")
preflight = _load("preflight")


@pytest.fixture()
def demo_on():
    settings = get_settings()
    settings.demo_mode = True
    yield
    settings.demo_mode = False


@pytest.fixture()
def demo_db():
    admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"koine_t_demo_{uuid.uuid4().hex[:10]}"
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError:
        admin.dispose()
        pytest.skip("Postgres test DB not reachable (docker compose up -d db_test)")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    seed_demo.migrate(url)
    engine = create_engine(url)
    yield url, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


@pytest.mark.pg
def test_small_seed_through_the_real_pipeline(demo_db, demo_on):
    url, sf = demo_db
    seed_demo.check_target(sf, url)  # DEMO_MODE on, a demo name, no real users
    summary = seed_demo.seed(sf, scale=seed_demo.SMALL, log=lambda _line: None)
    assert summary.failed_jobs == []

    with sf() as db:
        strategies = db.scalars(select(Strategy)).all()
        assert len(strategies) == len(seed_demo.CREATORS)
        venues = {s.slug: s for s in strategies}
        listed = {r["slug"]: r for r in list_visible(db, include_incubating=True)}
        assert set(listed) == set(venues)  # every seeded strategy is public in DEMO_MODE
        assert all(r["demo"] for r in listed.values())  # ... as sample data
        assert {r["venue"] for r in listed.values()} >= {
            "kraken",
            "hyperliquid",
            "coinbase",
            "alpaca",
            "ibkr",
            "kalshi",
        }
        for strategy in strategies:
            stats = latest_stats(db, strategy.id)
            assert stats is not None and stats.track_days >= seed_demo.SMALL.min_history - 1
            snap = db.scalar(
                select(EquitySnapshot)
                .where(EquitySnapshot.strategy_id == strategy.id)
                .order_by(EquitySnapshot.as_of.desc())
                .limit(1)
            )
            assert snap is not None and snap.reconciled, strategy.slug  # ledger == venue
        assert any(
            (latest_stats(db, s.id).extras or {}).get("valuation") == "v2" for s in strategies
        )

        investor = db.scalar(select(User).where(User.email == f"investor@{seed_demo.EMAIL_DOMAIN}"))
        assert investor.is_demo
        summary_p = portfolio_summary(db, investor)
        assert summary_p["value"] > 0 and len(summary_p["allocations"]) >= 4
        assert all(len(v["curve"]) >= 2 for v in summary_p["allocations"])  # nightly snapshots
        ids = [a.id for a in db.scalars(select(Allocation)).all()]
        copied = db.scalars(
            select(MirrorAssetFill).where(MirrorAssetFill.allocation_id.in_(ids))
        ).all()
        assert copied  # signals v2 -> paper fills
        assert {f.price_basis for f in copied} <= {"ask", "bid", "creator_price_penalty"}

        # Invariant 7: without DEMO_MODE nothing seeded is public.
        get_settings().demo_mode = False
        assert list_visible(db, include_incubating=True) == []
        with pytest.raises(HTTPException):
            visible_strategy_or_404(db, strategies[0].slug)
        get_settings().demo_mode = True

    engine = create_engine(url)
    with engine.connect() as conn:
        problems = preflight.demo_problems(conn)
    engine.dispose()
    assert problems and "demo user" in problems[0]
    assert seed_demo.already_seeded(sf)


@pytest.mark.pg
def test_seed_refuses_anything_that_could_be_real(demo_db, demo_on):
    url, sf = demo_db
    with pytest.raises(seed_demo.Refused):
        seed_demo.check_target(sf, url.rsplit("/", 1)[0] + "/alphamarket")  # not a demo name
    get_settings().demo_mode = False
    with pytest.raises(seed_demo.Refused):
        seed_demo.check_target(sf, url)  # DEMO_MODE off
    get_settings().demo_mode = True
    with sf() as db:
        db.add(User(email="real@example.com", password_hash="x", display_name="Real"))
        db.commit()
    with pytest.raises(seed_demo.Refused):
        seed_demo.check_target(sf, url)  # a real user lives here


def test_world_is_deterministic():
    end = datetime(2026, 10, 4, 12, tzinfo=UTC)

    def fingerprint(seed: int):
        world = seed_demo.World(end, seed_demo.SMALL, seed)
        world.generate()
        return [
            (h, v.at, v.item.model_dump_json())
            for h, book in sorted(world.by_handle.items())
            for v in book.fills + book.events + book.settlements
        ]

    first = fingerprint(7)
    assert first and first == fingerprint(7)
    assert first != fingerprint(8)
