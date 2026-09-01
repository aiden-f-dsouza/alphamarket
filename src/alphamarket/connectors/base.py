"""Venue-agnostic connector contract (the MVP seed of the PRD's MI-7 SDK).

Every venue adapter maps its native API into these normalized models.
Rules:
- Money is integer MICRO-DOLLARS (1e-6 USD). See alphamarket.money for why
  the scale is 1e6 and not cents. Floats never touch a value.
- Contract quantities carry the same 1e6 scale, because venue quantities are
  not necessarily whole contracts (Kalshi's `count_fp` is fractional).
- Timestamps are timezone-aware UTC.
- `venue_fill_id` / `venue_settlement_id` must be stable and unique per venue
  account; ingestion idempotency depends on them.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class Side(StrEnum):
    YES = "yes"
    NO = "no"
    LONG = "long"  # for non-binary venues later
    SHORT = "short"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"


class NormalizedFill(BaseModel):
    venue_fill_id: str
    ticker: str
    side: Side
    action: Action
    count_micros: int  # micro-contracts (1e6 = one contract)
    price_micros: int  # price per contract, micro-dollars
    fee_micros: int = 0
    filled_at: datetime


class NormalizedSettlement(BaseModel):
    venue_settlement_id: str
    ticker: str
    market_result: str
    yes_count_micros: int = 0
    no_count_micros: int = 0
    revenue_micros: int  # gross payout credited at settlement
    fee_micros: int = 0
    settled_at: datetime


class NormalizedPosition(BaseModel):
    ticker: str
    position_micros: int  # signed micro-contracts (positive = yes/long)
    market_exposure_micros: int = 0


class NormalizedBalance(BaseModel):
    balance_micros: int
    as_of: datetime


class VenueConnector(ABC):
    """One instance per connected venue account."""

    venue: str

    @abstractmethod
    def check_connection(self) -> NormalizedBalance:
        """Cheapest authenticated call. Raises on bad credentials."""

    @abstractmethod
    def check_write_scope(self) -> bool | None:
        """True if the key can write (must be rejected at connect time),
        False if confirmed read-only, None if undeterminable. Implementations
        must only ever probe with calls that cannot mutate account state."""

    @abstractmethod
    def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        """All fills after `since` (or full available history), oldest first."""

    @abstractmethod
    def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedSettlement]: ...

    @abstractmethod
    def fetch_positions(self) -> list[NormalizedPosition]: ...

    @abstractmethod
    def fetch_balance(self) -> NormalizedBalance: ...
