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

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, LargeBinary, String, Text, event
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    description: Mapped[str] = mapped_column(Text, default="")  # unverified, labeled as such
    style_tags: Mapped[str] = mapped_column(String(255), default="")  # comma-separated
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


# ---- append-only enforcement (invariant #3) ---------------------------------

APPEND_ONLY_MODELS = (Fill, Settlement)


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
