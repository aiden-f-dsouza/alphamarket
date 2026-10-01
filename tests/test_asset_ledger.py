"""Ledger v2 on an asset venue: instruments, asset fills, events, event-level
idempotency and quarantine, resumable pages, reconcile v2 and asset anchors.
The venue is a scripted AssetVenueConnector registered under a test profile;
no network (invariant 6). Amounts are hand-computed in the comments."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import ingest, reconcile
from alphamarket.connectors import registry
from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetVenueConnector,
    CredentialKind,
    InstrumentKind,
    InstrumentType,
    LegKind,
    NormalizedAssetFill,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    OwnershipProof,
    Page,
    RateLimited,
    VenueProfile,
)
from alphamarket.models import (
    AnchorBalance,
    AppendOnlyViolation,
    AssetFill,
    AssetReconciliation,
    Instrument,
    LedgerEntry,
    LedgerQuarantine,
    LedgerSyncState,
    Strategy,
    SymbolHistory,
    User,
    VenueAccount,
)
from alphamarket.money import to_atoms
from alphamarket.vault import encrypt_secret

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
VENUE = "testx"

PROFILE = VenueProfile(
    venue=VENUE,
    display_name="TestX",
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
)

BTC_USD = NormalizedInstrument(
    symbol="XBTUSD",
    type=InstrumentType.SPOT,
    base_asset="BTC",
    quote_asset="USD",
    qty_decimals=8,
    qty_step_atoms=1,
)


def usd(value: str) -> int:
    return to_atoms(value, 4)  # the venue reports USD at 4 dp (like Kraken ZUSD)


def btc(value: str) -> int:
    return to_atoms(value, 8)


def buy_btc(fill_id: str, qty: str, price: str, fee: str, minutes: int = 1) -> NormalizedAssetFill:
    """Spot buy: +qty BTC, -(qty x price) USD, -fee USD."""
    notional = to_atoms(qty, 8) * to_atoms(price, 4) // 10**8  # at 4 dp, exact for these inputs
    legs = [
        NormalizedLeg(asset="BTC", delta_atoms=btc(qty), decimals=8, kind=LegKind.FILL_BASE),
        NormalizedLeg(asset="USD", delta_atoms=-notional, decimals=4, kind=LegKind.FILL_QUOTE),
        NormalizedLeg(asset="USD", delta_atoms=-usd(fee), decimals=4, kind=LegKind.FEE),
    ]
    return NormalizedAssetFill(
        venue_fill_id=fill_id,
        order_id=f"o-{fill_id}",
        instrument=BTC_USD,
        action=Action.BUY,
        qty_atoms=btc(qty),
        price=price,
        legs=legs,
        filled_at=T0 + timedelta(minutes=minutes),
        liquidity="taker",
    )


def deposit(ref: str, amount: str, minutes: int = 0, balance_after: str | None = None):
    return NormalizedEvent(
        venue_ref=ref,
        kind=LegKind.DEPOSIT,
        legs=[
            NormalizedLeg(
                asset="USD",
                delta_atoms=usd(amount),
                decimals=4,
                kind=LegKind.DEPOSIT,
                balance_after_atoms=usd(balance_after) if balance_after else None,
            )
        ],
        occurred_at=T0 + timedelta(minutes=minutes),
    )


class FakeVenue(AssetVenueConnector):
    venue = VENUE

    def __init__(self):
        self.fill_pages: dict[str | None, Page] = {None: Page()}
        self.event_pages: dict[str | None, Page] = {None: Page()}
        self.balances: list[AssetAmount] = []
        self.calls: list[tuple[str, str | None, datetime | None]] = []

    def check_write_scope(self):
        return False

    def fetch_balances(self):
        return NormalizedBalances(as_of=datetime.now(UTC), assets=self.balances)

    def fetch_asset_fills_page(self, cursor=None, since=None):
        self.calls.append(("fills", cursor, since))
        page = self.fill_pages[cursor]
        if isinstance(page, Exception):
            raise page
        return page

    def fetch_events_page(self, cursor=None, since=None):
        self.calls.append(("events", cursor, since))
        return self.event_pages[cursor]


@pytest.fixture()
def venue(monkeypatch):
    monkeypatch.setitem(registry.PROFILES, VENUE, PROFILE)
    fake = FakeVenue()
    monkeypatch.setattr(ingest, "build_connector", lambda *a, **k: fake)
    return fake


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def account(db, venue):
    user = User(email="a@example.com", password_hash="x", display_name="A")
    db.add(user)
    db.flush()
    acct = VenueAccount(
        user_id=user.id,
        venue=VENUE,
        venue_env="prod",
        key_id="k",
        encrypted_private_key=encrypt_secret("secret"),
        anchor_balance_micros=0,
        anchor_at=T0,
    )
    db.add(acct)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=acct.id, name="A", slug="a"))
    db.commit()
    return acct


def legs_of(db, group_ref):
    rows = db.scalars(
        select(LedgerEntry).where(LedgerEntry.group_ref == group_ref).order_by(LedgerEntry.id)
    ).all()
    return [(r.kind, r.asset, r.delta_atoms, r.decimals) for r in rows]


def strategy_status(db):
    return db.scalars(select(Strategy)).one().status


# ---- asset fills ---------------------------------------------------------------


def test_asset_fill_writes_row_legs_and_instrument(db, account):
    # 0.5 BTC @ 60000.0000 = 30000.0000 USD (300_000_000 at 4 dp); fee 30.0000.
    result = ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    db.commit()
    assert (result.new, result.duplicates, result.quarantined) == (1, 0, [])
    assert legs_of(db, "fill:t1") == [
        ("fill_base", "BTC", 50_000_000, 8),
        ("fill_quote", "USD", -300_000_000, 4),
        ("fee", "USD", -300_000, 4),
    ]
    inst = db.scalars(select(Instrument)).one()
    assert (inst.venue, inst.symbol, inst.type, inst.base_asset, inst.qty_decimals) == (
        VENUE,
        "XBTUSD",
        "spot",
        "BTC",
        8,
    )
    assert db.scalars(select(SymbolHistory)).one().symbol == "XBTUSD"
    row = db.scalars(select(AssetFill)).one()
    assert (row.qty_atoms, row.qty_decimals, row.price, row.action) == (
        50_000_000,
        8,
        "60000",
        "buy",
    )
    assert all(e.instrument_id == inst.id for e in db.scalars(select(LedgerEntry)))


def test_same_fill_again_is_a_duplicate_not_a_row(db, account):
    ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    again = ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    assert (again.new, again.duplicates) == (0, 1)
    assert len(db.scalars(select(LedgerEntry)).all()) == 3
    assert len(db.scalars(select(Instrument)).all()) == 1


def test_duplicate_at_a_finer_scale_still_matches(db, account):
    """The venue now reports the fee at 8 dp: 30.0000 == 30.00000000."""
    ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    finer = buy_btc("t1", "0.5", "60000", "30")
    finer.legs[2] = NormalizedLeg(
        asset="USD", delta_atoms=-3_000_000_000, decimals=8, kind=LegKind.FEE
    )
    assert ingest.ingest_asset_fills(db, account, [finer]).duplicates == 1


def test_mismatching_duplicate_quarantines_and_writes_nothing(db, account):
    ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    changed = ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "31")])
    db.commit()
    assert changed.new == 0 and len(changed.quarantined) == 1
    assert "fee USD" in changed.quarantined[0]
    assert strategy_status(db) == "quarantined"
    q = db.scalars(select(LedgerQuarantine)).one()
    assert q.ref == "fill:t1"
    assert legs_of(db, "fill:t1")[2] == ("fee", "USD", -300_000, 4)  # untouched


def test_changed_fill_fields_under_the_same_id_quarantine(db, account):
    ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    other = buy_btc("t1", "0.5", "60000", "30").model_copy(update={"price": "60001"})
    assert ingest.ingest_asset_fills(db, account, [other]).quarantined


def test_partial_duplicate_quarantines(db, account):
    """Legs of fill t2 exist without its asset_fills row (e.g. written by an
    older code path): all-or-nothing means the event is not re-written."""
    db.add(
        LedgerEntry(
            venue_account_id=account.id,
            asset="BTC",
            delta_atoms=1,
            decimals=8,
            kind="fill_base",
            venue_ref="t2",
            group_ref="fill:t2",
            occurred_at=T0,
        )
    )
    db.flush()
    result = ingest.ingest_asset_fills(db, account, [buy_btc("t2", "0.00000001", "1", "0")])
    assert result.new == 0
    assert "partial duplicate" in result.quarantined[0]


def test_zero_delta_legs_are_dropped(db, account):
    ingest.ingest_asset_fills(db, account, [buy_btc("t3", "0.1", "50000", "0")])
    # 0.1 x 50000 = 5000.0000 -> -50_000_000; the zero fee leg is not stored.
    assert legs_of(db, "fill:t3") == [
        ("fill_base", "BTC", 10_000_000, 8),
        ("fill_quote", "USD", -50_000_000, 4),
    ]


def test_options_are_rejected(db, account):
    option = buy_btc("t4", "1", "10", "0")
    option = option.model_copy(
        update={"instrument": BTC_USD.model_copy(update={"type": InstrumentType.OPTION})}
    )
    result = ingest.ingest_asset_fills(db, account, [option])
    assert (result.new, result.rejected) == (0, 1)
    assert db.scalars(select(LedgerEntry)).all() == []


def test_instrument_follows_the_venues_lot_size(db, account):
    ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    later = buy_btc("t5", "0.5", "60000", "30").model_copy(
        update={"instrument": BTC_USD.model_copy(update={"qty_step_atoms": 1000})}
    )
    ingest.ingest_asset_fills(db, account, [later])
    assert db.scalars(select(Instrument)).one().qty_step_atoms == 1000


def test_asset_ledger_rows_are_append_only(db, account):
    ingest.ingest_asset_fills(db, account, [buy_btc("t1", "0.5", "60000", "30")])
    db.commit()
    entry = db.scalars(select(LedgerEntry)).first()
    entry.delta_atoms = 0
    with pytest.raises(AppendOnlyViolation):
        db.commit()
    db.rollback()
    db.delete(db.scalars(select(AssetFill)).one())
    with pytest.raises(AppendOnlyViolation):
        db.commit()
    db.rollback()


# ---- events ----------------------------------------------------------------------


def test_events_are_idempotent_and_mismatches_quarantine(db, account):
    first = ingest.ingest_events(db, account, [deposit("d1", "1000")])
    assert first.new == 1
    assert legs_of(db, "event:deposit:d1") == [("deposit", "USD", 10_000_000, 4)]
    assert ingest.ingest_events(db, account, [deposit("d1", "1000")]).duplicates == 1
    changed = ingest.ingest_events(db, account, [deposit("d1", "999")])
    assert changed.quarantined and strategy_status(db) == "quarantined"


def test_venue_ref_reused_across_event_kinds_quarantines(db, account):
    """(account, venue_ref, kind, asset) is the table identity: an ADJUSTMENT
    event reusing deposit d1's ref with a DEPOSIT USD leg would collide."""
    ingest.ingest_events(db, account, [deposit("d1", "1000")])
    clash = NormalizedEvent(
        venue_ref="d1",
        kind=LegKind.ADJUSTMENT,
        legs=[NormalizedLeg(asset="USD", delta_atoms=5, decimals=4, kind=LegKind.DEPOSIT)],
        occurred_at=T0,
    )
    result = ingest.ingest_events(db, account, [clash])
    assert result.new == 0 and "already used" in result.quarantined[0]


def test_quarantine_reason_is_recorded_once(db, account):
    ingest.ingest_events(db, account, [deposit("d1", "1000")])
    for _ in range(3):
        ingest.ingest_events(db, account, [deposit("d1", "999")])
    assert len(db.scalars(select(LedgerQuarantine)).all()) == 1


# ---- sync, pages and resume cursors ----------------------------------------------


def test_sync_account_uses_v2_streams_and_resume_cursor(db, account, venue):
    venue.fill_pages = {
        None: Page([buy_btc("t1", "0.5", "60000", "30", 1)], next_cursor="c1"),
        "c1": Page([buy_btc("t2", "0.1", "50000", "5", 2)], resume_cursor="R1"),
        "R1": Page([buy_btc("t3", "0.2", "50000", "10", 3)], resume_cursor="R2"),
    }
    venue.event_pages = {None: Page([deposit("d1", "50000")])}

    first = ingest.sync_account(db, account)
    assert (first.new_fills, first.new_settlements, first.new_events) == (2, 0, 1)
    assert ingest.stored_resume_cursor(db, account, ingest.STREAM_ASSET_FILLS) == "R1"
    assert [c[1] for c in venue.calls if c[0] == "fills"] == [None, "c1"]

    second = ingest.sync_account(db, account)
    assert second.new_fills == 1
    fills_calls = [c for c in venue.calls if c[0] == "fills"]
    # Resumed from the stored cursor, with since = newest fill (t2, +2 min) - OVERLAP.
    assert fills_calls[2][1] == "R1"
    assert fills_calls[2][2] == T0 + timedelta(minutes=2) - ingest.OVERLAP
    assert ingest.stored_resume_cursor(db, account, ingest.STREAM_ASSET_FILLS) == "R2"
    assert len(db.scalars(select(LedgerSyncState)).all()) == 2


def test_page_function_commits_each_page_and_survives_rate_limits(db, account, venue):
    venue.fill_pages = {
        None: Page([buy_btc("t1", "0.5", "60000", "30")], next_cursor="c1"),
        "c1": RateLimited(30, "TradesHistory"),
    }
    cursors = ingest.ingest_asset_fills_page(db, account, venue, None, None)
    assert cursors == ingest.PageCursors("c1", None)
    with pytest.raises(RateLimited):
        ingest.ingest_asset_fills_page(db, account, venue, "c1", None)
    db.rollback()
    # Page one is committed; the job retries from "c1".
    assert len(db.scalars(select(AssetFill)).all()) == 1
    with pytest.raises(RateLimited):
        ingest.sync_account(db, account)
    assert account.status == "error"


def test_repeated_cursor_is_an_error_not_a_loop(db, account, venue):
    venue.fill_pages = {None: Page([], next_cursor="c1"), "c1": Page([], next_cursor="c1")}
    with pytest.raises(RuntimeError, match="repeated cursor"):
        ingest.sync_account(db, account)


def test_interrupted_newest_first_backfill_resumes_and_loses_nothing(db, account, venue):
    """A venue that pages newest-first across pages (Kraken ofs paging) is
    rate-limited after page one. The next sync must continue from the stored
    mid-pass cursor, not restart from since = newest stored fill - OVERLAP,
    which would skip every older page for good."""
    venue.fill_pages = {
        None: Page([buy_btc("t3", "0.1", "50000", "5", 30)], next_cursor="p2"),
        "p2": RateLimited(30, "TradesHistory"),
    }
    with pytest.raises(RateLimited):
        ingest.sync_account(db, account)
    assert ingest.stored_resume_cursor(db, account, ingest.STREAM_ASSET_FILLS) == "p2"

    # The limit lifts: page two holds the OLDER fills (+1 and +2 minutes).
    venue.fill_pages["p2"] = Page(
        [buy_btc("t1", "0.5", "60000", "30", 1), buy_btc("t2", "0.1", "50000", "5", 2)],
        resume_cursor="R1",
    )
    before = len(venue.calls)
    result = ingest.sync_account(db, account)
    assert result.new_fills == 2
    # The second sync's first request continues the pass (a since-based
    # restart, cursor None, is what loses the older pages on a real venue).
    assert [c[1] for c in venue.calls[before:] if c[0] == "fills"] == ["p2"]
    assert sorted(db.scalars(select(AssetFill.venue_fill_id)).all()) == ["t1", "t2", "t3"]
    assert ingest.stored_resume_cursor(db, account, ingest.STREAM_ASSET_FILLS) == "R1"


# ---- reconcile v2 ------------------------------------------------------------------


def test_tolerance_per_asset_hand_computed():
    # USD at 4 dp: 1000.0000 = 10_000_000 atoms; 0.1% = 10_000 ($1); cent = 100.
    assert reconcile.asset_tolerance_atoms("USD", 10_000_000, 4) == 10_000
    # USD 5.0000 = 50_000: 0.1% = 50 atoms < one cent (100 atoms) -> 100.
    assert reconcile.asset_tolerance_atoms("USD", 50_000, 4) == 100
    # At 6 dp the USD rule is exactly M2's tolerance_for.
    assert reconcile.asset_tolerance_atoms("USD", 1_000_000_000, 6) == reconcile.tolerance_for(
        1_000_000_000
    )
    # BTC 0.5 at 8 dp: 50_000_000 x 10 // 10_000 = 50_000 atoms.
    assert reconcile.asset_tolerance_atoms("BTC", 50_000_000, 8) == 50_000
    # BTC 0.000001 = 100 atoms: 0.1% floors to 0 -> one atom.
    assert reconcile.asset_tolerance_atoms("BTC", 100, 8) == 1


def test_compare_vectors_at_the_finer_scale():
    # Ledger BTC 0.3765432109 (10 dp); venue says 0.37654321 (8 dp) = 3_765_432_100
    # at 10 dp: divergence -9 atoms; tolerance 3_765_432_100 x 10 // 10_000 = 3_765_432.
    (check,) = reconcile.compare_vectors(
        {"BTC": AssetAmount(asset="BTC", atoms=3_765_432_109, decimals=10)},
        [AssetAmount(asset="BTC", atoms=37_654_321, decimals=8)],
    )
    assert (check.decimals, check.divergence_atoms, check.tolerance_atoms, check.ok) == (
        10,
        -9,
        3_765_432,
        True,
    )
    # An asset only the venue knows about diverges by its whole amount.
    (eth,) = reconcile.compare_vectors({}, [AssetAmount(asset="ETH", atoms=5, decimals=2)])
    assert (eth.computed_atoms, eth.divergence_atoms, eth.ok) == (0, 5, False)


def _anchor(db, account, **assets):
    for asset, (atoms, decimals) in assets.items():
        db.add(
            AnchorBalance(
                venue_account_id=account.id, asset=asset, atoms=atoms, decimals=decimals, as_of=T0
            )
        )
    db.commit()


def test_reconcile_asset_account_ok(db, account, venue):
    # Anchor USD 50000.0000; buy 0.5 BTC @ 60000 fee 30 ->
    # USD 50000 - 30000 - 30 = 19970.0000; BTC 0.5.
    _anchor(db, account, USD=(usd("50000"), 4))
    venue.fill_pages = {None: Page([buy_btc("t1", "0.5", "60000", "30")])}
    venue.balances = [
        AssetAmount(asset="USD", atoms=usd("19970"), decimals=4),
        AssetAmount(asset="BTC", atoms=btc("0.5"), decimals=8),
    ]
    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is True
    assert snap.venue_balance_micros == 19_970_000_000 == snap.computed_cash_micros
    rows = {r.asset: r for r in db.scalars(select(AssetReconciliation))}
    assert rows["BTC"].computed_atoms == 50_000_000 and rows["BTC"].reconciled
    assert rows["USD"].divergence_atoms == 0
    assert strategy_status(db) == "incubating"


def test_reconcile_asset_divergence_quarantines(db, account, venue):
    # Venue holds 0.49 BTC; ledger says 0.5: divergence -1_000_000 atoms,
    # tolerance 49_000_000 x 10 // 10_000 = 49_000.
    _anchor(db, account, USD=(usd("50000"), 4))
    venue.fill_pages = {None: Page([buy_btc("t1", "0.5", "60000", "30")])}
    venue.balances = [
        AssetAmount(asset="USD", atoms=usd("19970"), decimals=4),
        AssetAmount(asset="BTC", atoms=btc("0.49"), decimals=8),
    ]
    snap = reconcile.reconcile_account(db, account)
    assert snap.reconciled is False
    btc_row = db.scalars(
        select(AssetReconciliation).where(AssetReconciliation.asset == "BTC")
    ).first()
    assert (btc_row.divergence_atoms, btc_row.tolerance_atoms) == (-1_000_000, 49_000)
    assert strategy_status(db) == "quarantined"
    assert db.scalars(select(LedgerQuarantine)).one().ref == "reconcile:BTC"


def test_broken_running_balance_quarantines_even_when_totals_match(db, account, venue):
    # +100.0000 (bal 100.0000) then +(-20.0000) reported bal 70.0000: expected 80.
    # The vector (anchor 0 + 100 - 20 = 80 vs venue 80) matches; the chain does not.
    _anchor(db, account, USD=(0, 4))
    withdrawal = NormalizedEvent(
        venue_ref="w1",
        kind=LegKind.WITHDRAWAL,
        legs=[
            NormalizedLeg(
                asset="USD",
                delta_atoms=-usd("20"),
                decimals=4,
                kind=LegKind.WITHDRAWAL,
                balance_after_atoms=usd("70"),
            )
        ],
        occurred_at=T0 + timedelta(minutes=5),
    )
    venue.event_pages = {None: Page([deposit("d1", "100", 1, balance_after="100"), withdrawal])}
    venue.balances = [AssetAmount(asset="USD", atoms=usd("80"), decimals=4)]
    snap = reconcile.reconcile_account(db, account)
    assert snap.divergence_micros == 0
    assert snap.reconciled is False
    q = db.scalars(select(LedgerQuarantine)).one()
    assert "running balance 700000" in q.reason and "(800000)" in q.reason


def test_reconcile_asset_requires_anchor(db, account, venue):
    with pytest.raises(ValueError, match="anchor"):
        reconcile.reconcile_account(db, account)


def test_asset_anchor_uses_balances_and_asset_fill_pages(db, account, venue, monkeypatch):
    now = datetime.now(UTC)
    venue.balances = [
        AssetAmount(asset="USD", atoms=usd("12.3456"), decimals=4),
        AssetAmount(asset="BTC", atoms=btc("0.25"), decimals=8, entry_price="58000.1"),
    ]
    venue.fill_pages = {
        None: Page([buy_btc("t1", "0.1", "1", "0").model_copy(update={"filled_at": now})])
    }
    sleeps: list[float] = []
    monkeypatch.setattr(reconcile.time, "sleep", sleeps.append)

    anchor = reconcile.take_anchor(venue, settle_seconds=5.0, attempts=2)
    assert sleeps == [5.0]  # activity near the anchor: waited once, then gave up
    assert all(c[0] == "fills" and c[1] is None for c in venue.calls)
    # USD 12.3456 at 4 dp = 123_456 atoms -> 12_345_600 micros.
    assert anchor.balance_micros == 12_345_600 and anchor.positions == []

    reconcile.store_anchor_balances(db, account, anchor)
    db.commit()
    rows = {r.asset: r for r in reconcile.latest_anchor_balances(db, account)}
    assert rows["BTC"].atoms == 25_000_000 and rows["BTC"].entry_price == "58000.1"
    assert rows["USD"].decimals == 4
