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
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class BalanceUnavailable(Exception):
    """The venue exposes no cash balance for this account (e.g. Polymarket
    proxy wallets). Callers fall back to the fills-only verification tier."""


class CredentialKind(StrEnum):
    RSA_PEM = "rsa_pem"  # key id + private key PEM (Kalshi)
    ADDRESS = "address"  # public identifier, no secret (Polymarket wallet)
    KEY_SECRET = "key_secret"  # key id + secret string (brokerage APIs)


class InstrumentKind(StrEnum):
    BINARY = "binary"  # contracts that settle at $1 or $0 (yes/no)
    ASSET = "asset"  # shares/coins valued at a mark, no settlement


class OwnershipProof(StrEnum):
    KEY = "key"  # holding a valid read-only key proves the account is yours
    PROFILE_CODE = "profile_code"  # put a Koine code in the venue profile bio


@dataclass(frozen=True)
class VenueProfile:
    """Everything core code needs to know about a venue without importing
    its module: how it is connected, what it trades, and the constants the
    paper mirror uses. One per venue, registered in registry.py."""

    venue: str
    display_name: str
    envs: tuple[str, ...]
    credential: CredentialKind
    key_label: str  # what the "key id" field means on the connect form
    secret_label: str | None  # None when the venue takes no secret
    key_help: str
    balance_observable: bool
    instrument: InstrumentKind
    tick_micros: int  # smallest price increment
    contract_step_micros: int  # smallest quantity the paper mirror trades
    fee_rate_bps: int  # paper fee: rate x count x p x (1-p) for binary venues
    fee_rounding_micros: int  # the venue reports fees to this precision
    ownership: OwnershipProof

    @property
    def needs_secret(self) -> bool:
        return self.credential is not CredentialKind.ADDRESS


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
    # Gross cash value of the fill before fees, when the venue reports one
    # that price x count cannot reproduce exactly (Polymarket's 10-dp average
    # prices). None means "price x count, exactly" (Kalshi).
    notional_micros: int | None = None


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
    def fetch_balance(self) -> NormalizedBalance:
        """Raises BalanceUnavailable on venues with no observable cash."""

    def verify_claim(self, code: str) -> bool | None:
        """For OwnershipProof.PROFILE_CODE venues: True if `code` is visible
        in the account's public profile, False if not, None if the venue
        could not be asked. Key-based venues never need this."""
        return None


# ---- market data (public, unauthenticated) ----------------------------------


class NormalizedMarket(BaseModel):
    """One market's current state as the venue reports it. Prices are per
    contract in micro-dollars; a bid or ask of 0 means "no quote on that
    side". `result` is empty until the market settles."""

    ticker: str
    status: str  # venue status string, e.g. "active" | "closed" | "finalized"
    yes_bid_micros: int
    yes_ask_micros: int
    last_price_micros: int
    result: str = ""  # "" | "yes" | "no" (| venue-specific)
    close_time: datetime | None = None
    as_of: datetime

    @property
    def settled(self) -> bool:
        return self.result in ("yes", "no")

    @property
    def yes_mark_micros(self) -> int | None:
        """Fair value of one YES contract for marking an open position:
        the settlement value once settled, the bid/ask midpoint while both
        sides are quoted, the last trade otherwise. None if nothing usable."""
        if self.result == "yes":
            return 1_000_000
        if self.result == "no":
            return 0
        if self.yes_bid_micros > 0 and self.yes_ask_micros > 0:
            return (self.yes_bid_micros + self.yes_ask_micros) // 2
        if self.last_price_micros > 0:
            return self.last_price_micros
        return None


class MarketDataSource(ABC):
    """Public market data for one venue. Needs no credentials, so one
    instance serves every account and every follower."""

    venue: str

    @abstractmethod
    def fetch_market(self, ticker: str) -> NormalizedMarket | None:
        """Current state of one market; None if the venue no longer knows it."""

    def fetch_markets(self, tickers: list[str]) -> dict[str, NormalizedMarket]:
        out: dict[str, NormalizedMarket] = {}
        for ticker in tickers:
            market = self.fetch_market(ticker)
            if market is not None:
                out[ticker] = market
        return out
