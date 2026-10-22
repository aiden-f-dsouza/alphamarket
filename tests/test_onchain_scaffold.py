"""On-chain perps shared scaffold (docs/onchain_perps_plan.md section 2): the
contract additions, asymmetric funding, non-copyable instruments, POSITIONS
reconciliation, the venue privacy hooks, preflight's terms markers, the venue
anchors, and a Solana address round trip through discover, watch and
opt-out with its case intact. Every venue here is a stub registered for the
test; no network (invariant 6). Keys are generated in the test."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException
from sqlalchemy import event, select

from alphamarket import discover, jobs, paper, prices, reconcile, watch_pages, watchlist
from alphamarket.config import get_settings
from alphamarket.connectors import registry
from alphamarket.connectors.addresses import AddressKind, b58encode, verify_ed25519
from alphamarket.connectors.base import (
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    CredentialKind,
    FundingRate,
    InstrumentKind,
    InstrumentType,
    LeaderboardEntry,
    NormalizedBalances,
    NormalizedInstrument,
    OwnershipProof,
    Page,
    PaperFeeModel,
    RateBudget,
    RateScope,
    ReadOnlyProof,
    ReconcileMode,
    VenueProfile,
)
from alphamarket.ingest import upsert_instrument
from alphamarket.models import (
    Allocation,
    AllocationStatus,
    AnchorBalance,
    AssetReconciliation,
    AuditLog,
    EquitySnapshot,
    FundingRateRow,
    Instrument,
    LeaderboardSnapshot,
    LedgerQuarantine,
    Strategy,
    StrategyKind,
    StrategyStatus,
    User,
    VenueAccount,
    Watch,
    WatchOptout,
)
from alphamarket.paper_lifecycle import funding_payment_micros
from alphamarket.venue_limits import MemoryBuckets, VenueLimiter
from alphamarket.venues import verification_tier
from test_watchlist import login, signup

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
M = 1_000_000


def profile(venue: str, **kw) -> VenueProfile:
    """A watch-only ADDRESS venue in the shape every on-chain profile has."""
    base = dict(
        venue=venue,
        display_name=venue.capitalize(),
        envs=("prod",),
        credential=CredentialKind.ADDRESS,
        key_label="Wallet address",
        secret_label=None,
        key_help="",
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        tick_micros=1,
        contract_step_micros=1,
        fee_rate_bps=4,
        fee_rounding_micros=1,
        ownership=OwnershipProof.SIGNED_MESSAGE,
        readonly_proof=ReadOnlyProof.NONE,
        quote_asset="USDC",
        requires_partner_approval=True,  # watch only: the connect form stays closed
        paper_fee_model=PaperFeeModel.BPS_OF_NOTIONAL,
        asset_classes=("crypto",),
        leaderboard_periods=("week",),
    )
    base.update(kw)
    return VenueProfile(**base)


class StubConnector(AssetVenueConnector):
    """Public-address connector: an empty account that exists, and an ed25519
    claim (the way a Solana venue proves ownership)."""

    def __init__(self, venue: str, address: str) -> None:
        self.venue = venue
        self.address = address
        self.balances: list[AssetAmount] = [AssetAmount(asset="USDC", atoms=0, decimals=6)]

    def check_write_scope(self):
        return None

    def fetch_balances(self):
        return NormalizedBalances(as_of=datetime.now(UTC), assets=self.balances)

    def fetch_asset_fills_page(self, cursor=None, since=None):
        return Page()

    def claim_text(self, code: str) -> str:
        return f"Koine ownership claim\nVenue: Stub\nAddress: {self.address}\nCode: {code}\n"

    def claim_payload(self, code: str, signature: str) -> str:
        return json.dumps({"code": code, "signature": signature})

    def verify_claim(self, code: str) -> bool | None:
        data = json.loads(code)
        return verify_ed25519(self.address, self.claim_text(data["code"]), data["signature"])


class StubMarketData(AssetMarketData):
    def __init__(self, venue: str, board=None, private=None, owner=None) -> None:
        self.venue = venue
        self.board = board or []
        self.private = private
        self.owner = owner or {}
        self.owner_calls: list[str] = []

    def fetch_leaderboards(self, periods):
        return {p: list(self.board) for p in periods}

    def private_addresses(self):
        return self.private

    def owner_private(self, address):
        self.owner_calls.append(address)
        return self.owner.get(address)


@pytest.fixture()
def stub_venue(monkeypatch):
    """Register a stub venue (profile, connector, market data) and open it for
    watching and discover. Returns a function: register(venue, **profile_kw)."""
    s = get_settings()
    opened: list[str] = []
    sources: dict[str, StubMarketData] = {}

    def register(venue: str, **kw) -> StubMarketData:
        monkeypatch.setitem(registry.PROFILES, venue, profile(venue, **kw))
        monkeypatch.setitem(
            registry._BUILDERS, venue, lambda env, key_id, secret: StubConnector(venue, key_id)
        )
        sources[venue] = StubMarketData(venue)
        monkeypatch.setitem(registry._MARKET_DATA_BUILDERS, venue, lambda env: sources[venue])
        opened.append(venue)
        venues = ("hyperliquid", "polymarket", *opened)
        monkeypatch.setattr(watchlist, "WATCHABLE_VENUES", venues)
        monkeypatch.setattr(watch_pages, "WATCHABLE_VENUES", venues)
        monkeypatch.setattr(s, "watch_venues", ",".join(opened))
        return sources[venue]

    monkeypatch.setattr(s, "watch_discover_enabled", True)
    return register


def solana_wallet():
    key = Ed25519PrivateKey.generate()
    address = b58encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    return key, address


def _user(db, email: str, **kw) -> User:
    u = User(email=email, password_hash="!", display_name=email.split("@")[0], **kw)
    db.add(u)
    db.flush()
    return u


# ---- S7: asymmetric funding (hand-computed) ---------------------------------------------


def test_funding_payment_long_short_and_asymmetric():
    q, mark, rate = Fraction(2), Fraction(2500), Fraction("0.0001")
    short_rate = Fraction("0.00003")
    # long pays: -2 x 2500 x 0.0001 = -0.5 USD = -500,000 micros (short_rate unused)
    assert funding_payment_micros(q, mark, rate) == -500_000
    assert funding_payment_micros(q, mark, rate, short_rate) == -500_000
    # symmetric short receives: -(-2) x 2500 x 0.0001 = +0.5 = 500,000
    assert funding_payment_micros(-q, mark, rate) == 500_000
    # asymmetric short pays its own rate: -2 x 2500 x 0.00003 = -0.15 = -150,000
    assert funding_payment_micros(-q, mark, rate, short_rate) == -150_000
    # a negative short_rate is received: -2 x 2500 x -0.00002 = +0.1 = 100,000
    assert funding_payment_micros(-q, mark, rate, Fraction("-0.00002")) == 100_000
    # flooring against the follower: -1 x 1 x 0.0000015 = -1.5 micros -> -2;
    # with the sign flipped the credit +1.5 micros -> +1
    assert funding_payment_micros(Fraction(-1), Fraction(1), rate, Fraction("0.0000015")) == -2
    assert funding_payment_micros(Fraction(-1), Fraction(1), rate, Fraction("-0.0000015")) == 1


def test_refresh_funding_stores_short_rate(session_factory):
    class Source:
        def fetch_funding_history(self, symbol, start, end):
            return [
                FundingRate(symbol=symbol, at=NOW - timedelta(hours=2), rate="0.0001"),
                FundingRate(
                    symbol=symbol,
                    at=NOW - timedelta(hours=1),
                    rate="0.0001",
                    short_rate="0.00003",
                    mark_price="2500",
                ),
            ]

    with session_factory() as db:
        inst = Instrument(
            venue="stubx",
            symbol="ETH",
            type=InstrumentType.PERP,
            base_asset="ETH-PERP",
            quote_asset="USDC",
            qty_decimals=4,
        )
        db.add(inst)
        db.flush()
        prices._refresh_funding(db, Source(), inst, NOW)
        db.flush()
        rows = db.scalars(select(FundingRateRow).order_by(FundingRateRow.at)).all()
        assert [(r.rate, r.short_rate) for r in rows] == [("0.0001", None), ("0.0001", "0.00003")]


# ---- S2/S7: copyable instruments ---------------------------------------------------------


def _norm(copyable: bool) -> NormalizedInstrument:
    return NormalizedInstrument(
        symbol="AAPL",
        type=InstrumentType.PERP,
        base_asset="AAPL-PERP",
        quote_asset="USDC",
        qty_decimals=3,
        copyable=copyable,
    )


def test_upsert_writes_and_follows_copyable(session_factory):
    assert NormalizedInstrument.model_fields["copyable"].default is True
    with session_factory() as db:
        row = upsert_instrument(db, "stubx", _norm(False), NOW)
        db.flush()
        assert row.copyable is False
        row = upsert_instrument(db, "stubx", _norm(True), NOW)  # the venue's latest word
        assert row.copyable is True
        other = upsert_instrument(
            db,
            "stubx",
            NormalizedInstrument(
                symbol="BTC",
                type=InstrumentType.PERP,
                base_asset="BTC-PERP",
                quote_asset="USDC",
                qty_decimals=5,
            ),
            NOW,
        )
        db.flush()
        assert other.copyable is True  # the default


def test_not_copyable_is_skipped_before_sizing():
    hl = registry.get_profile("hyperliquid")
    signal = SimpleNamespace(observed_at=NOW, filled_at=NOW - timedelta(seconds=5))
    inst = Instrument(
        venue="hyperliquid",
        symbol="AAPL",
        type="perp",
        base_asset="AAPL-PERP",
        quote_asset="USDC",
        qty_decimals=3,
        copyable=False,
    )
    assert paper._precheck(signal, inst, hl) == paper.NOT_COPYABLE == "not_copyable"
    inst.copyable = True
    assert paper._precheck(signal, inst, hl) is None
    # A stale signal is still reported as stale first (the cheaper fact).
    stale = SimpleNamespace(observed_at=NOW, filled_at=NOW - timedelta(days=2))
    inst.copyable = False
    assert paper._precheck(stale, inst, hl) == paper.STALE_SIGNAL


# ---- S2: a second host's endpoints cost 0 --------------------------------------------------


def test_cost_zero_neither_blocks_nor_consumes_tokens():
    api = RateBudget(
        scope=RateScope.IP, capacity=2, refill_per_minute=1, costs=(("explorer_logs", 0),)
    )
    explorer = RateBudget(
        scope=RateScope.IP,
        capacity=4,
        refill_per_minute=1,
        default_cost=0,
        costs=(("explorer_logs", 2),),
    )
    clock = SimpleNamespace(ms=1_800_000_000_000)
    limiter = VenueLimiter(
        MemoryBuckets(),
        "203.0.113.9",
        budgets_for=lambda v: (api, explorer),
        clock_ms=lambda: clock.ms,
    )

    def levels():
        out = []
        for budget, spec in limiter.buckets("two_hosts", None):
            state = limiter.backend.state(spec, clock.ms)  # None: never charged, full
            out.append(state.level_milli if state is not None else budget.capacity * 1_000)
        return out

    start = levels()
    limiter.acquire("two_hosts", None, "explorer_logs")  # explorer: 2, api: 0
    limiter.acquire("two_hosts", None, "explorer_logs")
    after_explorer = levels()
    assert after_explorer[0] == start[0]  # the API bucket was not touched
    assert after_explorer[1] == start[1] - 4_000  # 2 + 2 tokens, in milli-tokens
    # The explorer bucket is empty now, yet API calls (cost 0 there) still pass,
    # and the API bucket alone pays for them.
    limiter.acquire("two_hosts", None, "trades")
    limiter.acquire("two_hosts", None, "trades")
    final = levels()
    assert final[0] == start[0] - 2_000 and final[1] == after_explorer[1]


# ---- S2: POSITIONS reconciliation and its tier -------------------------------------------


@pytest.fixture()
def positions_venue(monkeypatch):
    monkeypatch.setitem(
        registry.PROFILES,
        "posx",
        profile(
            "posx",
            balance_observable=False,
            reconcile_mode=ReconcileMode.POSITIONS,
            position_tolerance_atoms=2,
        ),
    )
    fake = StubConnector("posx", "0x" + "ab" * 20)
    monkeypatch.setattr(reconcile, "connector_for", lambda account: fake)
    monkeypatch.setattr(reconcile, "sync_account", lambda db, account: None)
    return fake


def _positions_account(db, eth_atoms: int) -> VenueAccount:
    user = _user(db, "p@example.com")
    acct = VenueAccount(
        user_id=user.id, venue="posx", venue_env="prod", key_id="0x" + "ab" * 20, claimed_at=NOW
    )
    db.add(acct)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=acct.id, name="P", slug="p"))
    for asset, atoms, decimals in (("ETH-L-PERP", eth_atoms, 12), ("USDC", 5_000 * M, 6)):
        db.add(
            AnchorBalance(
                venue_account_id=acct.id,
                asset=asset,
                atoms=atoms,
                decimals=decimals,
                as_of=NOW - timedelta(days=1),
            )
        )
    db.commit()
    return acct


def test_positions_mode_reconciles_positions_and_never_reads_cash(session_factory, positions_venue):
    with session_factory() as db:
        acct = _positions_account(db, 1_500_000_000_000)  # 1.5 ETH at 12 dp
        assert reconcile.reconcilable(acct)  # no balance, still reconciled nightly
        assert verification_tier(acct) == "positions_verified"
        # The venue's position is 1 atom off (inside the 2-atom tolerance), and
        # its USDC is nothing like the ledger's: cash is never compared.
        positions_venue.balances = [
            AssetAmount(asset="ETH-L-PERP", atoms=1_500_000_000_001, decimals=12),
            AssetAmount(asset="USDC", atoms=1, decimals=6),
        ]
        snap = reconcile.reconcile_account(db, acct)
        assert snap.reconciled is True
        assert (snap.venue_balance_micros, snap.computed_cash_micros) == (0, 0)
        rows = db.scalars(select(AssetReconciliation)).all()
        assert [(r.asset, r.divergence_atoms, r.tolerance_atoms) for r in rows] == [
            ("ETH-L-PERP", 1, 2)
        ]
        strategy = db.scalar(select(Strategy))
        assert strategy.status != StrategyStatus.QUARANTINED
        assert db.scalars(select(LedgerQuarantine)).all() == []


def test_positions_mode_quarantines_a_mismatched_position(session_factory, positions_venue):
    with session_factory() as db:
        acct = _positions_account(db, 1_500_000_000_000)
        positions_venue.balances = [
            AssetAmount(asset="ETH-L-PERP", atoms=1_500_000_000_003, decimals=12),  # 3 > 2
            AssetAmount(asset="BTC-S-PERP", atoms=-2_000, decimals=5),  # unknown to the ledger
        ]
        snap = reconcile.reconcile_account(db, acct)
        assert snap.reconciled is False
        assert db.scalar(select(Strategy)).status == StrategyStatus.QUARANTINED
        reasons = sorted(q.ref for q in db.scalars(select(LedgerQuarantine)))
        assert reasons == ["reconcile:BTC-S-PERP", "reconcile:ETH-L-PERP"]


def test_dashboard_says_cash_is_not_compared_in_positions_mode(
    client, session_factory, positions_venue
):
    signup(client, "p@example.com")
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == "p@example.com"))
        acct = VenueAccount(
            user_id=user.id, venue="posx", venue_env="prod", key_id="0x" + "ab" * 20
        )
        db.add(acct)
        db.flush()
        strategy = Strategy(user_id=user.id, venue_account_id=acct.id, name="P", slug="p")
        db.add(strategy)
        db.flush()
        # What reconcile_positions stores: the cash columns are zeros, unchecked.
        db.add(
            EquitySnapshot(
                strategy_id=strategy.id,
                as_of=NOW,
                venue_balance_micros=0,
                computed_cash_micros=0,
                divergence_micros=0,
                tolerance_micros=0,
                reconciled=True,
            )
        )
        db.commit()
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "cash not compared · positions only" in page.text
    assert "$0.0000" not in page.text


def test_balances_mode_keeps_its_tiers(monkeypatch, session_factory):
    monkeypatch.setitem(registry.PROFILES, "fillsx", profile("fillsx", balance_observable=False))
    with session_factory() as db:
        user = _user(db, "f@example.com")
        acct = VenueAccount(user_id=user.id, venue="fillsx", venue_env="prod", key_id="k")
        assert not reconcile.reconcilable(acct)
        acct.claimed_at = NOW
        assert verification_tier(acct) == "fills_only"
    assert registry.get_profile("hyperliquid").reconcile_mode is ReconcileMode.BALANCES
    # Only the venues that predate the scaffold: venue branches add profiles
    # with their own kinds and tolerances without touching this test.
    for venue in ("hyperliquid", "polymarket"):
        assert registry.get_profile(venue).position_tolerance_atoms == 0
        assert registry.get_profile(venue).address_kind is AddressKind.EVM
    # A plain StrEnum: members compare and hash apart (a dataclass decorator
    # once made every member equal).
    assert ReconcileMode.BALANCES != ReconcileMode.POSITIONS
    assert len({ReconcileMode.BALANCES: 1, ReconcileMode.POSITIONS: 2}) == 2


# ---- S3/S5: the venue's own privacy flag ------------------------------------------------------


def test_watch_time_privacy_flag_refuses_and_stores_nothing(session_factory, stub_venue):
    source = stub_venue("privx")
    private, public = "0x" + "11" * 20, "0x" + "22" * 20
    source.owner = {private: True, public: False}
    with session_factory() as db:
        user = _user(db, "u@example.com", email_verified_at=NOW)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            watchlist.add_watch(db, user, watchlist.WatchIn(venue="privx", address=private))
        assert exc.value.status_code == 422
        assert exc.value.detail == "The owner of this address has turned on privacy on Privx."
        assert db.scalars(select(VenueAccount)).all() == []
        assert db.scalars(select(WatchOptout)).all() == []
        watched = watchlist.add_watch(db, user, watchlist.WatchIn(venue="privx", address=public))
        assert watched.account.key_id == public
    assert source.owner_calls == [private, public]  # once per new watch, never per sync


def _watched_account(db, system: User, user: User, venue: str, address: str) -> VenueAccount:
    acct = VenueAccount(
        user_id=system.id, venue=venue, venue_env="prod", key_id=address, is_watch=True
    )
    db.add(acct)
    db.flush()
    strategy = Strategy(
        user_id=system.id,
        venue_account_id=acct.id,
        name="w",
        slug=f"watch-{acct.id}",
        kind=StrategyKind.WATCH,
        public=False,
    )
    db.add(strategy)
    db.flush()
    db.add(Watch(user_id=user.id, strategy_id=strategy.id, label=""))
    return acct


def test_sweep_removes_flagged_watches_without_an_optout_row(session_factory, stub_venue):
    stub_venue("privx")
    flagged, kept = "0x" + "33" * 20, "0x" + "44" * 20
    with session_factory() as db:
        system = _user(db, "system@koine.invalid", is_system=True)
        user = _user(db, "u@example.com")
        a = _watched_account(db, system, user, "privx", flagged)
        b = _watched_account(db, system, user, "privx", kept)
        tail = Allocation(
            user_id=user.id,
            strategy_id=db.scalar(select(Strategy.id).where(Strategy.venue_account_id == a.id)),
            allocation_micros=1_000 * M,
            drawdown_limit_bps=2_000,
            high_water_micros=1_000 * M,
        )
        db.add(tail)
        db.commit()

        source = StubMarketData("privx", private=frozenset({flagged, "0x" + "55" * 20}))
        assert discover.sweep_private(db, "privx", source) == 1

        left = (
            db.execute(
                select(VenueAccount.key_id)
                .join(Strategy)
                .join(Watch, Watch.strategy_id == Strategy.id)
            )
            .scalars()
            .all()
        )
        assert left == [kept]
        db.refresh(tail)
        assert (tail.status, tail.stop_reason) == (AllocationStatus.STOPPED, "venue_private")
        assert db.scalars(select(WatchOptout)).all() == []  # the flag can be turned off again
        [row] = db.scalars(select(AuditLog).where(AuditLog.action == "watch.venue_private")).all()
        assert row.target_id in (str(a.id), a.id)
        assert row.details == {"venue": "privx", "watches_removed": 1, "tails_stopped": 1}
        assert flagged not in json.dumps(row.details)  # no address in the audit details
        assert db.get(VenueAccount, b.id) is not None


def test_sweep_with_no_privacy_signal_makes_no_query(session_factory, stub_venue):
    stub_venue("privx")
    with session_factory() as db:
        statements: list[str] = []
        bind = db.get_bind()
        listener = lambda *args: statements.append(args[2])  # noqa: E731
        event.listen(bind, "before_cursor_execute", listener)
        try:
            assert discover.sweep_private(db, "privx", StubMarketData("privx")) == 0
            assert (
                discover.sweep_private(db, "privx", StubMarketData("privx", private=frozenset()))
                == 0
            )
        finally:
            event.remove(bind, "before_cursor_execute", listener)
        assert statements == []


def test_refresh_drops_invalid_addresses_and_sweeps(session_factory, stub_venue, monkeypatch):
    source = stub_venue("privx")
    monkeypatch.setattr(get_settings(), "watch_discover_min_account_value", 0)
    flagged = "0x" + "66" * 20
    source.board = [
        LeaderboardEntry(address="0x" + "AB" * 20, rank=1, pnl="10"),
        LeaderboardEntry(address="not-an-address", rank=2, pnl="9"),
        LeaderboardEntry(address="0x" + "cd" * 20, rank=3, pnl="8"),
    ]
    source.private = frozenset({flagged})
    with session_factory() as db:
        system = _user(db, "system@koine.invalid", is_system=True)
        user = _user(db, "u@example.com")
        _watched_account(db, system, user, "privx", flagged)
        db.commit()
        ctx = SimpleNamespace(db=db, limiter=None, clock=lambda: NOW, settings=get_settings())
        counts = discover.refresh(ctx)
        assert counts == {
            ("privx", "week"): 2,
            ("privx", "invalid_addresses"): 1,
            ("privx", "private_removed"): 1,
        }
        snap = db.scalar(select(LeaderboardSnapshot))
        assert [r["address"] for r in snap.rows] == ["0x" + "ab" * 20, "0x" + "cd" * 20]
        assert db.scalars(select(Watch)).all() == []


# ---- the Solana round trip (scaffold exit) ------------------------------------------------------


def test_solana_address_keeps_its_case_through_discover_watch_and_optout(
    client, session_factory, stub_venue
):
    stub_venue("solx", address_kind=AddressKind.SOLANA)
    key, address = solana_wallet()
    assert address != address.lower() and address != address.upper()  # mixed case

    row = discover.snapshot_row("solx", LeaderboardEntry(address=f" {address} ", rank=1, pnl="5"))
    assert row["address"] == address
    with session_factory() as db:
        discover.store_snapshot(db, "solx", "week", [row], NOW)
        db.commit()
        assert db.scalar(select(LeaderboardSnapshot)).rows[0]["address"] == address

    signup(client, "w@example.com")
    page = client.get("/watch/discover?venue=solx&period=week")
    assert page.status_code == 200
    assert f'name="address" value="{address}"' in page.text
    assert f"{address[:4]}…{address[-4:]}" in page.text  # addresses.short

    watch_form = client.get("/watch")
    assert "Enter a Solana address (32-44 characters, case-sensitive)" in watch_form.text
    assert 'maxlength="66"' in watch_form.text and "pattern=" not in watch_form.text

    resp = client.post(
        "/watch",
        data={
            "venue": "solx",
            "address": address,
            "label": "",
            "return_to": "discover",
            "d_venue": "solx",
            "d_period": "week",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    with session_factory() as db:
        account = watchlist.watch_account(db, "solx", address)
        assert account is not None and account.key_id == address
        assert watchlist.watch_account(db, "solx", address.lower()) is None
        assert (
            discover.discover_view(
                db, db.scalar(select(User).where(User.email == "w@example.com")), "solx", "week"
            )
            .rows[0]
            .state
            == discover.RowState.WATCHING
        )
        account_id = account.id

    # The owner opts it out with an ed25519 signature (pasted: no wallet JS).
    signup(client, "owner@example.com")
    proof = client.post("/watch/optout", data={"venue": "solx", "address": address})
    assert proof.status_code == 200
    assert 'data-claim-kind="solana"' in proof.text and "Phantom" in proof.text
    assert "Sign with my wallet" not in proof.text  # the EVM wallet button only
    message = proof.text.split('data-claim-message="', 1)[1].split('"', 1)[0]
    message = message.replace("&#10;", "\n").replace("&#34;", '"')
    signature = b58encode(key.sign(message.encode("utf-8")))
    done = client.post(
        "/watch/optout/verify", data={"venue": "solx", "address": address, "signature": signature}
    )
    assert done.status_code == 200, done.text
    assert "Opted out." in done.text
    with session_factory() as db:
        [optout] = db.scalars(select(WatchOptout)).all()
        assert (optout.venue, optout.address) == ("solx", address)
        assert db.scalars(select(Watch)).all() == []
        assert jobs.watch_cadences(db)[account_id] == jobs.WatchCadence.OPTED_OUT
    login(client, "w@example.com")
    refused = client.post("/watch", data={"venue": "solx", "address": address})
    assert refused.status_code == 422
    assert "asked not to be tracked" in refused.text


def test_solana_address_validation_errors_are_the_kind_hint(session_factory, stub_venue):
    stub_venue("solx", address_kind=AddressKind.SOLANA)
    for bad in ("0x" + "ab" * 20, "short", "O" * 44):
        with pytest.raises(HTTPException) as exc:
            watchlist.normalize_address("solx", bad)
        assert exc.value.status_code == 422
        assert exc.value.detail == "Enter a Solana address (32-44 characters, case-sensitive)"
    with pytest.raises(HTTPException) as exc:
        watchlist.normalize_optout_address("hyperliquid", "9GN1notevm")
    assert exc.value.detail == "Enter a 0x address (42 characters)"


# ---- S9: preflight refuses uncleared terms ------------------------------------------------------


def _preflight():
    spec = importlib.util.spec_from_file_location("preflight", ROOT / "scripts" / "preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DOC = """# Venue

## Terms-of-service position

{position}

## Consent text (connect screen)

Position: PENDING COUNSEL is only a problem inside the terms section.
"""


def test_preflight_refuses_unread_or_pending_counsel_terms(tmp_path, monkeypatch):
    preflight = _preflight()
    docs = tmp_path / "docs" / "venues"
    docs.mkdir(parents=True)
    for venue, position in (
        ("okv", "Conclusion: ok."),
        ("unreadv", "Position: UNREAD (terms not found 2026-10-22)."),
        ("counselv", "Position: PENDING COUNSEL (Q1)."),
        ("nosection", None),
    ):
        text = DOC.format(position=position) if position else "# Venue\n\nNothing.\n"
        (docs / f"{venue}.md").write_text(text)
        monkeypatch.setitem(
            registry.PROFILES, venue, profile(venue, tos_doc=f"docs/venues/{venue}.md")
        )
    monkeypatch.setitem(registry.PROFILES, "nodoc", profile("nodoc"))

    def problems(venues: str) -> list[str]:
        return preflight.watch_venue_problems(SimpleNamespace(watch_venues=venues), root=tmp_path)

    assert problems("okv") == []
    [unread] = problems("okv,unreadv")
    assert "unreadv" in unread and "Position: UNREAD" in unread
    [counsel] = problems("counselv")
    assert "Position: PENDING COUNSEL" in counsel
    assert "no 'Terms-of-service position' section" in problems("nosection")[0]
    assert "no terms doc" in problems("nodoc")[0]
    assert "not a known venue" in problems("dydx")[0]


def test_preflight_passes_the_venues_open_today():
    preflight = _preflight()
    assert (
        preflight.watch_venue_problems(SimpleNamespace(watch_venues="hyperliquid,polymarket")) == []
    )


# ---- S8: venue anchors --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,count",
    [
        (
            "src/alphamarket/connectors/registry.py",
            4,
        ),  # PROFILES, builders, market data, CASH_AT_PAR
        ("src/alphamarket/demo.py", 1),
        ("scripts/seed_demo.py", 3),  # CREATORS, WATCHES, LEADERBOARD_SEEDS
    ],
)
def test_each_venue_has_its_marked_blocks(path, count):
    text = (ROOT / path).read_text()
    for venue in ("lighter", "pacifica", "gmx_arbitrum", "nado"):
        assert text.count(f"# ---- venue-{venue} ----") == count, (path, venue)
        assert text.count(f"# ---- end venue-{venue} ----") == count, (path, venue)


def test_demo_discover_addresses_follow_the_kind(monkeypatch):
    from alphamarket import demo
    from alphamarket.connectors.addresses import is_valid

    monkeypatch.setitem(registry.PROFILES, "solx", profile("solx", address_kind=AddressKind.SOLANA))
    sol = demo._discover_address("solx", 3)
    assert sol.startswith("De3o") and is_valid(AddressKind.SOLANA, sol)
    assert sol != demo._discover_address("solx", 4)
    evm = demo._discover_address("hyperliquid", 3)
    assert is_valid(AddressKind.EVM, evm)
    assert demo._discover_address("polymarket", 0) == "0x" + "2007" + "0" * 30 + "c0ffee"
    view = demo.discover_sample("polymarket", "week", "pnl")
    assert view.venues[:2] == ("hyperliquid", "polymarket")
    assert all(is_valid(AddressKind.EVM, r.address) for r in view.rows)
