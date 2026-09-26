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

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
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


class VenueAccountStatus(StrEnum):
    ACTIVE = "active"
    ERROR = "error"  # last sync failed; retried on the next worker tick
    DISCONNECTED = "disconnected"  # user disconnected; encrypted key erased


class VenueAccount(Base):
    __tablename__ = "venue_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    venue: Mapped[str] = mapped_column(String(40))
    venue_env: Mapped[str] = mapped_column(String(10))  # "demo" | "prod"
    key_id: Mapped[str] = mapped_column(String(120))
    # Fernet token of the RSA private key PEM. Nulled on disconnect.
    encrypted_private_key: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
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

    @property
    def claimed(self) -> bool:
        return self.claimed_at is not None


class StrategyStatus(StrEnum):
    INCUBATING = "incubating"  # < 90 observed days (display badge, not a gate)
    LIVE = "live"
    QUARANTINED = "quarantined"  # reconciliation divergence (M2) or fraud flag (M3)
    DELISTED = "delisted"


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
    the denominator of proportional sizing."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    strategy_id: Mapped[int] = mapped_column(ForeignKey("strategies.id"), index=True)
    fill_id: Mapped[int] = mapped_column(ForeignKey("fills.id"), unique=True)
    ticker: Mapped[str] = mapped_column(String(120), index=True)
    side: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(10))
    count_micros: Mapped[int] = mapped_column(BigInteger)
    price_micros: Mapped[int] = mapped_column(BigInteger)
    creator_equity_micros: Mapped[int] = mapped_column(BigInteger)
    position_after_micros: Mapped[int] = mapped_column(BigInteger)  # creator's (ticker, side)
    filled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    emitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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

from datetime import date  # noqa: E402

from sqlalchemy import CheckConstraint, Date, Index  # noqa: E402

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
