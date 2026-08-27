"""Venue-agnostic connector contract (the MVP seed of the PRD's MI-7 SDK).

Every venue adapter maps its native API into these normalized models.
Rules:
- Money is integer cents.
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
    count: int
    price_cents: int  # price paid per contract, in cents
    fee_cents: int = 0
    filled_at: datetime


class NormalizedSettlement(BaseModel):
    venue_settlement_id: str
    ticker: str
    market_result: str
    yes_count: int = 0
    no_count: int = 0
    revenue_cents: int  # gross payout credited at settlement
    fee_cents: int = 0
    settled_at: datetime


class NormalizedPosition(BaseModel):
    ticker: str
    position: int  # signed contract count (positive = yes/long)
    market_exposure_cents: int = 0


class NormalizedBalance(BaseModel):
    balance_cents: int
    as_of: datetime


class VenueConnector(ABC):
    """One instance per connected venue account."""

    venue: str

    @abstractmethod
    def check_connection(self) -> NormalizedBalance:
        """Cheapest authenticated call. Raises on bad credentials."""

    @abstractmethod
    def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        """All fills after `since` (or full available history), oldest first."""

    @abstractmethod
    def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedSettlement]: ...

    @abstractmethod
    def fetch_positions(self) -> list[NormalizedPosition]: ...

    @abstractmethod
    def fetch_balance(self) -> NormalizedBalance: ...
