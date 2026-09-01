"""Reconciliation: snapshots, tolerance, quarantine, and the append-only guard."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import ingest, reconcile
from alphamarket.connectors.base import (
    Action,
    NormalizedBalance,
    NormalizedFill,
    NormalizedSettlement,
    Side,
)
from alphamarket.models import (
    AppendOnlyViolation,
    EquitySnapshot,
    Fill,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.money import to_micros
from alphamarket.vault import encrypt_secret

T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class StubConnector:
    venue = "stub"

    def __init__(self, balance: str):
        self.balance_micros = to_micros(balance)
        self.fills: list[NormalizedFill] = []
        self.settlements: list[NormalizedSettlement] = []

    def check_connection(self):
        return self.fetch_balance()

    def check_write_scope(self):
        return False

    def fetch_balance(self):
        return NormalizedBalance(balance_micros=self.balance_micros, as_of=datetime.now(UTC))

    def fetch_fills(self, since=None):
        return [f for f in self.fills if since is None or f.filled_at > since]

    def fetch_settlements(self, since=None):
        return [s for s in self.settlements if since is None or s.settled_at > since]

    def fetch_positions(self):
        return []


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
        anchor_balance_micros=to_micros("1000.00"),
        anchor_at=T0,
    )
    db.add(acct)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=acct.id, name="S", slug="s"))
    db.commit()
    return acct


def _stub(monkeypatch, balance: str) -> StubConnector:
    stub = StubConnector(balance)
    monkeypatch.setattr(ingest, "build_connector", lambda *a, **k: stub)
    monkeypatch.setattr(reconcile, "build_connector", lambda *a, **k: stub)
    return stub


def _buy(count, price, fee, minutes):
    return NormalizedFill(
        venue_fill_id=f"f{minutes}",
        ticker="T",
        side=Side.YES,
        action=Action.BUY,
        count_micros=to_micros(count),
        price_micros=to_micros(price),
        fee_micros=to_micros(fee),
        filled_at=T0 + timedelta(minutes=minutes),
    )


def test_tolerance_is_ten_bps_floored_at_one_cent():
    assert reconcile.tolerance_for(to_micros("1000.00")) == to_micros("1.00")
    assert reconcile.tolerance_for(to_micros("5.00")) == to_micros("0.01")


def test_reconciled_when_ledger_explains_balance(db, account, monkeypatch):
    stub = _stub(monkeypatch, "995.93")  # 1000 - (10 x 0.40 + 0.07)
    stub.fills = [_buy("10", "0.40", "0.07", 1)]

    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is True
    assert snap.computed_cash_micros == to_micros("995.93")
    assert snap.divergence_micros == 0
    strategy = db.scalars(select(Strategy)).one()
    assert strategy.status == "incubating"


def test_divergence_beyond_tolerance_quarantines(db, account, monkeypatch):
    stub = _stub(monkeypatch, "990.00")  # venue says $5.93 less than the ledger explains
    stub.fills = [_buy("10", "0.40", "0.07", 1)]

    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is False
    assert snap.divergence_micros == to_micros("-5.93")
    assert db.scalars(select(Strategy)).one().status == "quarantined"
    assert len(db.scalars(select(EquitySnapshot)).all()) == 1


def test_divergence_within_tolerance_passes(db, account, monkeypatch):
    stub = _stub(monkeypatch, "995.50")  # 43c off on a ~$1000 account (tolerance $0.9955)
    stub.fills = [_buy("10", "0.40", "0.07", 1)]
    assert reconcile.reconcile_account(db, account).reconciled is True


def test_reconcile_requires_anchor(db, account, monkeypatch):
    _stub(monkeypatch, "1000.00")
    account.anchor_balance_micros = None
    db.commit()
    with pytest.raises(ValueError, match="anchor"):
        reconcile.reconcile_account(db, account)


def test_take_anchor_waits_out_nearby_fills(monkeypatch):
    stub = StubConnector("100.00")
    now = datetime.now(UTC)
    stub.fills = [_buy("1", "0.50", "0", 0)]
    stub.fills[0] = stub.fills[0].model_copy(update={"filled_at": now})
    sleeps: list[float] = []
    monkeypatch.setattr(reconcile.time, "sleep", sleeps.append)

    anchor = reconcile.take_anchor(stub, settle_seconds=5.0, attempts=3)
    assert anchor.balance_micros == to_micros("100.00")
    assert anchor.positions == []
    assert sleeps == [5.0, 5.0]  # retried, then gave up on the last observation


def test_ledger_rows_cannot_be_updated_or_deleted(db, account):
    strategy = db.scalars(select(Strategy)).one()
    row = Fill(
        strategy_id=strategy.id,
        venue_fill_id="x",
        ticker="T",
        side="yes",
        action="buy",
        count_micros=1,
        price_micros=1,
        fee_micros=0,
        filled_at=T0,
    )
    db.add(row)
    db.commit()

    row.fee_micros = 5
    with pytest.raises(AppendOnlyViolation):
        db.commit()
    db.rollback()

    db.delete(db.get(Fill, row.id))
    with pytest.raises(AppendOnlyViolation):
        db.commit()
    db.rollback()
    assert db.get(Fill, row.id).fee_micros == 0
