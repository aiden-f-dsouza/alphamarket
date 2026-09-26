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
    # p3-auth: set when the user follows the emailed link (account.py). An
    # unverified user can browse but cannot connect a venue.
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    mfa_pending: Mapped[bool] = mapped_column(Boolean, default=False)
    # Last TOTP (or recovery code) check on this session: the step-up window.
    step_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


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


class AuthToken(Base):
    """Single-use emailed token (email verification, password reset). Only the
    SHA-256 of the token is stored; `used_at` makes it single-use."""

    __tablename__ = "auth_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class RateLimitCounter(Base):
    """One fixed window of one inbound rate-limit key (ratelimit.py). Rows
    past `expires_at` are dead weight and pruned."""

    __tablename__ = "rate_limit_counters"

    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    window_start: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # unix seconds
    count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
