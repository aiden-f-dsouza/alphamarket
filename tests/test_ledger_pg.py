"""Ledger v2 on real Postgres (migration 0009): append-only triggers, the
decimals CHECK, the instrument ON CONFLICT upsert and event-level
idempotency. Runs only when db_test is up."""

import importlib.util
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alphamarket import ingest
from alphamarket.connectors.base import (
    Action,
    InstrumentType,
    LegKind,
    NormalizedAssetFill,
    NormalizedFill,
    NormalizedInstrument,
    NormalizedLeg,
    Side,
)
from alphamarket.models import (
    AssetFill,
    Instrument,
    LedgerEntry,
    Strategy,
    SymbolHistory,
    User,
    VenueAccount,
)

pytestmark = pytest.mark.pg

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _account(db, venue="kalshi"):
    tag = uuid.uuid4().hex[:8]
    user = User(email=f"{tag}@example.com", password_hash="x", display_name="P")
    db.add(user)
    db.flush()
    account = VenueAccount(user_id=user.id, venue=venue, venue_env="demo", key_id=tag)
    db.add(account)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=account.id, name="P", slug=f"p-{tag}")
    db.add(strategy)
    db.commit()
    return account, strategy


def _fill(fill_id: str) -> NormalizedAssetFill:
    inst = NormalizedInstrument(
        symbol=f"PG-{fill_id}-USD",
        type=InstrumentType.SPOT,
        base_asset="PGC",
        quote_asset="USD",
        qty_decimals=8,
    )
    return NormalizedAssetFill(
        venue_fill_id=fill_id,
        instrument=inst,
        action=Action.BUY,
        qty_atoms=100_000_000,
        price="2",
        legs=[
            NormalizedLeg(asset="PGC", delta_atoms=100_000_000, decimals=8, kind=LegKind.FILL_BASE),
            NormalizedLeg(asset="USD", delta_atoms=-20_000, decimals=4, kind=LegKind.FILL_QUOTE),
        ],
        filled_at=T0,
    )


def test_ledger_v2_triggers_match_preflight(pg_session_factory):
    path = Path(__file__).parent.parent / "scripts" / "preflight.py"
    spec = importlib.util.spec_from_file_location("preflight", path)
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    with pg_session_factory() as db:
        names = set(
            db.scalars(text("SELECT tgname FROM pg_trigger WHERE tgname LIKE '%_append_only'"))
        )
    assert {"ledger_entries_append_only", "asset_fills_append_only"} <= names
    assert names >= preflight.EXPECTED_APPEND_ONLY_TRIGGERS


def test_raw_sql_cannot_mutate_legs_or_asset_fills(pg_session_factory):
    with pg_session_factory() as db:
        account, _ = _account(db, venue="kalshi")
        ingest.ingest_asset_fills(db, account, [_fill(uuid.uuid4().hex)])
        db.commit()
        for statement in (
            "UPDATE ledger_entries SET delta_atoms = 0 WHERE venue_account_id = :a",
            "DELETE FROM ledger_entries WHERE venue_account_id = :a",
            "UPDATE asset_fills SET price = '3' WHERE venue_account_id = :a",
            "DELETE FROM asset_fills WHERE venue_account_id = :a",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                db.execute(text(statement), {"a": account.id})
            db.rollback()


def test_decimals_check_rejects_thirteen(pg_session_factory):
    with pg_session_factory() as db:
        account, _ = _account(db)
        db.add(
            LedgerEntry(
                venue_account_id=account.id,
                asset="X",
                delta_atoms=1,
                decimals=13,
                kind="adjustment",
                venue_ref="r",
                group_ref="event:adjustment:r",
                occurred_at=T0,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()


def test_asset_ingest_is_idempotent_on_postgres(pg_session_factory):
    fill_id = uuid.uuid4().hex
    with pg_session_factory() as db:
        account, _ = _account(db)
        first = ingest.ingest_asset_fills(db, account, [_fill(fill_id)])
        db.commit()
        again = ingest.ingest_asset_fills(db, account, [_fill(fill_id)])
        db.commit()
        assert (first.new, again.duplicates) == (1, 1)
        legs = db.scalars(select(LedgerEntry).where(LedgerEntry.venue_ref == fill_id)).all()
        assert sorted((e.asset, e.delta_atoms) for e in legs) == [
            ("PGC", 100_000_000),
            ("USD", -20_000),
        ]
        assert db.scalars(select(AssetFill).where(AssetFill.venue_fill_id == fill_id)).one()


def test_instrument_upsert_on_conflict_keeps_one_row(pg_session_factory):
    fill = _fill(uuid.uuid4().hex)
    with pg_session_factory() as a, pg_session_factory() as b:
        # Two sessions (two workers) meet the same new symbol: b's INSERT
        # waits on a's uncommitted row, then hits ON CONFLICT DO NOTHING and
        # reads a's row instead of failing.
        ia = ingest.upsert_instrument(a, "pgvenue", fill.instrument, T0)
        found: dict = {}

        def second_worker():
            found["id"] = ingest.upsert_instrument(b, "pgvenue", fill.instrument, T0).id
            b.commit()

        worker = threading.Thread(target=second_worker)
        worker.start()
        time.sleep(0.3)  # let b block on the unique index
        a.commit()
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert ia.id == found["id"]
        rows = a.scalars(select(Instrument).where(Instrument.symbol == fill.instrument.symbol))
        assert len(rows.all()) == 1
        history = a.scalars(select(SymbolHistory).where(SymbolHistory.instrument_id == ia.id))
        assert len(history.all()) == 1


def test_binary_sync_legs_on_postgres(pg_session_factory):
    with pg_session_factory() as db:
        account, strategy = _account(db)
        from alphamarket.models import Fill

        fid = uuid.uuid4().hex
        f = NormalizedFill(
            venue_fill_id=fid,
            ticker="KX-PG",
            side=Side.NO,
            action=Action.BUY,
            count_micros=840_000,
            price_micros=570_000,
            fee_micros=14_500,
            filled_at=T0,
        )
        db.add(
            Fill(
                strategy_id=strategy.id,
                venue_fill_id=f.venue_fill_id,
                ticker=f.ticker,
                side=f.side,
                action=f.action,
                count_micros=f.count_micros,
                price_micros=f.price_micros,
                fee_micros=f.fee_micros,
                filled_at=f.filled_at,
            )
        )
        db.flush()
        assert ingest.write_binary_legs(db, account, strategy) == 1
        assert ingest.write_binary_legs(db, account, strategy) == 0
        db.commit()
        legs = db.scalars(select(LedgerEntry).where(LedgerEntry.group_ref == f"fill:{fid}"))
        # -0.84 contract (NO, netted), -0.4788 notional, -0.0145 fee.
        assert sorted((e.kind, e.delta_atoms) for e in legs) == [
            ("fee", -14_500),
            ("fill_base", -840_000),
            ("fill_quote", -478_800),
        ]
