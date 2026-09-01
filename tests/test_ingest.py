"""Ingestion service (M1.3): idempotent, append-only, cursor-driven."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import ingest
from alphamarket.connectors.base import (
    Action,
    NormalizedBalance,
    NormalizedFill,
    NormalizedSettlement,
    Side,
)
from alphamarket.models import Fill, Settlement, Strategy, User, VenueAccount
from alphamarket.vault import encrypt_secret

T0 = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


def _fill(fill_id: str, at: datetime) -> NormalizedFill:
    return NormalizedFill(
        venue_fill_id=fill_id,
        ticker="KXBTCD-TEST",
        side=Side.YES,
        action=Action.BUY,
        count_micros=1_000_000,
        price_micros=500_000,
        fee_micros=7_000,
        filled_at=at,
    )


def _settlement(sid: str, at: datetime) -> NormalizedSettlement:
    return NormalizedSettlement(
        venue_settlement_id=sid,
        ticker="KXBTCD-TEST",
        market_result="yes",
        yes_count_micros=1_000_000,
        revenue_micros=1_000_000,
        fee_micros=0,
        settled_at=at,
    )


class StubConnector:
    """Scripted venue: returns whatever the test loads into it and records
    the `since` cursors it was called with."""

    venue = "stub"

    def __init__(self):
        self.fills: list[NormalizedFill] = []
        self.settlements: list[NormalizedSettlement] = []
        self.fill_cursors: list[datetime | None] = []

    def check_connection(self) -> NormalizedBalance:
        return NormalizedBalance(balance_micros=0, as_of=T0)

    def check_write_scope(self) -> bool | None:
        return False

    def fetch_fills(self, since=None):
        self.fill_cursors.append(since)
        return [f for f in self.fills if since is None or f.filled_at > since]

    def fetch_settlements(self, since=None):
        return [s for s in self.settlements if since is None or s.settled_at > since]

    def fetch_positions(self):
        return []

    def fetch_balance(self) -> NormalizedBalance:
        return self.check_connection()


@pytest.fixture()
def stub(monkeypatch):
    connector = StubConnector()
    monkeypatch.setattr(ingest, "build_connector", lambda *a, **k: connector)
    return connector


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def account(db):
    user = User(email="t@example.com", password_hash="x", display_name="T")
    db.add(user)
    db.flush()
    acct = VenueAccount(
        user_id=user.id,
        venue="kalshi",
        venue_env="demo",
        key_id="k",
        encrypted_private_key=encrypt_secret("fake-pem"),
    )
    db.add(acct)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=acct.id, name="S", slug="s"))
    db.commit()
    return acct


def test_sync_appends_and_is_idempotent(db, account, stub):
    stub.fills = [_fill("f1", T0), _fill("f2", T0 + timedelta(minutes=1))]
    stub.settlements = [_settlement("s1", T0 + timedelta(hours=1))]

    first = ingest.sync_account(db, account)
    assert (first.new_fills, first.new_settlements) == (2, 1)

    # Re-running against identical venue data must add nothing.
    again = ingest.sync_account(db, account)
    assert (again.new_fills, again.new_settlements) == (0, 0)
    assert len(db.scalars(select(Fill)).all()) == 2
    assert len(db.scalars(select(Settlement)).all()) == 1
    assert account.last_synced_at is not None
    assert account.status == "active"


def test_sync_fetches_incrementally_with_overlap(db, account, stub):
    stub.fills = [_fill("f1", T0)]
    ingest.sync_account(db, account)
    assert stub.fill_cursors == [None]  # first sync = full history

    stub.fills.append(_fill("f2", T0 + timedelta(minutes=30)))
    result = ingest.sync_account(db, account)
    assert result.new_fills == 1
    # Second sync asks from newest stored fill minus the overlap window.
    assert stub.fill_cursors[1] == T0 - ingest.OVERLAP


def test_sync_failure_marks_account_and_reraises(db, account, stub):
    def boom(since=None):
        raise RuntimeError("venue exploded")

    stub.fetch_fills = boom
    with pytest.raises(RuntimeError):
        ingest.sync_account(db, account)
    assert account.status == "error"
    assert "venue exploded" in account.last_sync_error

    # Recovery on the next successful tick.
    stub.fetch_fills = lambda since=None: []
    ingest.sync_account(db, account)
    assert account.status == "active"
    assert account.last_sync_error is None


def test_corrections_are_new_rows_not_updates(db, account, stub):
    """A venue-side amendment arrives under a new venue_fill_id; the original
    row is untouched (invariant #3)."""
    stub.fills = [_fill("f1", T0)]
    ingest.sync_account(db, account)

    corrected = _fill("f1-amended", T0)
    stub.fills.append(corrected)
    ingest.sync_account(db, account)

    rows = db.scalars(select(Fill).order_by(Fill.id)).all()
    assert [r.venue_fill_id for r in rows] == ["f1", "f1-amended"]
