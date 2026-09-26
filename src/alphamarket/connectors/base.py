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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class BalanceUnavailable(Exception):
    """The venue exposes no cash balance for this account (e.g. Polymarket
    proxy wallets). Callers fall back to the fills-only verification tier."""


class CredentialKind(StrEnum):
    RSA_PEM = "rsa_pem"  # key id + private key PEM (Kalshi)
    ADDRESS = "address"  # public identifier, no secret (Polymarket wallet)
    KEY_SECRET = "key_secret"  # key id + secret string (Kraken, Binance.US)
    # v2 (platform_plan 2.3). What `key_id` / `secret` mean per kind:
    KEY_SECRET_PASSPHRASE = "key_secret_passphrase"  # secret is JSON {"secret","passphrase"}
    OAUTH2 = "oauth2"  # key_id = venue account id; secret = access token (vault holds refresh)
    REPORT_TOKEN = "report_token"  # key_id = report/query id; secret = reporting token (IBKR Flex)
    AGGREGATOR = "aggregator"  # key_id = aggregator user id; secret = aggregator user secret


class InstrumentType(StrEnum):
    """Per-instrument type (v2). InstrumentKind above stays the venue-level
    shape the M6/M7 code branches on; this is what one ledger instrument is."""

    BINARY = "binary"  # settles at $1/$0
    SPOT = "spot"  # crypto spot pair, valued qty x close
    EQUITY = "equity"  # stock/ETF share, valued qty x close
    PERP = "perp"  # perpetual future: position valued qty x (mark - entry)
    OPTION = "option"  # out of scope this pass; rejected at ingest


class ReadOnlyProof(StrEnum):
    """How Koine knows a credential cannot trade or withdraw (platform_plan D4).
    Shown per venue on the methodology page."""

    SCOPE_ENDPOINT = "scope_endpoint"  # venue describes the key's permissions
    ORDER_PROBE = "order_probe"  # a validate-only order / withdraw query is refused
    TOKEN_TYPE = "token_type"  # the credential type is reporting-only by construction
    AGGREGATOR_FLAG = "aggregator_flag"  # the aggregator's read-only connection type
    NONE = "none"  # nothing to prove: no secret is held (public address)


class RateScope(StrEnum):
    CREDENTIAL = "credential"  # the venue counts per key/account
    IP = "ip"  # the venue counts per caller IP (shared by every account we poll)


@dataclass(frozen=True)
class RateBudget:
    """One token bucket a venue enforces. The outbound limiter (P2) keeps a
    bucket per (venue, credential) or per (venue, egress IP) and charges
    `cost(endpoint)` before every request the connector makes through
    `VenueConnector.throttle`. Integers only so budgets compare exactly.

    Example, Kraken Starter tier: capacity 15, refill 20/min (0.33/s decay),
    TradesHistory and Ledgers cost 2."""

    scope: RateScope
    capacity: int  # tokens in a full bucket
    refill_per_minute: int  # tokens restored per minute
    costs: tuple[tuple[str, int], ...] = ()  # (endpoint name, tokens); others cost default_cost
    default_cost: int = 1

    def cost(self, endpoint: str) -> int:
        return dict(self.costs).get(endpoint, self.default_cost)


class PaperFeeModel(StrEnum):
    """How the paper mirror charges a simulated fill (P6)."""

    BINARY_PXQ = "binary_pxq"  # rate x count x p x (1-p) (Kalshi, Polymarket)
    BPS_OF_NOTIONAL = "bps_of_notional"  # fee_rate_bps x notional (crypto taker rate)
    US_EQUITY_REGULATORY = "us_equity_regulatory"  # $0 commission + SEC fee + FINRA TAF on sells


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
    # ---- v2 fields (platform_plan 2.3); defaults keep M7 profiles valid ----
    readonly_proof: ReadOnlyProof = ReadOnlyProof.SCOPE_ENDPOINT
    transfers_observable: bool = False  # deposits/withdrawals arrive as events
    history_depth: str = "all"  # "all" | "90d" | "10k_fills" | ...
    quote_asset: str = "USD"  # the account's cash asset ("USD", "USDC")
    tos_doc: str | None = None  # "docs/venues/<venue>.md" (invariant 10)
    requires_partner_approval: bool = False  # connect refused in prod until True->False
    daily_resolution: bool = False  # EOD data only -> "daily_verified" tier
    rate_budgets: tuple[RateBudget, ...] = ()
    paper_fee_model: PaperFeeModel = PaperFeeModel.BINARY_PXQ

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


# ---- v2 normalized models: instruments, legs, events (platform_plan 2.1/2.3) ----
#
# Asset codes: spot/equity/cash use the canonical upper-case code ("BTC",
# "USDC", "USD", "AAPL"; map venue aliases like Kraken "XXBT"/"ZUSD").
# Perp positions are "<COIN>-PERP"; binary contracts are "<ticker>:<yes|no>".
# Ledger rows are per venue account, so codes need no venue prefix.
#
# Precision: a connector uses ONE fixed `decimals` per asset for the life of
# an account (the finest the venue ever reports for it), and parses every
# quantity with money.to_atoms(value_string, decimals). Never a float.


class LegKind(StrEnum):
    FILL_BASE = "fill_base"  # the instrument/asset bought (+) or sold (-)
    FILL_QUOTE = "fill_quote"  # the cash/quote asset paid (-) or received (+)
    FEE = "fee"  # always negative, in whatever asset the venue charged
    SETTLEMENT = "settlement"  # binary payout / contract removal
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER_IN = "transfer_in"  # between the user's own accounts/sub-accounts
    TRANSFER_OUT = "transfer_out"
    DIVIDEND = "dividend"
    INTEREST = "interest"  # incl. margin interest (negative)
    STAKING = "staking"
    FUNDING = "funding"  # perp funding payment (+/-)
    REALIZED_PNL = "realized_pnl"  # perp close: cash P&L credited to margin
    LIQUIDATION = "liquidation"
    SPLIT = "split"  # quantity change with no cash
    ADJUSTMENT = "adjustment"  # venue correction or admin classification (audited)


EXTERNAL_FLOW_KINDS = frozenset(
    {LegKind.DEPOSIT, LegKind.WITHDRAWAL, LegKind.TRANSFER_IN, LegKind.TRANSFER_OUT}
)
"""Legs that move capital in or out rather than earn a return; the
flow-adjusted TWR chains across them (methodology "External flows")."""


class NormalizedLeg(BaseModel):
    asset: str
    delta_atoms: int  # signed change in the account's holding of `asset`
    decimals: int = Field(ge=0, le=12)
    kind: LegKind
    instrument_symbol: str | None = None  # venue symbol the leg belongs to, if any


class NormalizedInstrument(BaseModel):
    symbol: str  # exactly as the venue reports it ("XXBTZUSD", "BTC-USD", "AAPL", "BTC")
    type: InstrumentType
    base_asset: str  # canonical code of what the quantity counts ("BTC", "AAPL", "BTC-PERP")
    quote_asset: str  # canonical code prices are in ("USD", "USDC", "BTC")
    qty_decimals: int = Field(ge=0, le=12)
    qty_step_atoms: int = 1  # smallest tradable increment (lot size), in atoms
    min_qty_atoms: int = 0
    min_notional: str | None = None  # decimal string in quote asset, if the venue has one
    multiplier: int = 1  # options: 100
    expiry: datetime | None = None


class NormalizedAssetFill(BaseModel):
    """One execution on a non-binary venue. `legs` carry the ledger truth;
    the other fields are what signals and paper copying read.

    Spot/equity buy: FILL_BASE +qty base, FILL_QUOTE -(qty x price) quote,
    FEE -fee in the fee asset. Perp: FILL_BASE +/-qty of "<COIN>-PERP",
    REALIZED_PNL on a reducing fill, FEE; no FILL_QUOTE (margin, not spot)."""

    venue_fill_id: str  # stable + unique per account (idempotency)
    order_id: str | None = None
    instrument: NormalizedInstrument
    action: Action
    qty_atoms: int = Field(gt=0)  # unsigned, at instrument.qty_decimals
    price: str  # decimal string, quote asset per one base unit, as reported
    legs: list[NormalizedLeg]
    filled_at: datetime
    liquidity: str | None = None  # "maker" | "taker" | None


class NormalizedEvent(BaseModel):
    """Any non-fill ledger activity: deposits, withdrawals, dividends,
    interest, staking, funding, splits, adjustments."""

    venue_ref: str  # stable + unique per account and kind
    kind: LegKind  # the event's primary kind
    legs: list[NormalizedLeg]
    occurred_at: datetime
    description: str | None = None
    # True when venue_ref is a content hash because the venue's ids are not
    # stable (SnapTrade reprocessing); dedupe then relies on the hash.
    ref_is_content_hash: bool = False


class AssetAmount(BaseModel):
    asset: str
    atoms: int
    decimals: int = Field(ge=0, le=12)


class NormalizedBalances(BaseModel):
    """The venue's own view of the account as a vector. Reconciliation v2
    compares it per asset with the replayed ledger. Perp positions appear as
    "<COIN>-PERP" entries next to the margin asset."""

    as_of: datetime
    assets: list[AssetAmount]
    # Broker-reported total account value in USD micros (Alpaca `equity`,
    # IBKR NAV), when the venue computes one; reconciled with a tolerance.
    equity_usd_micros: int | None = None


class ReadOnlyEvidence(BaseModel):
    """Result of VenueConnector.prove_read_only(). Stored on the account and
    re-checked nightly; read_only=False means reject at connect, suspend later."""

    method: ReadOnlyProof
    read_only: bool | None  # True confirmed, False writable, None undeterminable
    checked_at: datetime
    details: dict[str, str | bool | int | None] = {}  # raw flags; never secrets


@dataclass
class Page[T]:
    """One page of a resumable backfill. `next_cursor` None means done. The
    cursor is opaque venue state (Kraken ofs, Binance symbol+fromId+window,
    a Coinbase cursor) that the job queue persists between runs."""

    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None


class RateLimited(Exception):
    """The venue answered 429/418. The job re-schedules after `retry_after`."""

    def __init__(self, retry_after_seconds: float, endpoint: str = "") -> None:
        super().__init__(f"rate limited on {endpoint or 'venue'}; retry in {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds
        self.endpoint = endpoint


class BudgetExhausted(Exception):
    """Raised by the injected throttle when Koine's own budget for this venue
    is spent; nothing was sent. Same handling as RateLimited."""

    def __init__(self, retry_after_seconds: float, endpoint: str = "") -> None:
        super().__init__(
            f"budget exhausted for {endpoint or 'venue'}; retry in {retry_after_seconds}s"
        )
        self.retry_after_seconds = retry_after_seconds
        self.endpoint = endpoint


class CredentialExpired(Exception):
    """OAuth access token expired or revoked; the vault must refresh it (or the
    user must reconnect) before the next call."""


Throttle = Callable[[str], None]
"""Called with the endpoint name before every venue request. Raises
BudgetExhausted to stop the request. Default: no-op (tests, scripts)."""


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

    # ---- v2 (platform_plan 2.3): additive, with defaults for M7 venues ----

    _throttle: Throttle | None = None

    def set_throttle(self, throttle: Throttle | None) -> None:
        self._throttle = throttle

    def throttle(self, endpoint: str) -> None:
        """Connectors call this before EVERY HTTP request, with an endpoint
        name that matches the keys in their profile's RateBudget.costs."""
        if self._throttle is not None:
            self._throttle(endpoint)

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        """One page of asset fills, oldest first within the page. Binary venues
        return nothing (their fills go through fetch_fills)."""
        return Page()

    def fetch_asset_fills(self, since: datetime | None = None) -> list[NormalizedAssetFill]:
        out: list[NormalizedAssetFill] = []
        cursor: str | None = None
        while True:
            page = self.fetch_asset_fills_page(cursor, since)
            out.extend(page.items)
            if page.next_cursor is None:
                return out
            cursor = page.next_cursor

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        """One page of non-fill ledger events (transfers, dividends, funding...).
        Venues without observable transfers return nothing."""
        return Page()

    def fetch_events(self, since: datetime | None = None) -> list[NormalizedEvent]:
        out: list[NormalizedEvent] = []
        cursor: str | None = None
        while True:
            page = self.fetch_events_page(cursor, since)
            out.extend(page.items)
            if page.next_cursor is None:
                return out
            cursor = page.next_cursor

    def fetch_balances(self) -> NormalizedBalances:
        """Balance vector. Default wraps the single USD balance (6 dp micros)."""
        bal = self.fetch_balance()
        return NormalizedBalances(
            as_of=bal.as_of,
            assets=[AssetAmount(asset="USD", atoms=bal.balance_micros, decimals=6)],
        )

    def prove_read_only(self) -> ReadOnlyEvidence:
        """Default derives from check_write_scope(); venues override with
        their own method and details."""
        writable = self.check_write_scope()
        return ReadOnlyEvidence(
            method=ReadOnlyProof.SCOPE_ENDPOINT if writable is not None else ReadOnlyProof.NONE,
            read_only=None if writable is None else not writable,
            checked_at=datetime.now(UTC),
        )


class AssetVenueConnector(VenueConnector):
    """Base for non-binary venues (crypto, equities). Implements the binary
    M7 methods as empty so core code can treat every connector alike;
    subclasses implement the v2 methods plus check_connection,
    check_write_scope/prove_read_only and fetch_balances."""

    def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        return []

    def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedSettlement]:
        return []

    def fetch_positions(self) -> list[NormalizedPosition]:
        return []

    def fetch_balance(self) -> NormalizedBalance:
        """USD-equivalent cash for M7 callers: the quote asset's amount in
        micros. Raises BalanceUnavailable if the account has none."""
        balances = self.fetch_balances()
        if balances.equity_usd_micros is not None:
            return NormalizedBalance(
                balance_micros=balances.equity_usd_micros, as_of=balances.as_of
            )
        for amount in balances.assets:
            if amount.asset in ("USD", "USDC") and amount.decimals <= 6:
                micros = amount.atoms * 10 ** (6 - amount.decimals)
                return NormalizedBalance(balance_micros=micros, as_of=balances.as_of)
        raise BalanceUnavailable(f"{self.venue}: no USD/USDC cash asset in balances")

    @abstractmethod
    def fetch_balances(self) -> NormalizedBalances: ...

    def check_connection(self) -> NormalizedBalance:
        return self.fetch_balance()


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

    # ---- v2: asset market data (defaults: nothing available) ----

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        """Every instrument the venue lists (symbols, decimals, lot sizes)."""
        return []

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list["DailyClose"]:
        """UTC daily closes for [start, end], oldest first. Fills prices_daily."""
        return []

    def fetch_quote(self, symbol: str) -> "Quote | None":
        """Current top of book; the paper mirror fills at it (plan E5)."""
        return None

    def fetch_funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> list["FundingRate"]:
        """Perp funding rates in [start, end]; paper perps accrue them (P6)."""
        return []

    def fetch_corporate_actions(self, start: date, end: date) -> list["CorporateAction"]:
        """Splits, dividends and symbol changes in [start, end] (equities)."""
        return []


class AssetMarketData(MarketDataSource):
    """Base for non-binary market data: there are no binary markets to fetch."""

    def fetch_market(self, ticker: str) -> NormalizedMarket | None:
        return None


class DailyClose(BaseModel):
    symbol: str
    day: date
    close: str  # decimal string, quote asset per one base unit


class Quote(BaseModel):
    symbol: str
    bid: str | None = None  # decimal strings, quote asset per unit
    ask: str | None = None
    last: str | None = None
    as_of: datetime


class FundingRate(BaseModel):
    symbol: str
    at: datetime
    rate: str  # decimal string per funding interval, e.g. "0.0000125"
    mark_price: str | None = None


class CorporateActionKind(StrEnum):
    SPLIT = "split"  # also reverse splits (ratio < 1)
    CASH_DIVIDEND = "cash_dividend"
    SYMBOL_CHANGE = "symbol_change"


class CorporateAction(BaseModel):
    symbol: str
    kind: CorporateActionKind
    ex_date: date
    pay_date: date | None = None
    ratio_new: int | None = None  # split: new shares per ratio_old old shares
    ratio_old: int | None = None
    cash_per_share: str | None = None  # dividend, decimal string USD
    new_symbol: str | None = None  # symbol change
