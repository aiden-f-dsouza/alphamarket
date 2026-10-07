"""Wave 2 end to end on real Postgres, through the real JobRunner and handlers:

A. Kraken spot: backfill -> signals -> prices_daily -> a follower allocation
   -> incremental sync -> signals v2 (quote captured) -> mirror (paper book)
   -> asset stats v2.
B. Hyperliquid perp: a follower copies a 1 BTC long, then the PRICES tick
   writes a funding rate and a crashed mark, and the next MIRROR pass books the
   funding and liquidates the allocation.

Venues and market data are scripted in-process (invariant 6). Every expected
number is worked out beside it (USD micros; 1 USD = M)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select

from alphamarket import jobs, prices, worker  # noqa: F401  (worker registers the handlers)
from alphamarket.analytics import compute_strategy_stats
from alphamarket.connectors.base import (
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    DailyClose,
    FundingRate,
    MarginTier,
    NormalizedAssetFill,
    NormalizedBalances,
    NormalizedInstrument,
    NormalizedLeg,
    Page,
    Quote,
)
from alphamarket.jobs import JobKind, JobRunner, enqueue
from alphamarket.models import (
    Allocation,
    AnchorBalance,
    Instrument,
    Job,
    MirrorAssetFill,
    MirrorSkip,
    Signal,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.paper_book import load_book

pytestmark = pytest.mark.pg

M = 1_000_000
NOW = datetime.now(UTC)


class ScriptedVenue(AssetVenueConnector):
    """Serves `fills` once each (the ledger dedupes by venue_fill_id anyway)."""

    def __init__(self, venue: str, quote_asset: str) -> None:
        self.venue = venue
        self.quote_asset = quote_asset
        self.fills: list[NormalizedAssetFill] = []

    def fetch_asset_fills_page(self, cursor=None, since=None):
        return Page(list(self.fills), next_cursor=None, resume_cursor="done")

    def fetch_events_page(self, cursor=None, since=None):
        return Page([], next_cursor=None, resume_cursor="done")

    def fetch_balances(self):
        return NormalizedBalances(
            as_of=NOW, assets=[AssetAmount(asset=self.quote_asset, atoms=0, decimals=6)]
        )

    def check_write_scope(self):
        return False


class ScriptedMarket(AssetMarketData):
    """Public data for one venue; tests move `quote` and `funding` over time."""

    def __init__(self, venue: str, instruments, closes: dict[str, str]) -> None:
        self.venue = venue
        self.instruments = instruments
        self.closes = closes  # symbol -> close every day
        self.quote: tuple[str, str] | None = None  # (bid, ask)
        self.funding: list[FundingRate] = []
        self.tiers: list[MarginTier] = []

    def fetch_instruments(self):
        return list(self.instruments)

    def fetch_daily_closes(self, symbol, start, end):
        if symbol not in self.closes:
            return []
        days = (end - start).days + 1
        return [
            DailyClose(symbol=symbol, day=start + timedelta(days=n), close=self.closes[symbol])
            for n in range(days)
        ]

    def fetch_quote(self, symbol):
        if self.quote is None:
            return None
        bid, ask = self.quote
        return Quote(symbol=symbol, bid=bid, ask=ask, as_of=datetime.now(UTC))

    def fetch_funding_history(self, symbol, start, end):
        return [r for r in self.funding if start <= r.at <= end]

    def fetch_margin_tiers(self, symbol):
        return list(self.tiers)


@pytest.fixture()
def pg(pg_session_factory):
    with pg_session_factory() as s:
        s.execute(delete(Job))
        s.commit()
    yield pg_session_factory
    with pg_session_factory() as s:
        s.execute(delete(Job))
        s.commit()


def _drain(runner: JobRunner) -> None:
    for _ in range(50):
        if not runner.run_once():
            return
    raise AssertionError("job queue did not drain")


def _creator(db, venue: str, quote_asset: str, atoms: int, anchor_at: datetime) -> VenueAccount:
    tag = uuid.uuid4().hex[:8]
    user = User(email=f"c-{tag}@example.com", password_hash="x", display_name="C")
    db.add(user)
    db.flush()
    account = VenueAccount(
        user_id=user.id,
        venue=venue,
        venue_env="prod",
        key_id=f"k-{tag}",
        anchor_balance_micros=atoms,
        anchor_at=anchor_at,
        claimed_at=anchor_at,
    )
    db.add(account)
    db.flush()
    db.add(
        AnchorBalance(
            venue_account_id=account.id, asset=quote_asset, atoms=atoms, decimals=6, as_of=anchor_at
        )
    )
    db.add(Strategy(user_id=user.id, venue_account_id=account.id, name=tag, slug=f"s-{tag}"))
    db.commit()
    return account


def _follow(db, strategy: Strategy, started_at: datetime) -> Allocation:
    tag = uuid.uuid4().hex[:8]
    user = User(email=f"f-{tag}@example.com", password_hash="x", display_name="F")
    db.add(user)
    db.flush()
    last = db.scalar(select(Signal.id).order_by(Signal.id.desc()).limit(1)) or 0
    allocation = Allocation(
        user_id=user.id,
        strategy_id=strategy.id,
        allocation_micros=1_000 * M,
        drawdown_limit_bps=9_000,
        high_water_micros=1_000 * M,
        last_signal_id=last,  # as tails.create_allocation: only signals from now on
        started_at=started_at,
    )
    db.add(allocation)
    db.commit()
    return allocation


KRAKEN_BTC = NormalizedInstrument(
    symbol="XXBTZUSD", type="spot", base_asset="BTC", quote_asset="USD", qty_decimals=8
)


def _kraken_buy(n: int, at: datetime) -> NormalizedAssetFill:
    # 0.05 BTC at 60,000: +5,000,000 sat; -3,000.0000 USD and fee -24.0000 USD (4 dp).
    return NormalizedAssetFill(
        venue_fill_id=f"KT{n}-{uuid.uuid4().hex[:6]}",
        instrument=KRAKEN_BTC,
        action="buy",
        qty_atoms=5_000_000,
        price="60000",
        legs=[
            NormalizedLeg(asset="BTC", delta_atoms=5_000_000, decimals=8, kind="fill_base"),
            NormalizedLeg(asset="USD", delta_atoms=-30_000_000, decimals=4, kind="fill_quote"),
            NormalizedLeg(asset="USD", delta_atoms=-240_000, decimals=4, kind="fee"),
        ],
        filled_at=at,
    )


def test_kraken_spot_from_backfill_to_paper_book_and_stats_v2(pg, monkeypatch):
    market = ScriptedMarket("kraken", [KRAKEN_BTC], {"XXBTZUSD": "61000"})
    monkeypatch.setattr(prices, "build_market_data", lambda venue, env: market)
    venue = ScriptedVenue("kraken", "USD")
    runner = JobRunner(pg, None, worker_id="e2e", connector_factory=lambda account: venue)

    # Anchor $9,974 two days ago; T1 (yesterday) buys 0.05 BTC for $3,024 incl. fee.
    with pg() as db:
        account = _creator(db, "kraken", "USD", 9_974 * M, NOW - timedelta(days=2))
        account_id = account.id
        strategy_id = db.scalar(select(Strategy.id).where(Strategy.venue_account_id == account_id))
        venue.fills = [_kraken_buy(1, NOW - timedelta(days=1))]
        enqueue(db, JobKind.BACKFILL, account_id=account_id)
        db.commit()
    _drain(runner)  # backfill -> signals (T1 is stale for copying; no allocation yet)

    with pg() as db:
        enqueue(
            db,
            JobKind.PRICES_DAILY,
            payload={"venue": "kraken"},
            dedupe_key=f"prices_daily:kraken:{uuid.uuid4().hex}",
        )
        db.commit()
    _drain(runner)  # BTC closes at 61,000 through yesterday

    with pg() as db:
        allocation = _follow(db, db.get(Strategy, strategy_id), NOW - timedelta(hours=1))
        allocation_id = allocation.id
    # T2, ten minutes ago, is seen by an incremental sync while BTC quotes 59,990 / 60,030.
    market.quote = ("59990", "60030")
    venue.fills.append(_kraken_buy(2, NOW - timedelta(minutes=10)))
    with pg() as db:
        enqueue(db, JobKind.SYNC, account_id=account_id)
        db.commit()
    _drain(runner)  # sync -> signals -> mirror

    with pg() as db:
        [t2] = db.scalars(
            select(Signal)
            .where(Signal.strategy_id == strategy_id)
            .order_by(Signal.id.desc())
            .limit(1)
        ).all()
        # E(t2-) = cash 9,974 - 3,024 = 6,950 + 0.05 BTC x yesterday's 61,000 = 10,000.
        assert t2.creator_equity_micros == 10_000 * M
        assert (t2.position_before_atoms, t2.position_after_atoms) == (5_000_000, 10_000_000)
        assert (t2.quote_bid, t2.quote_ask, t2.notional_micros) == ("59990", "60030", 3_000 * M)

        allocation = db.get(Allocation, allocation_id)
        [fill] = db.scalars(
            select(MirrorAssetFill).where(MirrorAssetFill.allocation_id == allocation_id)
        ).all()
        # Follower: 0.05 x 1,000 / 10,000 = 0.005 BTC at the ask 60,030 = 300.15;
        # fee 80 bps = 2.4012 (to 100 micros); cash 1,000 - 300.15 - 2.4012 = 697.4488.
        assert (fill.qty_atoms, fill.price, fill.price_basis) == (500_000, "60030", "ask")
        assert (fill.notional_micros, fill.fee_micros) == (300_150_000, 2_401_200)
        book = load_book(db, allocation)
        assert book.cash_micros == 697_448_800
        assert book.holdings["BTC"].atoms == 500_000
        assert allocation.last_signal_id == t2.id
        assert (
            db.scalars(select(MirrorSkip).where(MirrorSkip.allocation_id == allocation_id)).all()
            == []
        )

        stats = compute_strategy_stats(db, db.get(Strategy, strategy_id))
        # Days D-2 .. D-1: 9,974 (cash only), then 6,950 + 0.05 x 61,000 = 10,000.
        assert stats.extras["valuation"] == "v2"
        assert [e for _, e in stats.extras["daily_equity"]] == [9_974 * M, 10_000 * M]
        assert stats.total_return == pytest.approx(10_000 / 9_974 - 1)


HL_PERP = NormalizedInstrument(
    symbol="BTC",
    type="perp",
    base_asset="BTC-PERP",
    quote_asset="USDC",
    qty_decimals=5,
    min_notional="10",
)


def test_hyperliquid_perp_copy_funding_and_liquidation_through_the_ticks(pg, monkeypatch):
    market = ScriptedMarket("hyperliquid", [HL_PERP], {"BTC": "60000"})
    market.tiers = [MarginTier(lower_bound="0", max_leverage=40)]
    monkeypatch.setattr(prices, "build_market_data", lambda venue, env: market)
    venue = ScriptedVenue("hyperliquid", "USDC")
    runner = JobRunner(pg, None, worker_id="e2e", connector_factory=lambda account: venue)

    with pg() as db:
        account = _creator(db, "hyperliquid", "USDC", 10_000 * M, NOW - timedelta(days=2))
        account_id = account.id
        strategy_id = db.scalar(select(Strategy.id).where(Strategy.venue_account_id == account_id))
        allocation_id = _follow(db, db.get(Strategy, strategy_id), NOW - timedelta(hours=1)).id
    # The creator opens 1 BTC long at 60,000 ten minutes ago (fee 27 USDC).
    venue.fills = [
        NormalizedAssetFill(
            venue_fill_id=f"HL-{uuid.uuid4().hex[:6]}",
            instrument=HL_PERP,
            action="buy",
            qty_atoms=100_000,
            price="60000",
            legs=[
                NormalizedLeg(asset="BTC-PERP", delta_atoms=100_000, decimals=5, kind="fill_base"),
                NormalizedLeg(asset="USDC", delta_atoms=-27 * M, decimals=6, kind="fee"),
            ],
            filled_at=NOW - timedelta(minutes=10),
        )
    ]
    market.quote = ("60000", "60010")
    with pg() as db:
        enqueue(db, JobKind.BACKFILL, account_id=account_id)
        db.commit()
    _drain(runner)  # backfill -> signals -> mirror

    with pg() as db:
        allocation = db.get(Allocation, allocation_id)
        book = load_book(db, allocation)
        # Notional 60,000 x 1,000 / 10,000 = 6,000 at ask 60,010 -> 0.09998 BTC (5 dp);
        # fee 5 bps of 5,999.7998 = 2.9999; cash 1,000 - 2.9999 = 997.0001.
        assert book.perps["BTC-PERP"].qty.atoms == 9_998
        assert book.cash_micros == 997_000_100

        # Margin tiers come from the nightly prices_daily job.
        enqueue(
            db,
            JobKind.PRICES_DAILY,
            payload={"venue": "hyperliquid"},
            dedupe_key=f"prices_daily:hyperliquid:{uuid.uuid4().hex}",
        )
        db.commit()
    _drain(runner)
    with pg() as db:
        perp = db.scalar(
            select(Instrument).where(Instrument.venue == "hyperliquid", Instrument.symbol == "BTC")
        )
        assert perp.margin_tiers and perp.margin_tiers[0]["max_leverage"] == 40

    # The PRICES tick: a funding rate of +0.0000125 at mark 60,000, then BTC crashes to 50,500.
    market.funding = [
        FundingRate(symbol="BTC", at=datetime.now(UTC), rate="0.0000125", mark_price="60000")
    ]
    market.quote = ("50490", "50510")
    with pg() as db:
        enqueue(db, JobKind.PRICES, dedupe_key=f"prices:{uuid.uuid4().hex}")
        db.commit()
    _drain(runner)
    with pg() as db:
        enqueue(
            db,
            JobKind.MIRROR,
            payload={"strategy_id": strategy_id},
            dedupe_key=f"m:{uuid.uuid4().hex}",
        )
        db.commit()
    _drain(runner)

    with pg() as db:
        allocation = db.get(Allocation, allocation_id)
        book = load_book(db, allocation)
        # Funding -0.09998 x 60,000 x 0.0000125 = -0.074985 -> 996.925115.
        # At mid 50,500: unrealized 0.09998 x (50,500 - 60,010) = -950.8098, AV 46.115315
        # < MM 0.09998 x 50,500 / 80 = 63.112375 -> liquidated; cash 46.115315.
        assert (allocation.status, allocation.stop_reason) == ("stopped", "liquidated")
        assert book.perps == {}
        assert book.cash_micros == 46_115_315
        reasons = db.scalars(
            select(MirrorAssetFill.reason)
            .where(MirrorAssetFill.allocation_id == allocation_id)
            .order_by(MirrorAssetFill.id)
        ).all()
        assert reasons == ["signal", "liquidation"]
