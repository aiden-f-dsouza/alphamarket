"""Broker equity check (D16) in reconcile v2: the venue's total equity vs the
ledger's equity at marks/closes, tolerance max($1, 0.5%). A mismatch flags the
stats and never quarantines. The venue is a scripted connector; no network.
Methodology: docs/methodology.md "Broker equity check"."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import analytics_v2, prices, reconcile
from alphamarket.connectors import registry
from alphamarket.connectors.base import (
    AssetAmount,
    AssetVenueConnector,
    CredentialKind,
    InstrumentKind,
    InstrumentType,
    NormalizedBalances,
    OwnershipProof,
    Page,
    VenueProfile,
)
from alphamarket.models import (
    AnchorBalance,
    EquitySnapshot,
    Instrument,
    LedgerQuarantine,
    PriceDaily,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.money import to_atoms

M = 1_000_000
VENUE = "testeq"
PROFILE = VenueProfile(
    venue=VENUE,
    display_name="TestEq",
    envs=("prod",),
    credential=CredentialKind.KEY_SECRET,
    key_label="API key",
    secret_label="Secret",
    key_help="",
    balance_observable=True,
    instrument=InstrumentKind.ASSET,
    tick_micros=10_000,
    contract_step_micros=1,
    fee_rate_bps=0,
    fee_rounding_micros=1,
    ownership=OwnershipProof.KEY,
    asset_classes=("crypto",),
)


class FakeVenue(AssetVenueConnector):
    venue = VENUE

    def __init__(self):
        self.balances: list[AssetAmount] = []
        self.equity: int | None = None

    def check_write_scope(self):
        return False

    def fetch_balances(self):
        return NormalizedBalances(
            as_of=datetime.now(UTC), assets=self.balances, equity_usd_micros=self.equity
        )

    def fetch_asset_fills_page(self, cursor=None, since=None):
        return Page()

    def fetch_events_page(self, cursor=None, since=None):
        return Page()


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def venue(monkeypatch):
    monkeypatch.setitem(registry.PROFILES, VENUE, PROFILE)
    fake = FakeVenue()
    monkeypatch.setattr(reconcile, "connector_for", lambda account: fake)
    monkeypatch.setattr(reconcile, "sync_account", lambda db, account: None)
    return fake


def _seed(db, venue: FakeVenue, usd_micros: int, btc: str | None) -> VenueAccount:
    """An account anchored two days ago with `usd_micros` cash and `btc` BTC,
    BTC closing at 50,000 yesterday; the venue reports the same vector."""
    now = datetime.now(UTC)
    user = User(email="a@example.com", password_hash="x", display_name="A")
    db.add(user)
    db.flush()
    acct = VenueAccount(user_id=user.id, venue=VENUE, venue_env="prod", key_id="k")
    db.add(acct)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=acct.id, name="A", slug="a"))
    anchor_at = now - timedelta(days=2)
    assets = [AssetAmount(asset="USD", atoms=usd_micros, decimals=6)]
    if btc is not None:
        assets.append(AssetAmount(asset="BTC", atoms=to_atoms(btc, 8), decimals=8))
    for a in assets:
        db.add(
            AnchorBalance(
                venue_account_id=acct.id,
                asset=a.asset,
                atoms=a.atoms,
                decimals=a.decimals,
                as_of=anchor_at,
            )
        )
    inst = Instrument(
        venue=VENUE,
        symbol="BTCUSD",
        type=InstrumentType.SPOT,
        base_asset="BTC",
        quote_asset="USD",
        qty_decimals=8,
    )
    db.add(inst)
    db.flush()
    db.add(
        PriceDaily(
            instrument_id=inst.id,
            day=(now - timedelta(days=1)).date(),
            close="50000",
            usd_micros_per_unit=50_000 * M,
            source=VENUE,
        )
    )
    db.commit()
    venue.balances = assets
    return acct


@pytest.fixture()
def account(db, venue):
    """$5,000 and 0.1 BTC: the ledger's equity now is 5,000 + 0.1 x 50,000 = $10,000."""
    return _seed(db, venue, 5_000 * M, "0.1")


def _strategy(db) -> Strategy:
    return db.scalars(select(Strategy)).one()


def test_broker_equity_within_tolerance(db, account, venue):
    # 10,040 vs 10,000: divergence 40 <= max(1, 0.5% x 10,040 = 50.20).
    venue.equity = 10_040 * M
    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is True
    assert (
        snap.venue_equity_micros,
        snap.computed_equity_micros,
        snap.equity_tolerance_micros,
        snap.equity_reconciled,
    ) == (10_040 * M, 10_000 * M, 50_200_000, True)
    stats = analytics_v2.compute_asset_stats(db, _strategy(db))
    assert "broker_equity_divergence" not in stats.flags
    assert stats.extras["broker_equity"]["ok"] is True
    assert stats.extras["broker_equity"]["divergence_micros"] == 40 * M


def test_broker_equity_mismatch_flags_but_never_quarantines(db, account, venue):
    # 10,100 vs 10,000: divergence 100 > 0.5% x 10,100 = 50.50.
    venue.equity = 10_100 * M
    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is True  # the balance vector still matches
    assert (snap.equity_tolerance_micros, snap.equity_reconciled) == (50_500_000, False)
    assert _strategy(db).status == "incubating"
    assert db.scalars(select(LedgerQuarantine)).all() == []
    stats = analytics_v2.compute_asset_stats(db, _strategy(db))
    assert "broker_equity_divergence" in stats.flags
    assert stats.extras["broker_equity"] == {
        "as_of": snap.as_of.isoformat(),
        "venue_micros": 10_100 * M,
        "computed_micros": 10_000 * M,
        "divergence_micros": 100 * M,
        "tolerance_micros": 50_500_000,
        "ok": False,
    }
    assert _strategy(db).status == "incubating"


def test_the_newest_check_wins(db, account, venue):
    venue.equity = 10_100 * M
    reconcile.reconcile_account(db, account)
    venue.equity = 10_000 * M
    reconcile.reconcile_account(db, account)
    stats = analytics_v2.compute_asset_stats(db, _strategy(db))
    assert "broker_equity_divergence" not in stats.flags


@pytest.mark.parametrize(
    ("venue_equity", "ok"),
    [
        (150 * M, True),  # divergence 0.80 <= $1 (0.5% x 150 = 0.75 would refuse it)
        (150_250_000, False),  # divergence 1.05 > max($1, 0.75125)
    ],
)
def test_a_small_account_gets_the_one_dollar_floor(db, venue, venue_equity, ok):
    acct = _seed(db, venue, 149_200_000, None)  # $149.20 cash, nothing else
    venue.equity = venue_equity
    snap = reconcile.reconcile_account(db, acct)
    assert snap.computed_equity_micros == 149_200_000
    assert (snap.equity_tolerance_micros, snap.equity_reconciled) == (M, ok)
    assert snap.reconciled is True and _strategy(db).status == "incubating"


def test_no_venue_equity_leaves_the_columns_null(db, account, venue):
    venue.equity = None
    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is True
    assert (
        snap.venue_equity_micros,
        snap.computed_equity_micros,
        snap.equity_tolerance_micros,
        snap.equity_reconciled,
    ) == (None, None, None, None)
    assert analytics_v2.compute_asset_stats(db, _strategy(db)).extras["broker_equity"] is None


def test_the_computed_side_uses_a_fresh_mark(db, account, venue, monkeypatch):
    # Mark 50,400 (fresh quote): 5,000 + 0.1 x 50,400 = 10,040 = the venue's figure.
    inst = db.scalars(select(Instrument)).one()

    def mark(db_, instrument_id, now):
        assert instrument_id == inst.id
        return prices.Mark(instrument_id, "50400", now, VENUE, "mid")

    monkeypatch.setattr(prices, "latest_mark", mark)
    venue.equity = 10_040 * M
    snap = reconcile.reconcile_account(db, account)
    assert (snap.computed_equity_micros, snap.equity_reconciled) == (10_040 * M, True)


def test_a_vector_divergence_still_quarantines(db, account, venue):
    # The M2/v2 vector check is unchanged: venue BTC 0.09 vs ledger 0.1.
    venue.balances[1] = AssetAmount(asset="BTC", atoms=to_atoms("0.09", 8), decimals=8)
    venue.equity = 9_500 * M
    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is False and _strategy(db).status == "quarantined"
    assert snap.equity_reconciled is False
    reasons = [q.ref for q in db.scalars(select(LedgerQuarantine))]
    assert reasons == ["reconcile:BTC"]  # the equity mismatch adds no quarantine
    assert db.scalars(select(EquitySnapshot)).one().id == snap.id
