"""ORM models.

Project invariants (see CLAUDE.md):
- All money is integer MICRO-DOLLARS (1e-6 USD) via money.to_micros; contract
  quantities share the 1e6 scale. Floats never touch the ledger.
- All timestamps are timezone-aware UTC.
- fills / settlements are append-only: no UPDATE, no DELETE, ever.
  Corrections are new rows; idempotency keys on the unique venue ids.
- Venue credentials: only the Fernet-encrypted private key is stored
  (see vault.py), and it is erased on disconnect. Never logged, never
  returned by any API.
"""

from datetime import UTC, date, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
    false,
    text,
    true,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from alphamarket.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # p3-auth: set when the user follows the emailed link (account.py). An
    # unverified user can browse but cannot connect a venue.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # w3-seed (invariant 7): a sample user made by scripts/seed_demo.py. What a
    # demo user owns is public only while DEMO_MODE is on, always under the
    # "Sample data" banner; preflight refuses a database that has one.
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # Migration 0017. A house user is Koine itself, otherwise a normal user
    # (verified email, TOTP, step-up to connect; read-only keys only): its
    # strategies carry the "Run by Koine" badge and get no ranking advantage.
    is_house: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    # The one user that owns shared watch accounts (watchlist.system_user).
    # Its password hash is unusable and auth refuses it: it never logs in.
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())


class VenueAccountStatus(StrEnum):
    ACTIVE = "active"
    ERROR = "error"  # last sync failed; retried on the next worker tick
    DISCONNECTED = "disconnected"  # user disconnected; encrypted key erased
    SUSPENDED = "suspended"  # p3-vault: key became writable on a re-check; key erased
    # wave2: a connect started but not finished (SnapTrade portal round trip).
    # Never synced; abandoned by housekeeping after pending_connect_ttl_hours.
    PENDING = "pending"


class VenueAccount(Base):
    __tablename__ = "venue_accounts"
    # Two watchers of one address share one account and one ingestion.
    __table_args__ = (
        Index(
            "uq_venue_accounts_watch_address",
            "venue",
            "key_id",
            unique=True,
            postgresql_where=text("is_watch"),
            sqlite_where=text("is_watch"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    venue: Mapped[str] = mapped_column(String(40))
    venue_env: Mapped[str] = mapped_column(String(10))  # "demo" | "prod"
    key_id: Mapped[str] = mapped_column(String(120))
    # Fernet token of the RSA private key PEM. Nulled on disconnect. Deferred
    # (p3-vault): only loaded when read, so the web DB role, which has no
    # SELECT on it, can still load accounts.
    encrypted_private_key: Mapped[bytes | None] = mapped_column(
        LargeBinary, default=None, deferred=True
    )
    status: Mapped[str] = mapped_column(String(20), default=VenueAccountStatus.ACTIVE)
    scope_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_sync_error: Mapped[str | None] = mapped_column(Text, default=None)
    # Reconciliation anchor (M2): the venue cash balance observed at connect
    # time. Cash at any later moment = anchor + ledger cash flows after
    # anchor_at (see ledger.py). Fill history before the anchor is displayed
    # but never needed for reconciliation, so limited venue history depth
    # cannot break the "verified" claim.
    anchor_balance_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    anchor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Open positions the venue reported at anchor time, each
    # {ticker, position_micros (signed, +yes/-no), cost_micros}. Seeds the
    # equity replay so pre-anchor positions carry their real cost basis and
    # stale ones (settled outside returned history) cannot inflate equity.
    anchor_positions: Mapped[list | None] = mapped_column(JSON, default=None)
    # Ownership proof for credential-less venues (M7): the user puts
    # `claim_code` in their public venue profile and we check for it.
    # Key-based venues are claimed the moment a valid read-only key is
    # accepted. An unclaimed account's strategy is never shown publicly.
    claim_code: Mapped[str | None] = mapped_column(String(40), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # ---- p3-vault ----
    # Last ReadOnlyEvidence from connector.prove_read_only() (connect time and
    # the nightly re-check), as JSON. `scope_checked_at` stays "last time the
    # key was CONFIRMED read-only". The key itself lives in venue_credentials;
    # `encrypted_private_key` above is read for one release, never written.
    scope_evidence: Mapped[dict | None] = mapped_column(JSON, default=None)
    # Migration 0017: a public address on the watchlist, owned by the system
    # user, never claimed, no credential (watchlist.py).
    is_watch: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())

    @property
    def claimed(self) -> bool:
        return self.claimed_at is not None


class StrategyStatus(StrEnum):
    INCUBATING = "incubating"  # < 90 observed days (display badge, not a gate)
    LIVE = "live"
    QUARANTINED = "quarantined"  # reconciliation divergence (M2) or fraud flag (M3)
    DELISTED = "delisted"


class StrategyKind(StrEnum):
    CREATOR = "creator"  # a Koine user's own account
    # A public address on someone's private watchlist: never public, whoever
    # asks (invariant 4); reached only through a Watch row (watchlist.py).
    WATCH = "watch"


class Strategy(Base):
    """One Kalshi account maps to one strategy at MVP (see BUILD_PLAN 2.2)."""

    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    venue_account_id: Mapped[int] = mapped_column(
        ForeignKey("venue_accounts.id"),
        unique=True,  # one-account-one-strategy
    )
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")  # unverified, labeled as such
    style_tags: Mapped[str] = mapped_column(String(255), default="")  # comma-separated
    # Public page on/off (privacy default: page shows display name only, never email).
    public: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(20), default=StrategyStatus.INCUBATING)
    listed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(
        String(10), default=StrategyKind.CREATOR, server_default=StrategyKind.CREATOR.value
    )


class Fill(Base):
    """APPEND-ONLY. Never UPDATE, never DELETE (invariant #3)."""

    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    venue_fill_id: Mapped[str] = mapped_column(String(120), unique=True)
    ticker: Mapped[str] = mapped_column(String(120), index=True)
    side: Mapped[str] = mapped_column(String(10))  # yes | no
    action: Mapped[str] = mapped_column(String(10))  # buy | sell
    count_micros: Mapped[int] = mapped_column(BigInteger)
    price_micros: Mapped[int] = mapped_column(BigInteger)
    fee_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    # Venue-reported gross value when price x count is not exact (M7);
    # NULL means price x count, exactly. See ledger.fill_notional.
    notional_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Settlement(Base):
    """APPEND-ONLY. Never UPDATE, never DELETE (invariant #3)."""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    venue_settlement_id: Mapped[str] = mapped_column(String(200), unique=True)
    ticker: Mapped[str] = mapped_column(String(120), index=True)
    market_result: Mapped[str] = mapped_column(String(20))
    yes_count_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    no_count_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    revenue_micros: Mapped[int] = mapped_column(BigInteger)
    fee_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EquitySnapshot(Base):
    """One reconciliation run: venue-reported cash vs. ledger-computed cash."""

    __tablename__ = "equity_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    venue_balance_micros: Mapped[int] = mapped_column(BigInteger)
    computed_cash_micros: Mapped[int] = mapped_column(BigInteger)
    divergence_micros: Mapped[int] = mapped_column(BigInteger)  # venue - computed
    tolerance_micros: Mapped[int] = mapped_column(BigInteger)
    reconciled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # wave2: broker-reported total equity vs ledger equity at current marks
    # (asset venues whose NormalizedBalances carries equity_usd_micros). NULL
    # when not checked. A mismatch flags the stats; it never quarantines.
    venue_equity_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    computed_equity_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    equity_tolerance_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    equity_reconciled: Mapped[bool | None] = mapped_column(Boolean, default=None)


class StrategyStats(Base):
    """One stats run (M3). Ratios are floats because they are dimensionless;
    money fields stay integer micros (invariant #1)."""

    __tablename__ = "strategy_stats"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    track_days: Mapped[int] = mapped_column(Integer)
    n_fills: Mapped[int] = mapped_column(Integer)
    n_settlements: Mapped[int] = mapped_column(Integer)
    total_return: Mapped[float | None] = mapped_column(Float, default=None)
    max_drawdown: Mapped[float] = mapped_column(Float)
    sharpe: Mapped[float | None] = mapped_column(Float, default=None)
    sortino: Mapped[float | None] = mapped_column(Float, default=None)
    win_rate: Mapped[float | None] = mapped_column(Float, default=None)
    risk_score: Mapped[int] = mapped_column(Integer)
    equity_micros: Mapped[int] = mapped_column(BigInteger)
    realized_pnl_micros: Mapped[int] = mapped_column(BigInteger)
    flags: Mapped[list] = mapped_column(JSON, default=list)
    extras: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- market data, signals, following and paper tailing (M6) ------------------


class MarketPrice(Base):
    """One observation of a market's public quote. Append-only by convention
    (history is what lets a paper curve be re-derived); the newest row per
    ticker is the mark used for valuation."""

    __tablename__ = "market_prices"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue: Mapped[str] = mapped_column(String(40))
    venue_env: Mapped[str] = mapped_column(String(10))
    ticker: Mapped[str] = mapped_column(String(120), index=True)
    status: Mapped[str] = mapped_column(String(20))
    yes_bid_micros: Mapped[int] = mapped_column(BigInteger)
    yes_ask_micros: Mapped[int] = mapped_column(BigInteger)
    last_price_micros: Mapped[int] = mapped_column(BigInteger)
    result: Mapped[str] = mapped_column(String(20), default="")
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def yes_mark_micros(self) -> int | None:
        """Same rule as NormalizedMarket.yes_mark_micros."""
        if self.result == "yes":
            return 1_000_000
        if self.result == "no":
            return 0
        if self.yes_bid_micros > 0 and self.yes_ask_micros > 0:
            return (self.yes_bid_micros + self.yes_ask_micros) // 2
        if self.last_price_micros > 0:
            return self.last_price_micros
        return None


class Signal(Base):
    """APPEND-ONLY. One row per post-anchor creator fill, in fill order: the
    event followers monitor and paper allocations mirror. `creator_equity_micros`
    is the creator's equity (cash + open cost) just before the fill, which is
    the denominator of proportional sizing.

    wave2 (signals v2): exactly one source per row (ck_signals_one_source).
    - Binary rows: `fill_id` set; ticker/side/action/count/price/position_after
      as in M6; every v2 column NULL.
    - Asset rows: `asset_fill_id` set; `count_micros`, `price_micros` and
      `position_after_micros` NULL; `ticker` = instrument symbol; `side` =
      "long" | "short" by the sign of the creator's position after the fill
      (before it, when the fill flattened it); quantities in atoms at
      `qty_decimals`; `price` the venue's string; the quote Koine captured at
      `observed_at` copied onto the row (paper_signals.py, methodology
      "Signals v2 and copy lag").

    Binary-only readers (follows.signal_item, mirror.mirror_pending) are
    reached only through the binary dispatch. Never read signals without
    filtering on fill_id / asset_fill_id unless the code handles both shapes."""

    __tablename__ = "signals"
    __table_args__ = (
        CheckConstraint(
            "(fill_id IS NULL) <> (asset_fill_id IS NULL)", name="ck_signals_one_source"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    fill_id: Mapped[int | None] = mapped_column(ForeignKey("fills.id"), unique=True, default=None)
    ticker: Mapped[str] = mapped_column(String(160), index=True)
    side: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(10))
    count_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    price_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    creator_equity_micros: Mapped[int] = mapped_column(BigInteger)
    # creator's (ticker, side)
    position_after_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # ---- wave2: asset signals (NULL on binary rows) ----
    asset_fill_id: Mapped[int | None] = mapped_column(
        ForeignKey("asset_fills.id"), unique=True, default=None
    )
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id"), index=True, default=None
    )
    qty_atoms: Mapped[int | None] = mapped_column(BigInteger, default=None)  # unsigned
    qty_decimals: Mapped[int | None] = mapped_column(Integer, default=None)
    price: Mapped[str | None] = mapped_column(String(64), default=None)  # as reported
    # qty x price in USD micros; NULL when the quote asset is not USD/stablecoin.
    notional_micros: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # Creator's signed position in the instrument (+ long, - short) around the
    # fill, at qty_decimals.
    position_before_atoms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    position_after_atoms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    # The UTC day whose closes valued creator_equity_micros (valuation.equity_before).
    equity_basis_day: Mapped[date | None] = mapped_column(Date, default=None)
    # When Koine first saw the fill (asset_fills.ingested_at). Copy lag is
    # observed_at - filled_at.
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    quote_bid: Mapped[str | None] = mapped_column(String(64), default=None)
    quote_ask: Mapped[str | None] = mapped_column(String(64), default=None)
    quote_last: Mapped[str | None] = mapped_column(String(64), default=None)
    quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    quote_source: Mapped[str | None] = mapped_column(String(40), default=None)

    @property
    def is_asset(self) -> bool:
        return self.asset_fill_id is not None


class Follow(Base):
    """A user watching a strategy: feeds and notifications, nothing else."""

    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("user_id", "strategy_id", name="uq_follows_user_strategy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AllocationMode(StrEnum):
    PAPER = "paper"  # virtual capital; nothing is placed anywhere
    LIVE = "live"  # NOT AVAILABLE: needs a licensed party (see execution.py)


class AllocationStatus(StrEnum):
    ACTIVE = "active"
    STOPPED = "stopped"


class Allocation(Base):
    """A follower's tail of one strategy. In paper mode the allocation is a
    virtual cash balance that mirrors the creator's signals proportionally;
    the resulting positions are valued at public market marks."""

    __tablename__ = "allocations"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    mode: Mapped[str] = mapped_column(String(10), default=AllocationMode.PAPER)
    allocation_micros: Mapped[int] = mapped_column(BigInteger)  # starting virtual cash
    drawdown_limit_bps: Mapped[int] = mapped_column(Integer)  # e.g. 1500 = stop at -15%
    status: Mapped[str] = mapped_column(String(20), default=AllocationStatus.ACTIVE)
    stop_reason: Mapped[str | None] = mapped_column(String(40), default=None)
    high_water_micros: Mapped[int] = mapped_column(BigInteger)
    # Signals with id <= this have been considered (mirrored or skipped).
    last_signal_id: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class MirrorFill(Base):
    """APPEND-ONLY. A follower-side fill produced from one signal. Paper rows
    carry the modelled price and fee; a live row (not available) would carry
    the venue's."""

    __tablename__ = "mirror_fills"

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), index=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    mode: Mapped[str] = mapped_column(String(10))
    ticker: Mapped[str] = mapped_column(String(120), index=True)
    side: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(10))
    count_micros: Mapped[int] = mapped_column(BigInteger)
    price_micros: Mapped[int] = mapped_column(BigInteger)
    fee_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    venue_order_id: Mapped[str | None] = mapped_column(String(120), default=None)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MirrorSettlement(Base):
    """APPEND-ONLY. A follower position closed by market settlement, paying
    $1 per contract on the winning side and nothing on the other."""

    __tablename__ = "mirror_settlements"
    __table_args__ = (
        UniqueConstraint("allocation_id", "ticker", name="uq_mirror_settlements_alloc_ticker"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), index=True)
    ticker: Mapped[str] = mapped_column(String(120), index=True)
    market_result: Mapped[str] = mapped_column(String(20))
    yes_count_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    no_count_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    revenue_micros: Mapped[int] = mapped_column(BigInteger)
    settled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AllocationSnapshot(Base):
    """One valuation of an allocation (nightly and on stop): the follower's
    curve is the series of these rows."""

    __tablename__ = "allocation_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cash_micros: Mapped[int] = mapped_column(BigInteger)
    open_cost_micros: Mapped[int] = mapped_column(BigInteger)
    open_value_micros: Mapped[int] = mapped_column(BigInteger)
    equity_micros: Mapped[int] = mapped_column(BigInteger)
    unmarked_cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- append-only enforcement (invariant #3) ---------------------------------

APPEND_ONLY_MODELS = (Fill, Settlement, Signal, MirrorFill, MirrorSettlement)


class AppendOnlyViolation(RuntimeError):
    pass


@event.listens_for(Session, "before_flush")
def _enforce_append_only(session: Session, flush_context, instances) -> None:
    """ORM-level guard: any attempt to UPDATE or DELETE a ledger row fails
    before it reaches the database. Postgres additionally enforces this with
    triggers (migration 0003), so it holds even for raw SQL."""
    for obj in session.dirty:
        if isinstance(obj, APPEND_ONLY_MODELS) and session.is_modified(obj):
            raise AppendOnlyViolation(f"{type(obj).__name__} rows are append-only; cannot update")
    for obj in session.deleted:
        if isinstance(obj, APPEND_ONLY_MODELS):
            raise AppendOnlyViolation(f"{type(obj).__name__} rows are append-only; cannot delete")


# ---- p1-ledger ----
# Ledger v2 (platform_plan 2.1): instruments, legs, asset fills, anchors and
# daily prices. Asset quantities are integer atoms at a per-row `decimals`
# (<= 12), parsed from the venue's decimal string via money.to_atoms.

from sqlalchemy import Index  # noqa: E402

MAX_LEDGER_DECIMALS = 12  # money.MAX_DECIMALS; the CHECK constraints pin it in the schema


def _decimals_check(column: str, table: str) -> CheckConstraint:
    return CheckConstraint(
        f"{column} >= 0 AND {column} <= {MAX_LEDGER_DECIMALS}", name=f"ck_{table}_{column}"
    )


# SQLite only auto-increments an INTEGER PRIMARY KEY; Postgres gets BIGINT.
_BigId = BigInteger().with_variant(Integer, "sqlite")


class Instrument(Base):
    """One tradable thing on one venue, exactly as the venue names it. Binary
    venues register one row per market ticker (base_asset = ticker)."""

    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("venue", "symbol", name="uq_instruments_venue_symbol"),
        _decimals_check("qty_decimals", "instruments"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str] = mapped_column(String(160))
    type: Mapped[str] = mapped_column(String(20))  # connectors.base.InstrumentType
    base_asset: Mapped[str] = mapped_column(String(120))
    quote_asset: Mapped[str] = mapped_column(String(40))
    qty_decimals: Mapped[int] = mapped_column(Integer)
    qty_step_atoms: Mapped[int] = mapped_column(BigInteger, default=1)
    min_qty_atoms: Mapped[int] = mapped_column(BigInteger, default=0)
    min_notional: Mapped[str | None] = mapped_column(String(64), default=None)  # decimal string
    multiplier: Mapped[int] = mapped_column(Integer, default=1)
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # wave2: perp maintenance-margin tiers, [{"lower_bound": str, "max_leverage": int}]
    # ascending (connectors.base.MarginTier), refreshed by the prices_daily job.
    margin_tiers: Mapped[list | None] = mapped_column(JSON, default=None)
    margin_tiers_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # 0019: False = paper copying skips it ("not_copyable"); signals are still
    # recorded (connectors.base.NormalizedInstrument.copyable).
    copyable: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())

    @property
    def label(self) -> str:
        """How the instrument reads on a page: spot pairs as BASE/QUOTE in
        canonical codes (Kraken's XXBTZUSD is BTC/USD), perps by their position
        code (BTC-PERP), stocks and contracts by their symbol."""
        if self.type == "spot":
            return f"{self.base_asset}/{self.quote_asset}"
        if self.type == "perp":
            base = self.base_asset
            return base if base.endswith("-PERP") else f"{base}-PERP"
        return self.symbol


class SymbolHistory(Base):
    """Which symbol an instrument traded under, and when (renames, mergers)."""

    __tablename__ = "symbol_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(160))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class LedgerEntry(Base):
    """APPEND-ONLY. One leg: the signed change of one asset in one venue
    account. Every fill, settlement and event is a group of legs sharing
    `group_ref`; balances are sums of legs (ledger.balance_vector)."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "venue_account_id", "venue_ref", "kind", "asset", name="uq_ledger_entries_identity"
        ),
        _decimals_check("decimals", "ledger_entries"),
        Index("ix_ledger_entries_account_group", "venue_account_id", "group_ref"),
        Index("ix_ledger_entries_account_asset", "venue_account_id", "asset"),
    )

    id: Mapped[int] = mapped_column(_BigId, primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), default=None)
    asset: Mapped[str] = mapped_column(String(120))
    delta_atoms: Mapped[int] = mapped_column(BigInteger)
    decimals: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))  # connectors.base.LegKind
    venue_ref: Mapped[str] = mapped_column(String(255))
    group_ref: Mapped[str] = mapped_column(String(300))
    balance_after_atoms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetFill(Base):
    """APPEND-ONLY. The non-leg fields of a NormalizedAssetFill (what signals
    and paper copying read); the cash truth is its ledger legs."""

    __tablename__ = "asset_fills"
    __table_args__ = (
        UniqueConstraint("venue_account_id", "venue_fill_id", name="uq_asset_fills_account_fill"),
        _decimals_check("qty_decimals", "asset_fills"),
    )

    id: Mapped[int] = mapped_column(_BigId, primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), index=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    venue_fill_id: Mapped[str] = mapped_column(String(255))
    order_id: Mapped[str | None] = mapped_column(String(255), default=None)
    action: Mapped[str] = mapped_column(String(10))  # buy | sell
    qty_atoms: Mapped[int] = mapped_column(BigInteger)
    qty_decimals: Mapped[int] = mapped_column(Integer)
    price: Mapped[str] = mapped_column(String(64))  # decimal string, as reported
    liquidity: Mapped[str | None] = mapped_column(String(10), default=None)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnchorBalance(Base):
    """One asset of the venue's balance vector at anchor time (asset venues).
    The newest `as_of` set per account is the reconciliation anchor."""

    __tablename__ = "anchor_balances"
    __table_args__ = (
        UniqueConstraint("venue_account_id", "asset", "as_of", name="uq_anchor_balances_asset_at"),
        _decimals_check("decimals", "anchor_balances"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), index=True)
    asset: Mapped[str] = mapped_column(String(120))
    atoms: Mapped[int] = mapped_column(BigInteger)
    decimals: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[str | None] = mapped_column(String(64), default=None)
    mark_price: Mapped[str | None] = mapped_column(String(64), default=None)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PriceDaily(Base):
    """One UTC daily close per instrument. `close` is the venue's string;
    `usd_micros_per_unit` is filled only when the quote is USD or a
    stablecoin valued at par."""

    __tablename__ = "prices_daily"
    __table_args__ = (UniqueConstraint("instrument_id", "day", name="uq_prices_daily_inst_day"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    day: Mapped[date] = mapped_column(Date)
    close: Mapped[str] = mapped_column(String(64))
    usd_micros_per_unit: Mapped[int | None] = mapped_column(BigInteger, default=None)
    source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerSyncState(Base):
    """Per-account, per-stream resume cursor (connectors.base.Page): the
    opaque state the final page of a pass hands back for the next sync."""

    __tablename__ = "ledger_sync_state"
    __table_args__ = (
        UniqueConstraint("venue_account_id", "stream", name="uq_ledger_sync_state_stream"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"))
    stream: Mapped[str] = mapped_column(String(20))  # "asset_fills" | "events"
    resume_cursor: Mapped[str | None] = mapped_column(Text, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LedgerQuarantine(Base):
    """APPEND-ONLY. Why an account's strategy was quarantined by the ledger
    (a mismatching or partial duplicate event, a broken venue running
    balance, a per-asset divergence). A human reads these before
    un-quarantining."""

    __tablename__ = "ledger_quarantines"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    ref: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetReconciliation(Base):
    """One asset of one reconciliation run on an asset venue (reconcile v2).
    The run's overall verdict is also written as an EquitySnapshot."""

    __tablename__ = "asset_reconciliations"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), index=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    asset: Mapped[str] = mapped_column(String(120))
    decimals: Mapped[int] = mapped_column(Integer)
    venue_atoms: Mapped[int] = mapped_column(BigInteger)
    computed_atoms: Mapped[int] = mapped_column(BigInteger)
    divergence_atoms: Mapped[int] = mapped_column(BigInteger)  # venue - computed
    tolerance_atoms: Mapped[int] = mapped_column(BigInteger)
    reconciled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


APPEND_ONLY_MODELS_V2 = (LedgerEntry, AssetFill, LedgerQuarantine)


@event.listens_for(Session, "before_flush")
def _enforce_append_only_v2(session: Session, flush_context, instances) -> None:
    """Same guard as _enforce_append_only for the ledger v2 tables; Postgres
    triggers (migration 0009) back it for raw SQL."""
    for obj in session.dirty:
        if isinstance(obj, APPEND_ONLY_MODELS_V2) and session.is_modified(obj):
            raise AppendOnlyViolation(f"{type(obj).__name__} rows are append-only; cannot update")
    for obj in session.deleted:
        if isinstance(obj, APPEND_ONLY_MODELS_V2):
            raise AppendOnlyViolation(f"{type(obj).__name__} rows are append-only; cannot delete")


# ---- p2-jobs ----
# Job queue, per-account sync schedule, outbound rate buckets and per-venue
# circuit breakers (platform_plan 2.5, implementation_plan P2). None of these
# are ledger facts: they are mutable coordination state, so invariant 3 does
# not apply (a job row is claimed, leased, retried and finished in place).

from sqlalchemy import Index, text  # noqa: E402  (kept in this block to avoid merge conflicts)


class JobStatus(StrEnum):
    PENDING = "pending"  # waiting for run_after
    RUNNING = "running"  # claimed by a worker; lease_until says until when
    DONE = "done"
    DEAD = "dead"  # dead-lettered after max_attempts failures


ACTIVE_JOB_STATUSES = (JobStatus.PENDING, JobStatus.RUNNING)


class Job(Base):
    """One unit of worker work. See jobs.py for the queue protocol and
    docs/worker.md for the kinds and priorities."""

    __tablename__ = "jobs"
    __table_args__ = (
        # The same dedupe key (default kind + account) is never queued twice
        # while a job for it is pending or running.
        Index(
            "uq_jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
            sqlite_where=text("status IN ('pending', 'running')"),
        ),
        Index("ix_jobs_claim", "status", "priority", "run_after"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    venue_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("venue_accounts.id"), index=True, default=None
    )
    dedupe_key: Mapped[str] = mapped_column(String(200))
    # Handler input; a backfill keeps its connector cursor here. Never secrets.
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer)  # lower runs first
    status: Mapped[str] = mapped_column(String(12), default=JobStatus.PENDING)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)  # failures so far
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    deferrals: Mapped[int] = mapped_column(Integer, default=0)  # budget/rate-limit reschedules
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    locked_by: Mapped[str | None] = mapped_column(String(80), default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AccountSchedule(Base):
    """Per-account worker state kept off venue_accounts: when the next
    incremental sync is due (deterministic per-account jitter, see
    jobs.next_sync_slot) and the resume cursors each page stream returned
    on its last completed pass (connectors.base.Page.resume_cursor)."""

    __tablename__ = "account_schedules"

    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), primary_key=True)
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    resume_cursors: Mapped[dict] = mapped_column(JSON, default=dict)  # stream -> cursor
    backfill_done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RateBucket(Base):
    """One outbound token bucket (venue_limits.PostgresBuckets). Integer
    milli-tokens and epoch milliseconds so the refill+take UPDATE is exact
    integer arithmetic. capacity/refill mirror the profile's RateBudget and
    are rewritten on every take."""

    __tablename__ = "rate_buckets"

    bucket_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    capacity_milli: Mapped[int] = mapped_column(BigInteger)
    refill_per_minute: Mapped[int] = mapped_column(Integer)
    level_milli: Mapped[int] = mapped_column(BigInteger)  # may go negative after after()
    updated_ms: Mapped[int] = mapped_column(BigInteger)
    penalty_until_ms: Mapped[int] = mapped_column(BigInteger, default=0)  # refill halved until
    blocked_until_ms: Mapped[int] = mapped_column(BigInteger, default=0)  # venue Retry-After


class VenueCircuit(Base):
    """Per-venue circuit breaker: consecutive 5xx/timeouts, open until."""

    __tablename__ = "venue_circuits"

    venue: Mapped[str] = mapped_column(String(40), primary_key=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    open_until_ms: Mapped[int] = mapped_column(BigInteger, default=0)


# ---- p3-vault ----------------------------------------------------------------


class VenueCredential(Base):
    """Envelope-encrypted venue credential (vault.py, platform_plan 2.4).

    Mutable on purpose: rotation re-wraps `wrapped_dek`, revocation erases
    every ciphertext column and stamps `revoked_at`. At most one row per
    account has revoked_at NULL (the active credential). The web role cannot
    SELECT the ciphertext columns (docs/security_roles.sql)."""

    __tablename__ = "venue_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # CredentialKind value
    # Deferred: loaded only on access, so ordinary queries never select them
    # (and the web role, which cannot, still works).
    ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, default=None, deferred=True)
    wrapped_dek: Mapped[bytes | None] = mapped_column(  # NULL = legacy row
        LargeBinary, default=None, deferred=True
    )
    key_version: Mapped[str] = mapped_column(String(200))
    account_ref: Mapped[str | None] = mapped_column(String(120), default=None)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    refresh_ciphertext: Mapped[bytes | None] = mapped_column(
        LargeBinary, default=None, deferred=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AuditLog(Base):
    """APPEND-ONLY security audit trail (invariant 3 applies: ORM guard below
    plus the Postgres trigger audit_log_append_only). `details` never holds a
    secret: audit.record() scrubs it. `actor_user_id` is a plain column, not a
    foreign key, so an audit row outlives the user it names."""

    __tablename__ = "audit_log"
    # wave2: the security page lists a user's own keys' rows by target.
    __table_args__ = (Index("ix_audit_log_target", "target_type", "target_id"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(Integer, default=None, index=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str | None] = mapped_column(String(40), default=None)
    target_id: Mapped[str | None] = mapped_column(String(80), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(400), default=None)
    request_or_job_id: Mapped[str | None] = mapped_column(String(80), default=None)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


@event.listens_for(Session, "before_flush")
def _enforce_audit_append_only(session: Session, flush_context, instances) -> None:
    """Same guard as _enforce_append_only, for audit_log (kept separate so the
    ledger tuple above can grow without touching this block)."""
    for obj in session.dirty:
        if isinstance(obj, AuditLog) and session.is_modified(obj):
            raise AppendOnlyViolation("AuditLog rows are append-only; cannot update")
    for obj in session.deleted:
        if isinstance(obj, AuditLog):
            raise AppendOnlyViolation("AuditLog rows are append-only; cannot delete")


# ---- p3-auth ----------------------------------------------------------------
# Account security (P3): DB sessions, TOTP, recovery codes, single-use email
# tokens and inbound rate-limit counters. None of these is a ledger, so none
# is append-only; secrets are only ever stored hashed or vault-encrypted.


class UserSession(Base):
    """A login. The cookie holds a random opaque id; only its SHA-256 is
    stored, so a database read cannot be replayed as a cookie. A session in
    `mfa_pending` state has passed the password step only and authenticates
    nothing but the TOTP step (sessions.py)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    mfa_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last TOTP (or recovery code) check on this session: the step-up window.
    step_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )


class UserTotp(Base):
    """TOTP second factor. The shared secret is vault-encrypted (vault.py);
    `enabled_at` is NULL while enrolment awaits its first valid code."""

    __tablename__ = "user_totp"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Highest 30-second time step accepted so far: a code is never accepted twice.
    last_used_step: Mapped[int | None] = mapped_column(BigInteger, default=None)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecoveryCode(Base):
    """Single-use TOTP recovery code, stored as SHA-256 of the normalized code
    (80 random bits each, so a fast hash is enough)."""

    __tablename__ = "totp_recovery_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class AuthTokenKind(StrEnum):
    VERIFY_EMAIL = "verify_email"
    PASSWORD_RESET = "password_reset"
    EMAIL_CHANGE = "email_change"  # wave2: sent to the NEW address; carries new_email


class AuthToken(Base):
    """Single-use emailed token (email verification, password reset). Only the
    SHA-256 of the token is stored; `used_at` makes it single-use."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # wave2: the address an EMAIL_CHANGE token switches the account to.
    new_email: Mapped[str | None] = mapped_column(String(320), default=None)


class RateLimitCounter(Base):
    """One fixed window of one inbound rate-limit key (ratelimit.py). Rows
    past `expires_at` are dead weight and pruned."""

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # unix seconds
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


# ---- wave2 scaffold -----------------------------------------------------------
# Paper copy v2 (implementation_plan P6), asset market data, consent
# (invariant 10) and venue activity flags. Migration 0013. Workstreams append
# their own blocks below this one ("# ---- wave2:<label> ----").


class MirrorAssetFill(Base):
    """APPEND-ONLY. One paper trade in an allocation of an ASSET strategy: the
    human-readable row (what was traded, at which price and why that price).
    The cash and quantity truth is its group of mirror_ledger_entries
    (paper_book.py). Binary allocations keep using mirror_fills."""

    __tablename__ = "mirror_asset_fills"
    __table_args__ = (
        Index(
            "uq_mirror_asset_fills_alloc_signal",
            "allocation_id",
            "signal_id",
            unique=True,
            postgresql_where=text("signal_id IS NOT NULL"),
            sqlite_where=text("signal_id IS NOT NULL"),
        ),
        _decimals_check("qty_decimals", "mirror_asset_fills"),
    )

    id: Mapped[int] = mapped_column(_BigId, primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), index=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id"), index=True, default=None
    )
    reason: Mapped[str] = mapped_column(String(20))  # "signal" | "liquidation"
    mode: Mapped[str] = mapped_column(String(10))  # AllocationMode; always paper
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    action: Mapped[str] = mapped_column(String(10))  # buy | sell
    qty_atoms: Mapped[int] = mapped_column(BigInteger)  # unsigned
    qty_decimals: Mapped[int] = mapped_column(Integer)
    price: Mapped[str] = mapped_column(String(64))  # decimal string, quote per unit
    notional_micros: Mapped[int] = mapped_column(BigInteger)  # qty x price, USD micros
    fee_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    # "ask" | "bid" | "creator_price_penalty" | "mark" (liquidation)
    price_basis: Mapped[str] = mapped_column(String(30))
    penalty_bps: Mapped[int | None] = mapped_column(Integer, default=None)
    quote_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    venue_order_id: Mapped[str | None] = mapped_column(String(120), default=None)  # live only
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MirrorLedgerEntry(Base):
    """APPEND-ONLY. One leg of a paper allocation's ledger, shaped like
    ledger_entries but keyed by allocation, so valuation.py values creators
    and followers with the same code. Starting cash is NOT a row: it is the
    allocation's implicit anchor (USD, 6 dp = micros). Leg and ref
    conventions: paper_book.py."""

    __tablename__ = "mirror_ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "allocation_id", "ref", "kind", "asset", name="uq_mirror_ledger_entries_identity"
        ),
        _decimals_check("decimals", "mirror_ledger_entries"),
        Index("ix_mirror_ledger_entries_alloc_asset", "allocation_id", "asset"),
        # w3-perf (migration 0016): checkpoint deltas and counts, the book's
        # newest event, and the per-perp funding watermark / booked refs.
        Index("ix_mirror_ledger_entries_alloc_id", "allocation_id", "id"),
        Index("ix_mirror_ledger_entries_alloc_at", "allocation_id", "occurred_at"),
        Index(
            "ix_mirror_ledger_entries_alloc_inst_kind_at",
            "allocation_id",
            "instrument_id",
            "kind",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(_BigId, primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"))
    instrument_id: Mapped[int | None] = mapped_column(ForeignKey("instruments.id"), default=None)
    asset: Mapped[str] = mapped_column(String(120))
    delta_atoms: Mapped[int] = mapped_column(BigInteger)
    decimals: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))  # connectors.base.LegKind
    ref: Mapped[str] = mapped_column(String(255))  # idempotency: sig:<id>, fund:..., ca:<id>
    group_ref: Mapped[str] = mapped_column(String(300))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MirrorSkip(Base):
    """A signal an allocation considered and did not copy, and why
    (below_minimum_size, insufficient_cash, stale_signal, ...). Display only;
    not a ledger fact, so not append-only."""

    __tablename__ = "mirror_skips"
    __table_args__ = (
        UniqueConstraint("allocation_id", "signal_id", name="uq_mirror_skips_alloc_signal"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), index=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"))
    reason: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetQuote(Base):
    """One observation of an instrument's public top of book (decimal strings,
    quote asset per unit). Append-only by convention like market_prices, but
    prunable: signals copy the quote they used, so no evidence lives only
    here. The newest fresh row is the mark for follower valuation and
    liquidation checks (prices.latest_mark)."""

    __tablename__ = "asset_quotes"
    __table_args__ = (Index("ix_asset_quotes_inst_as_of", "instrument_id", "as_of"),)

    id: Mapped[int] = mapped_column(_BigId, primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"))
    bid: Mapped[str | None] = mapped_column(String(64), default=None)
    ask: Mapped[str | None] = mapped_column(String(64), default=None)
    last: Mapped[str | None] = mapped_column(String(64), default=None)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(40))  # venue or data source name
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FundingRateRow(Base):
    """One perp funding rate (public venue data); paper perps accrue it."""

    __tablename__ = "funding_rates"
    __table_args__ = (UniqueConstraint("instrument_id", "at", name="uq_funding_rates_inst_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    instrument_id: Mapped[int] = mapped_column(ForeignKey("instruments.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    rate: Mapped[str] = mapped_column(String(64))  # decimal string per interval
    mark_price: Mapped[str | None] = mapped_column(String(64), default=None)
    # 0019: what shorts pay (positive = shorts pay) on asymmetric venues; None
    # = shorts receive `rate` (connectors.base.FundingRate.short_rate).
    short_rate: Mapped[str | None] = mapped_column(String(64), default=None)
    source: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CorporateActionRow(Base):
    """A split, cash dividend or symbol change (public data). Keyed by symbol,
    not instrument, because one equity is shared by Alpaca, IBKR and
    SnapTrade accounts. Paper equity followers apply these
    (paper_lifecycle.py); creators get theirs from their venue's legs."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint(
            "source", "symbol", "kind", "ex_date", name="uq_corporate_actions_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # connectors.base.CorporateActionKind
    ex_date: Mapped[date] = mapped_column(Date, index=True)
    pay_date: Mapped[date | None] = mapped_column(Date, default=None)
    ratio_new: Mapped[int | None] = mapped_column(Integer, default=None)
    ratio_old: Mapped[int | None] = mapped_column(Integer, default=None)
    cash_per_share: Mapped[str | None] = mapped_column(String(64), default=None)
    new_symbol: Mapped[str | None] = mapped_column(String(40), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VenueActivityFlag(Base):
    """APPEND-ONLY. Venue activity Koine skipped instead of guessing at (a
    short sale, an option, a stock dividend): disclosed on the account and
    its public page. A real balance effect is still caught by reconciliation."""

    __tablename__ = "venue_activity_flags"
    __table_args__ = (
        UniqueConstraint(
            "venue_account_id", "venue_ref", "kind", name="uq_venue_activity_flags_identity"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue_account_id: Mapped[int] = mapped_column(ForeignKey("venue_accounts.id"), index=True)
    venue_ref: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(30))  # "unsupported_activity"
    activity_type: Mapped[str] = mapped_column(String(60))
    reason: Mapped[str] = mapped_column(String(40))
    symbol: Mapped[str | None] = mapped_column(String(160), default=None)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ConsentText(Base):
    """APPEND-ONLY. The exact consent text shown before a connect, stored once
    per distinct body; venue_consents point at it by hash (invariant 10)."""

    __tablename__ = "consent_texts"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    venue: Mapped[str] = mapped_column(String(40))
    version: Mapped[str] = mapped_column(String(40))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VenueConsent(Base):
    """APPEND-ONLY. One accepted consent screen: who, for which venue and
    account, which exact text (by hash), from where, when."""

    __tablename__ = "venue_consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    venue: Mapped[str] = mapped_column(String(40))
    venue_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("venue_accounts.id"), index=True, default=None
    )
    text_sha256: Mapped[str] = mapped_column(ForeignKey("consent_texts.sha256"))
    text_version: Mapped[str] = mapped_column(String(40))
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(400), default=None)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


APPEND_ONLY_MODELS_W2 = (
    MirrorAssetFill,
    MirrorLedgerEntry,
    VenueActivityFlag,
    ConsentText,
    VenueConsent,
)


@event.listens_for(Session, "before_flush")
def _enforce_append_only_w2(session: Session, flush_context, instances) -> None:
    """Same guard as _enforce_append_only for the wave2 tables; Postgres
    triggers (migration 0013) back it for raw SQL."""
    for obj in session.dirty:
        if isinstance(obj, APPEND_ONLY_MODELS_W2) and session.is_modified(obj):
            raise AppendOnlyViolation(f"{type(obj).__name__} rows are append-only; cannot update")
    for obj in session.deleted:
        if isinstance(obj, APPEND_ONLY_MODELS_W2):
            raise AppendOnlyViolation(f"{type(obj).__name__} rows are append-only; cannot delete")


# ---- wave2:market-data ----
# Migration 0014.


class MarketDataCoverage(Base):
    """What the prices_daily job has already asked a data source for, so a
    rerun makes no request. Not a fact about prices (those are prices_daily /
    corporate_actions rows): deleting a row only costs one repeat request.

    kind "closes": source = the instrument's venue, scope = its symbol,
    covered_from..covered_through = the day range already fetched.
    kind "corporate_actions": scope = the equity symbol, fetched_on = the last
    UTC day it was fetched.
    kind "unpriced": scope = an asset no source prices, fetched_on = the last
    UTC day that was checked."""

    __tablename__ = "market_data_coverage"
    __table_args__ = (
        UniqueConstraint("source", "kind", "scope", name="uq_market_data_coverage_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(30))
    scope: Mapped[str] = mapped_column(String(160))
    covered_from: Mapped[date | None] = mapped_column(Date, default=None)
    covered_through: Mapped[date | None] = mapped_column(Date, default=None)
    fetched_on: Mapped[date] = mapped_column(Date)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- w3-perf ----
# Migration 0016.


class PaperBookCheckpoint(Base):
    """A CACHE, not a ledger fact, and deliberately NOT append-only: the end
    state of one paper allocation's book (cash, holdings, perp book with
    entries) folded from its mirror_ledger_entries with id <= through_entry_id
    and its perp mirror_asset_fills with id <= through_fill_id, so a pass
    folds only the rows after them (paper_book.book_state). Derived entirely
    from those append-only tables: it can be deleted at any time and is
    rebuilt by the next mirror pass. Used only after entry_count / fill_count
    match a fresh count of the rows at or below the through ids (a row that
    committed late is caught there); any mismatch falls back to a full replay
    and is logged. Written only by paper.process_asset_allocation, which runs
    under the allocation's row lock in the worker. `state` is
    valuation.LedgerFold.to_state(): integers and fractions as strings."""

    __tablename__ = "paper_book_checkpoints"

    allocation_id: Mapped[int] = mapped_column(ForeignKey("allocations.id"), primary_key=True)
    through_entry_id: Mapped[int] = mapped_column(BigInteger)
    entry_count: Mapped[int] = mapped_column(BigInteger)
    # max occurred_at of the folded entries / filled_at of the folded fills:
    # a book asked for at an earlier instant cannot start from this state.
    entries_through_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    through_fill_id: Mapped[int] = mapped_column(BigInteger)
    fill_count: Mapped[int] = mapped_column(BigInteger)
    fills_through_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    state: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- watchlist (migration 0017) ----


class Watch(Base):
    """A user's private watch of a public address's strategy. Mutable: removing
    the watch deletes the row. `label` is the user's own nickname for the
    address, shown to nobody else."""

    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("user_id", "strategy_id", name="uq_watches_user_strategy"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    label: Mapped[str] = mapped_column(String(60), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchOptout(Base):
    """APPEND-ONLY. The owner of an address proved it and asked not to be
    tracked: adding it to a watchlist is refused and existing watches stop."""

    __tablename__ = "watch_optouts"
    __table_args__ = (UniqueConstraint("venue", "address", name="uq_watch_optouts_venue_address"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    venue: Mapped[str] = mapped_column(String(40))
    address: Mapped[str] = mapped_column(String(120))  # addresses.normalize (EVM lowercased)
    proved_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaderboardSnapshot(Base):
    """A CACHE of one venue's public leaderboard for one period, refreshed by
    the watch_discover job (discover.py) and read by /watch/discover. Mutable:
    each refresh replaces `rows`. Never a Koine page, never public, never
    indexed; the venue's own figures, not Koine-computed ones. `rows` holds
    discover.snapshot_row dicts: address + integer micros, no identity
    fields (invariant 1 for the money, plan "never link an address to an
    identity" for the rest)."""

    __tablename__ = "leaderboard_snapshots"
    __table_args__ = (
        UniqueConstraint("venue", "period", name="uq_leaderboard_snapshots_venue_period"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    venue: Mapped[str] = mapped_column(String(40))
    period: Mapped[str] = mapped_column(String(10))  # connectors.base.LeaderboardPeriod
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rows: Mapped[list] = mapped_column(JSON, default=list)


@event.listens_for(Session, "before_flush")
def _enforce_append_only_watch(session: Session, flush_context, instances) -> None:
    """Same guard as _enforce_append_only for watch_optouts; the Postgres
    trigger (migration 0017) backs it for raw SQL."""
    for obj in session.dirty:
        if isinstance(obj, WatchOptout) and session.is_modified(obj):
            raise AppendOnlyViolation("WatchOptout rows are append-only; cannot update")
    for obj in session.deleted:
        if isinstance(obj, WatchOptout):
            raise AppendOnlyViolation("WatchOptout rows are append-only; cannot delete")
