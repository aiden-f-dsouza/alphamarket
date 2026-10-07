"""Benchmark the MIRROR and SIGNALS hot paths on a seeded Hyperliquid world.

    uv run python scripts/bench_mirror.py                     # Postgres (db_test, :5433)
    uv run python scripts/bench_mirror.py --sqlite            # in-memory SQLite
    uv run python scripts/bench_mirror.py -n 20 -m 6 -k 200   # allocations, months, signals

World: one creator (Hyperliquid, BTC-PERP with margin tiers) with M months of
hourly funding legs and K perp fills (each a signal), and N paper allocations
whose books already hold those K fills plus M months of hourly funding (as the
engine would have booked them). Rows are bulk-inserted, not computed through
the engine: the numbers are plausible, not meaningful. Then it times

- MIRROR, the worker's global pass (lock each allocation, process_allocation):
  cold (first pass after start) and warm (the steady state: nothing new);
- MIRROR after one new funding row and one new signal;
- SIGNALS (emit_signals for the strategy): nothing pending, and one new fill.

Postgres mode creates a throwaway database on the test server
(KOINE_TEST_PG_URL, as tests/conftest.py), migrates it and drops it after.
No network: quote capture is stubbed out (invariant 6 spirit).
"""

import argparse
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

PG_ADMIN_URL = os.environ.get(
    "KOINE_TEST_PG_URL",
    "postgresql+psycopg://alphamarket:alphamarket@localhost:5433/alphamarket_test",
)

M = 1_000_000


def _pg_database():
    from alembic import command
    from alembic.config import Config

    from alphamarket.config import get_settings

    admin = create_engine(PG_ADMIN_URL, isolation_level="AUTOCOMMIT")
    name = f"koine_bench_{uuid.uuid4().hex[:10]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    settings = get_settings()
    original = settings.database_url
    settings.database_url = url
    try:
        cfg = Config()
        cfg.set_main_option("script_location", "alembic")
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = original

    def drop():
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()

    return url, drop


def seed(db, n_alloc: int, months: int, k_signals: int, now: datetime) -> dict:
    from alphamarket.connectors.base import InstrumentType, LegKind
    from alphamarket.models import (
        Allocation,
        AnchorBalance,
        AssetFill,
        AssetQuote,
        FundingRateRow,
        Instrument,
        LedgerEntry,
        MirrorAssetFill,
        MirrorLedgerEntry,
        PriceDaily,
        Signal,
        Strategy,
        User,
        VenueAccount,
    )

    start = (now - timedelta(days=30 * months)).replace(minute=0, second=0, microsecond=0)
    hours = int((now - start).total_seconds() // 3600)
    creator = User(email="creator@bench.test", password_hash="x", display_name="c")
    db.add(creator)
    db.flush()
    acct = VenueAccount(user_id=creator.id, venue="hyperliquid", venue_env="prod", key_id="0xbench")
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=acct.id, name="b", slug="bench")
    db.add(strategy)
    inst = Instrument(
        venue="hyperliquid",
        symbol="BTC",
        type=InstrumentType.PERP,
        base_asset="BTC-PERP",
        quote_asset="USD",
        qty_decimals=5,
        margin_tiers=[{"lower_bound": "0", "max_leverage": 40}],
    )
    db.add(inst)
    db.flush()
    db.add(
        AnchorBalance(
            venue_account_id=acct.id,
            asset="USDC",
            atoms=100_000 * M,
            decimals=6,
            as_of=start - timedelta(hours=1),
        )
    )
    # Daily closes and a fresh quote.
    day = start.date()
    while day <= now.date():
        db.add(
            PriceDaily(
                instrument_id=inst.id,
                day=day,
                close="60000",
                usd_micros_per_unit=60_000 * M,
                source="hyperliquid",
            )
        )
        day += timedelta(days=1)
    db.add(
        AssetQuote(
            instrument_id=inst.id,
            bid="60000",
            ask="60001",
            as_of=now - timedelta(seconds=5),
            source="hyperliquid",
        )
    )
    # Funding rows (hourly) with a mark.
    funding_at = [start + timedelta(hours=h) for h in range(1, hours)]
    db.add_all(
        FundingRateRow(
            instrument_id=inst.id, at=t, rate="0.0000125", mark_price="60000", source="hyperliquid"
        )
        for t in funding_at
    )
    # Creator: hourly funding legs + K perp fills (buy 0.02, sell 0.01, ...).
    db.add_all(
        LedgerEntry(
            venue_account_id=acct.id,
            asset="USDC",
            delta_atoms=-15_000,
            decimals=6,
            kind=LegKind.FUNDING,
            venue_ref=f"f:{i}",
            group_ref=f"f:{i}",
            occurred_at=t,
        )
        for i, t in enumerate(funding_at)
    )
    step = (now - start) / (k_signals + 1)
    fills = []
    for i in range(k_signals):
        t = start + step * (i + 1) + timedelta(seconds=7)
        buy = i % 2 == 0
        qty = 2_000 if buy else 1_000
        fill = AssetFill(
            venue_account_id=acct.id,
            instrument_id=inst.id,
            venue_fill_id=f"x{i}",
            action="buy" if buy else "sell",
            qty_atoms=qty,
            qty_decimals=5,
            price="60000",
            filled_at=t,
            ingested_at=t + timedelta(seconds=5),
        )
        fills.append(fill)
        db.add(fill)
        db.add_all(
            [
                LedgerEntry(
                    venue_account_id=acct.id,
                    instrument_id=inst.id,
                    asset="BTC-PERP",
                    delta_atoms=qty if buy else -qty,
                    decimals=5,
                    kind=LegKind.FILL_BASE,
                    venue_ref=f"x{i}",
                    group_ref=f"x{i}",
                    occurred_at=t,
                ),
                LedgerEntry(
                    venue_account_id=acct.id,
                    instrument_id=inst.id,
                    asset="USDC",
                    delta_atoms=-600_000,
                    decimals=6,
                    kind=LegKind.FEE,
                    venue_ref=f"x{i}",
                    group_ref=f"x{i}",
                    occurred_at=t,
                ),
            ]
        )
    db.flush()
    signals = []
    position = 0
    for fill in fills:
        delta = fill.qty_atoms if fill.action == "buy" else -fill.qty_atoms
        sig = Signal(
            strategy_id=strategy.id,
            asset_fill_id=fill.id,
            instrument_id=inst.id,
            ticker="BTC",
            side="long",
            action=fill.action,
            qty_atoms=fill.qty_atoms,
            qty_decimals=5,
            price="60000",
            notional_micros=None,
            position_before_atoms=position,
            position_after_atoms=position + delta,
            creator_equity_micros=100_000 * M,
            filled_at=fill.filled_at,
            observed_at=fill.ingested_at,
            quote_bid="60000",
            quote_ask="60001",
            quote_at=fill.ingested_at,
        )
        position += delta
        signals.append(sig)
        db.add(sig)
    db.flush()
    # Followers: the same fills at 1/10 size, and every funding payment booked.
    users = [
        User(email=f"f{i}@bench.test", password_hash="x", display_name="f") for i in range(n_alloc)
    ]
    db.add_all(users)
    db.flush()
    allocations = [
        Allocation(
            user_id=u.id,
            strategy_id=strategy.id,
            allocation_micros=10_000 * M,
            drawdown_limit_bps=9000,
            high_water_micros=10_000 * M,
            started_at=start,
            last_signal_id=signals[-1].id if signals else 0,
        )
        for u in users
    ]
    db.add_all(allocations)
    db.flush()
    for alloc in allocations:
        entries = []
        for sig in signals:
            q = sig.qty_atoms // 10
            buy = sig.action == "buy"
            t = sig.observed_at
            db.add(
                MirrorAssetFill(
                    allocation_id=alloc.id,
                    signal_id=sig.id,
                    reason="signal",
                    mode="paper",
                    instrument_id=inst.id,
                    action=sig.action,
                    qty_atoms=q,
                    qty_decimals=5,
                    price="60001",
                    notional_micros=q * 600_010,
                    fee_micros=6_000,
                    price_basis="ask",
                    filled_at=t,
                )
            )
            entries += [
                MirrorLedgerEntry(
                    allocation_id=alloc.id,
                    instrument_id=inst.id,
                    asset="BTC-PERP",
                    delta_atoms=q if buy else -q,
                    decimals=5,
                    kind=LegKind.FILL_BASE,
                    ref=f"sig:{sig.id}",
                    group_ref=f"sig:{sig.id}",
                    occurred_at=t,
                ),
                MirrorLedgerEntry(
                    allocation_id=alloc.id,
                    instrument_id=None,
                    asset="USD",
                    delta_atoms=-6_000,
                    decimals=6,
                    kind=LegKind.FEE,
                    ref=f"sig:{sig.id}",
                    group_ref=f"sig:{sig.id}",
                    occurred_at=t,
                ),
            ]
        first = signals[0].observed_at if signals else now
        for t in funding_at:
            if t <= first:
                continue
            ref = f"fund:{inst.id}:{int(t.timestamp())}"
            entries.append(
                MirrorLedgerEntry(
                    allocation_id=alloc.id,
                    instrument_id=inst.id,
                    asset="USD",
                    delta_atoms=-1_500,
                    decimals=6,
                    kind=LegKind.FUNDING,
                    ref=ref,
                    group_ref=ref,
                    occurred_at=t,
                )
            )
        db.add_all(entries)
        db.flush()
    db.commit()
    return {
        "strategy_id": strategy.id,
        "account_id": acct.id,
        "instrument_id": inst.id,
        "allocation_ids": [a.id for a in allocations],
        "position": position,
        "hours": hours,
    }


def mirror_pass(factory, world) -> tuple[float, int, int]:
    """handle_mirror's loop: lock each allocation (SKIP LOCKED), process it."""
    from alphamarket.mirror import process_allocation
    from alphamarket.models import Allocation

    mirrored = applied = 0
    t0 = time.perf_counter()
    with factory() as db:
        for allocation_id in world["allocation_ids"]:
            allocation = db.scalars(
                select(Allocation)
                .where(Allocation.id == allocation_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            ).first()
            if allocation is None:
                db.rollback()
                continue
            m, a = process_allocation(db, allocation)
            mirrored += m
            applied += a
    return time.perf_counter() - t0, mirrored, applied


def signals_pass(factory, world) -> tuple[float, int]:
    from alphamarket.models import Strategy
    from alphamarket.signals import emit_signals

    t0 = time.perf_counter()
    with factory() as db:
        added = emit_signals(db, db.get(Strategy, world["strategy_id"]))
    return time.perf_counter() - t0, added


def add_new_events(factory, world, now: datetime) -> None:
    """One new funding row (next hour) and one new creator fill (no signal yet)."""
    from alphamarket.connectors.base import LegKind
    from alphamarket.models import AssetFill, FundingRateRow, LedgerEntry

    with factory() as db:
        t = now.replace(minute=0, second=0, microsecond=0)
        exists = db.scalar(
            select(FundingRateRow.id).where(
                FundingRateRow.instrument_id == world["instrument_id"], FundingRateRow.at == t
            )
        )
        if exists is None:
            db.add(
                FundingRateRow(
                    instrument_id=world["instrument_id"],
                    at=t,
                    rate="0.0000125",
                    mark_price="60000",
                    source="hyperliquid",
                )
            )
        fill_at = now - timedelta(seconds=30)
        db.add(
            AssetFill(
                venue_account_id=world["account_id"],
                instrument_id=world["instrument_id"],
                venue_fill_id="new-1",
                action="buy",
                qty_atoms=2_000,
                qty_decimals=5,
                price="60000",
                filled_at=fill_at,
                ingested_at=fill_at + timedelta(seconds=5),
            )
        )
        db.add(
            LedgerEntry(
                venue_account_id=world["account_id"],
                instrument_id=world["instrument_id"],
                asset="BTC-PERP",
                delta_atoms=2_000,
                decimals=5,
                kind=LegKind.FILL_BASE,
                venue_ref="new-1",
                group_ref="new-1",
                occurred_at=fill_at,
            )
        )
        db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", "--allocations", type=int, default=20)
    parser.add_argument("-m", "--months", type=int, default=6)
    parser.add_argument("-k", "--signals", type=int, default=200)
    parser.add_argument("--sqlite", action="store_true", help="in-memory SQLite, not Postgres")
    args = parser.parse_args()

    import logging

    logging.disable(logging.WARNING)
    from alphamarket import prices
    from alphamarket.db import Base

    prices.capture_quote = lambda *a, **k: None  # never reach a venue
    drop = None
    if args.sqlite:
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
    else:
        url, drop = _pg_database()
        engine = create_engine(url)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    try:
        now = datetime.now(UTC)
        t0 = time.perf_counter()
        with factory() as db:
            world = seed(db, args.allocations, args.months, args.signals, now)
        print(
            f"seeded {args.allocations} allocations x {world['hours']} hourly funding legs, "
            f"{args.signals} signals in {time.perf_counter() - t0:.1f}s "
            f"({'sqlite' if args.sqlite else 'postgres'})"
        )
        for label in ("MIRROR cold", "MIRROR warm", "MIRROR warm"):
            secs, m, a = mirror_pass(factory, world)
            print(f"{label:<34} {secs * 1000:9.1f} ms  (+{m} fills, +{a} lifecycle events)")
        secs, added = signals_pass(factory, world)
        print(f"{'SIGNALS nothing pending':<34} {secs * 1000:9.1f} ms  (+{added} signals)")
        add_new_events(factory, world, datetime.now(UTC))
        secs, added = signals_pass(factory, world)
        print(f"{'SIGNALS one new fill':<34} {secs * 1000:9.1f} ms  (+{added} signals)")
        secs, m, a = mirror_pass(factory, world)
        print(
            f"{'MIRROR new signal + funding row':<34} {secs * 1000:9.1f} ms  "
            f"(+{m} fills, +{a} lifecycle events)"
        )
        secs, m, a = mirror_pass(factory, world)
        print(f"{'MIRROR warm':<34} {secs * 1000:9.1f} ms  (+{m} fills, +{a} lifecycle events)")
    finally:
        engine.dispose()
        if drop is not None:
            drop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
