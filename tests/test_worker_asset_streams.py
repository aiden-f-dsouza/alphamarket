"""Integration of P1 ledger v2 with the P2 job queue: an asset venue's
backfill runs through the registered page runners (fills, then events), each
page committed with the job's cursor, and later syncs resume incrementally."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from alphamarket import jobs, worker  # noqa: F401  (worker registers the page runners)
from alphamarket.connectors.base import (
    AssetAmount,
    AssetVenueConnector,
    NormalizedAssetFill,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    Page,
)
from alphamarket.ingest import STREAM_ASSET_FILLS, STREAM_EVENTS
from alphamarket.models import (
    AccountSchedule,
    AssetFill,
    Job,
    LedgerEntry,
    Strategy,
    User,
    VenueAccount,
)

T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
BTC = NormalizedInstrument(
    symbol="XXBTZUSD", type="spot", base_asset="BTC", quote_asset="USD", qty_decimals=8
)


def _fill(n: int) -> NormalizedAssetFill:
    # Buy 0.01 BTC at 60000 USD: +1_000_000 sat-atoms, -600.0000 USD at 4 dp, fee 1.5600 USD.
    return NormalizedAssetFill(
        venue_fill_id=f"T{n}",
        instrument=BTC,
        action="buy",
        qty_atoms=1_000_000,
        price="60000",
        legs=[
            NormalizedLeg(asset="BTC", delta_atoms=1_000_000, decimals=8, kind="fill_base"),
            NormalizedLeg(asset="USD", delta_atoms=-6_000_000, decimals=4, kind="fill_quote"),
            NormalizedLeg(asset="USD", delta_atoms=-15_600, decimals=4, kind="fee"),
        ],
        filled_at=T0.replace(hour=12 + n),
    )


class TwoPageVenue(AssetVenueConnector):
    venue = "kraken"

    def __init__(self):
        self.fill_calls: list[str | None] = []

    def fetch_asset_fills_page(self, cursor=None, since=None):
        self.fill_calls.append(cursor)
        if cursor is None and since is None:
            return Page([_fill(1)], next_cursor="p2")
        if cursor == "p2":
            return Page([_fill(2)], next_cursor=None, resume_cursor="after-T2")
        return Page([], next_cursor=None, resume_cursor=cursor)  # incremental: nothing new

    def fetch_events_page(self, cursor=None, since=None):
        deposit = NormalizedEvent(
            venue_ref="L-dep-1",
            kind="deposit",
            legs=[NormalizedLeg(asset="USD", delta_atoms=10_000_000, decimals=4, kind="deposit")],
            occurred_at=T0,
        )
        return Page([deposit] if cursor is None else [], next_cursor=None, resume_cursor="ev-1")

    def fetch_balances(self):
        return NormalizedBalances(as_of=T0, assets=[AssetAmount(asset="USD", atoms=0, decimals=4)])

    def check_write_scope(self):
        return False


def _account(db) -> VenueAccount:
    user = User(email="k@example.com", password_hash="x", display_name="K")
    db.add(user)
    db.flush()
    account = VenueAccount(user_id=user.id, venue="kraken", venue_env="prod", key_id="kk")
    db.add(account)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=account.id, name="k", slug="k-1"))
    db.commit()
    return account


def test_backfill_then_incremental_sync_through_page_runners(session_factory):
    venue = TwoPageVenue()
    with session_factory() as db:
        account_id = _account(db).id
        jobs.enqueue(db, jobs.JobKind.BACKFILL, account_id=account_id)
        db.commit()
    runner = jobs.JobRunner(
        session_factory, None, worker_id="w1", connector_factory=lambda a: venue
    )
    while runner.run_once():
        pass
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AssetFill)) == 2
        # 2 fills x 3 legs + 1 deposit leg
        assert db.scalar(select(func.count()).select_from(LedgerEntry)) == 7
        sched = db.get(AccountSchedule, account_id)
        assert sched.backfill_done_at is not None
        assert sched.resume_cursors == {STREAM_ASSET_FILLS: "after-T2", STREAM_EVENTS: "ev-1"}
        assert db.scalar(select(Job.status).where(Job.kind == "backfill")) == "done"

    # The next sync resumes from the stored cursor and writes nothing twice.
    with session_factory() as db:
        jobs.enqueue(db, jobs.JobKind.SYNC, account_id=account_id)
        db.commit()
    while runner.run_once():
        pass
    assert venue.fill_calls[-1] == "after-T2"
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(LedgerEntry)) == 7
