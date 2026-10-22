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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field, model_validator

from alphamarket.connectors.addresses import AddressKind


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
    TradesHistory and Ledgers cost 2.

    Two hosts, two limits (an API and an explorer, say): declare one budget
    per host, and in each give the OTHER host's endpoints cost 0. A cost of 0
    (or less) neither waits nor spends a token (VenueLimiter.acquire skips
    it; pinned by tests/test_venue_limits.py), so each request is charged
    only to its own host's bucket."""

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


class StreamMode(StrEnum):
    """How a venue's private stream feeds the ledger (P7, stream.py). The
    poller stays the source of truth either way: a stream only shortens copy
    lag, and a dropped stream changes nothing but that lag."""

    # Frames carry fills normalized EXACTLY as the polled path normalizes
    # them (same venue ids, same legs), so they are ingested directly through
    # ingest.ingest_asset_fills; a fill seen by both is stored once. Only for
    # venues whose stream payload is the REST payload (Hyperliquid userFills).
    FILLS = "fills"
    # Frames only say "this account just traded"; the account's sync job is
    # queued at once and the REST path ingests the fill. For venues whose
    # stream shape differs from the polled one (Kraken legs come from Ledgers
    # rows with running balances, which no stream carries): ingesting a
    # differently-shaped copy would quarantine the account on the poller's
    # exact duplicate check.
    NUDGE = "nudge"


class InstrumentKind(StrEnum):
    BINARY = "binary"  # contracts that settle at $1 or $0 (yes/no)
    ASSET = "asset"  # shares/coins valued at a mark, no settlement


class OwnershipProof(StrEnum):
    KEY = "key"  # holding a valid read-only key proves the account is yours
    PROFILE_CODE = "profile_code"  # put a Koine code in the venue profile bio
    # wave2: sign a Koine message (the claim code is the nonce) with the
    # account's wallet; see VenueConnector.claim_text / claim_payload.
    SIGNED_MESSAGE = "signed_message"


@dataclass(frozen=True)
class RegulatoryFees:
    """US equity sell-side regulatory fees the paper mirror charges under
    PaperFeeModel.US_EQUITY_REGULATORY (decimal strings, USD).

    SEC Section 31 fee: notional x sec_fee_per_million / 1e6 on sells.
    FINRA TAF: shares x taf_per_share on sells, capped at taf_max.
    `effective` is the date the values were checked against the sources
    (docs/methodology.md "Paper copying v2"); rates change by SEC order."""

    sec_fee_per_million: str
    taf_per_share: str
    taf_max: str
    effective: str


class ReconcileMode(StrEnum):
    """What nightly reconciliation compares for an asset venue (S2)."""

    # The whole balance vector (cash and holdings) and every venue running
    # balance: today's rule, the "reconciled" tier.
    BALANCES = "balances"
    # Only the signed perp position per instrument, within
    # VenueProfile.position_tolerance_atoms; cash is never read. For venues
    # with no venue-side balance (GMX wallet mode): the cash is reconstructed
    # by the connector, so the tier is "positions_verified".
    POSITIONS = "positions"


@dataclass(frozen=True)
class MarginTier:
    """One perp maintenance-margin tier: positions with notional >=
    lower_bound (USD, decimal string) may use at most max_leverage.
    Maintenance margin rate = 1 / (2 x max_leverage) [VERIFY per venue]."""

    lower_bound: str
    max_leverage: int


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
    # ---- wave2 fields ----
    # Venue whose public market data prices this venue's assets when it has
    # none of its own (IBKR, SnapTrade -> "alpaca"). None = this venue.
    pricing_venue: str | None = None
    # Paper fill when no quote at observed_at exists: creator price moved
    # against the follower by this many bps (methodology "Paper copying v2").
    paper_fallback_penalty_bps: int = 50
    # A signal observed later than this after its fill is not copied
    # ("stale_signal"): a follower could not have traded near that price.
    max_copy_lag_seconds: int = 6 * 3600
    regulatory_fees: RegulatoryFees | None = None  # US_EQUITY_REGULATORY only
    asset_classes: tuple[str, ...] = ()  # "prediction" | "crypto" | "equities"
    # ---- copy strategies step 7 (discover.py) ----
    # LeaderboardPeriod values the venue's public leaderboard offers; () = none.
    # MarketDataSource.fetch_leaderboards serves them.
    leaderboard_periods: tuple[str, ...] = ()
    # The /watch/discover columns this venue's snapshot rows can fill, in
    # display order (discover.COLUMN_LABELS names them). The default is what a
    # full perp-DEX board has; a venue lists only what it really publishes.
    leaderboard_columns: tuple[str, ...] = ("pnl", "roi", "volume", "account_value")
    # One line under the period tabs: how to read this venue's figures. "" = none.
    leaderboard_note: str = ""
    # ---- P7 streaming (stream.py) ----
    # None: the venue is polled only. Otherwise how its private stream feeds
    # the ledger (StreamMode); the connector's stream_source() builds it.
    stream_mode: StreamMode | None = None
    # Venue cap on distinct accounts streamed from one egress IP (Hyperliquid:
    # "at most 10 unique users" across websocket user subscriptions per IP).
    # None: no documented cap.
    stream_users_per_ip: int | None = None
    # The venue's instruments are perpetuals: hourly funding and liquidation
    # apply to its paper tails (tail_terms says so).
    perps: bool = False
    # ---- on-chain perps scaffold (docs/onchain_perps_plan.md S1/S2) ----
    # How an ADDRESS venue's identifier is validated and stored
    # (connectors/addresses.py). Read only when credential is ADDRESS.
    address_kind: AddressKind = AddressKind.EVM
    # BALANCES: reconcile the whole vector (needs balance_observable).
    # POSITIONS: reconcile only the perp position vector; with
    # balance_observable=False the account is still reconciled nightly and
    # its tier is "positions_verified".
    reconcile_mode: ReconcileMode = ReconcileMode.BALANCES
    # POSITIONS mode: allowed |venue - ledger| per perp, in atoms at the
    # compared scale (the venue's documented quantity rounding).
    position_tolerance_atoms: int = 0

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
# Perp positions are "<COIN>-PERP". Binary contracts depend on the venue:
#   - netting venues (Kalshi): ONE asset per market, "<ticker>", signed like
#     NormalizedPosition.position_micros (+yes / -no). Buy YES = +count, sell
#     YES = -count, buy NO = -count, sell NO = +count; settlement leg =
#     -(yes_count - no_count). The replayed vector then equals the venue's.
#   - tokenised venues (Polymarket): a wallet can hold both outcome tokens, so
#     "<conditionId>:yes" and "<conditionId>:no", unsigned; REDEEM burns each.
# Ledger rows are per venue account, so codes need no venue prefix.
#
# Precision: a connector should use one `decimals` per asset (the finest the
# venue reports for it) and parses every quantity with
# money.to_atoms(value_string, decimals), never a float. The LEDGER must not
# rely on that: venues change precision, and one venue can report the same
# asset at different scales (Kraken fills 8 dp, Ledgers 10 dp, ZUSD 4 dp).
# Consumers never sum raw atoms across rows; they rescale each value to the
# max decimals seen for that (account, asset) first (scaling up is exact).
# NormalizedAssetFill.qty_atoms (instrument.qty_decimals) and its FILL_BASE
# leg (asset decimals) can legitimately differ in scale for the same reason.
# Suggested scales: crypto as the venue reports (<= 10), equities 9 (Alpaca
# fractional shares), cash <= 8.
#
# Identity: venue_fill_id / venue_ref are unique PER VENUE ACCOUNT. Prefix
# ids that are only unique per symbol (Binance myTrades.id -> f"{symbol}:{id}"),
# and include the instrument when a venue emits one row per instrument at the
# same timestamp (Hyperliquid funding -> f"{time}:{coin}").
#
# Timestamps: date-only venue rows (IBKR Flex cash transactions, SnapTrade
# daily activities) use 23:59:59 UTC of the reported date, so every consumer
# assigns them to the same day.
#
# Zero-delta legs (a losing settlement's $0 revenue) are allowed here; the
# ledger drops them on write.


class LegKind(StrEnum):
    FILL_BASE = "fill_base"  # the instrument/asset bought (+) or sold (-)
    FILL_QUOTE = "fill_quote"  # the cash/quote asset paid (-) or received (+)
    FEE = "fee"  # always negative, in whatever asset the venue charged
    SETTLEMENT = "settlement"  # binary payout / contract removal
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    # TRANSFER_* only when value leaves or enters THIS venue account (e.g. to a
    # sub-account); same-account conversions are CONVERT legs of one event.
    TRANSFER_IN = "transfer_in"
    TRANSFER_OUT = "transfer_out"
    DIVIDEND = "dividend"
    INTEREST = "interest"  # incl. margin interest (negative)
    STAKING = "staking"
    FUNDING = "funding"  # perp funding payment (+/-); instrument_symbol = the coin
    TAX = "tax"  # withholding (Alpaca DIVNRA, IBKR); not a trading cost, never counted as fee
    CONVERT = "convert"  # same-account conversion (Binance convert/dust, staking wrappers)
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
    # Venue-reported running balance of `asset` right after this leg, at the
    # same decimals (Kraken Ledgers `balance`). Reconciliation checks it
    # exactly, in ingestion order, on legs where it is not None.
    balance_after_atoms: int | None = None


def _no_duplicate_legs(legs: list[NormalizedLeg]) -> list[NormalizedLeg]:
    """The ledger's identity is (account, venue_ref, kind, asset): two legs of
    one fill/event with the same (kind, asset) would collide and one would be
    lost. Connectors must merge (sum) them before returning."""
    seen: set[tuple[str, str]] = set()
    for leg in legs:
        key = (leg.kind, leg.asset)
        if key in seen:
            raise ValueError(f"duplicate leg {key}: merge legs with the same kind and asset")
        seen.add(key)
    return legs


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
    # False: paper copying skips the instrument (skip reason "not_copyable")
    # while its signals are still recorded. For synthetic US equities/ETFs,
    # pre-IPO names, FX and commodities on crypto perp venues until counsel
    # answers (onchain_perps_plan Q3). A connector sets it the same way on
    # fills and in fetch_instruments: the stored row follows the latest one.
    copyable: bool = True


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

    @model_validator(mode="after")
    def _legs_unique(self) -> "NormalizedAssetFill":
        _no_duplicate_legs(self.legs)
        return self


class NormalizedEvent(BaseModel):
    """Any non-fill ledger activity: deposits, withdrawals, dividends,
    interest, staking, funding, splits, adjustments."""

    venue_ref: str  # stable + unique per account and kind
    kind: LegKind  # the event's primary kind
    legs: list[NormalizedLeg]
    occurred_at: datetime
    description: str | None = None
    # True when venue_ref is a content hash because the venue's ids are not
    # stable (SnapTrade reprocessing); dedupe then relies on the hash. Two
    # identical same-day activities collapse into one: acceptable only on the
    # daily_verified tier, and stated there.
    ref_is_content_hash: bool = False

    @model_validator(mode="after")
    def _legs_unique(self) -> "NormalizedEvent":
        _no_duplicate_legs(self.legs)
        return self


class AssetAmount(BaseModel):
    asset: str
    atoms: int
    decimals: int = Field(ge=0, le=12)
    # Quote asset per base unit, as reported. Needed to value positions the
    # ledger never saw open (anchors; Hyperliquid serves only 10k fills):
    # a perp is worth qty x (mark - entry). Alpaca avg_entry_price, IBKR
    # costBasisPrice and Hyperliquid entryPx all land in entry_price.
    entry_price: str | None = None
    mark_price: str | None = None


class NormalizedBalances(BaseModel):
    """The venue's own view of the account as a vector. Reconciliation v2
    compares it per asset with the replayed ledger. Perp positions appear as
    "<COIN>-PERP" entries (with entry_price) next to the margin asset, and the
    margin asset is cash EXCLUDING unrealized P&L (Hyperliquid: accountValue
    minus the sum of unrealizedPnl), with equity_usd_micros = accountValue."""

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
    """One page of a resumable backfill.

    Rules for every *_page method:
    - one venue HTTP request per page, so BudgetExhausted/RateLimited never
      lose fetched work (the job persists `next_cursor` and retries);
    - items oldest-first within the page (reverse venues that return newest
      first, like Kraken);
    - when `cursor` is not None, `since` is ignored;
    - a backfill pins its upper bound (e.g. Kraken `end`) in the first cursor,
      because offset pagination shifts as new rows arrive.

    `next_cursor` None means this pass is done. `resume_cursor`, set only on
    that final page, is opaque state the job stores on the account and passes
    back as `cursor` on the NEXT incremental sync (Binance: discovered symbol
    set + per-symbol fromId; Kraken: last seen time/refid). None means "use
    `since`"."""

    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None
    resume_cursor: str | None = None


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


class Throttle(Protocol):
    """The outbound limiter (P2) as connectors see it. Default: none (tests,
    scripts)."""

    def before(self, endpoint: str) -> None:
        """Charge the endpoint's cost; raise BudgetExhausted to stop the request."""

    def after(self, endpoint: str, extra_cost: int = 0, used: int | None = None) -> None:
        """Charge response-dependent cost (Hyperliquid +1 per 20 items) and/or
        sync to the venue's reported usage (Binance X-MBX-USED-WEIGHT-1M)."""


class _Throttled:
    """Mixin: the throttle hook shared by connectors and market data sources.
    Call throttle() before EVERY HTTP request, with an endpoint name matching
    the profile's RateBudget.costs keys, and throttle_after() once the
    response is in when cost depends on it."""

    _throttle: Throttle | None = None

    def set_throttle(self, throttle: Throttle | None) -> None:
        self._throttle = throttle

    def throttle(self, endpoint: str) -> None:
        if self._throttle is not None:
            self._throttle.before(endpoint)

    def throttle_after(self, endpoint: str, extra_cost: int = 0, used: int | None = None) -> None:
        if self._throttle is not None:
            self._throttle.after(endpoint, extra_cost, used)


# ---- P7 streaming (optional capability) --------------------------------------
#
# A venue that can push its own fills implements VenueConnector.stream_source()
# and sets VenueProfile.stream_mode. The source is protocol logic only: it
# names the websocket URL, builds the subscribe frames and parses incoming
# frames into StreamEvents. It never opens a socket; the core (stream.py)
# owns the connection, pings, timeouts, reconnects, leases and ingestion, so
# every frame parser is testable from fixture text without a network.
#
# Rules for implementations:
# - prepare() may make REST calls (a websocket token); they go through the
#   connector's throttle like any other request.
# - subscribe_frames() may contain a token or JWT: callers never log them.
# - parse() is synchronous and may call the connector's cached metadata
#   (Hyperliquid spotMeta); BudgetExhausted/RateLimited from it make the core
#   fall back to a nudge, never drop the activity silently.
# - Venue ids of streamed fills MUST equal the polled ids, and in
#   StreamMode.FILLS the legs must be identical too (ingest quarantines a
#   differing duplicate).


class StreamUnavailable(Exception):  # noqa: N818  (a state, like BalanceUnavailable)
    """This account cannot stream (the key lacks the websocket permission,
    the venue refused the subscription). The account stays polled; the
    stream process does not retry it until a cool-off has passed."""


class StreamProtocolError(Exception):
    """The venue sent an error frame or something unparseable; the stream
    reconnects with backoff."""


@dataclass
class StreamEvent:
    """What one frame means. `fills` (StreamMode.FILLS only) go through the
    idempotent ingest path; `nudge` asks for the account's sync job now.
    `ref` is a venue id for logs (never a secret)."""

    fills: list["NormalizedAssetFill"] = field(default_factory=list)
    nudge: bool = False
    ref: str | None = None


class StreamingSource(ABC):
    """One account's private stream, as protocol logic (see the rules above)."""

    venue: str
    # Application-level keepalive: the core sends ping_frame() this often.
    ping_interval_seconds: float = 30.0
    # No frame at all for this long means a dead connection: reconnect.
    idle_timeout_seconds: float = 90.0

    def prepare(self) -> None:
        """Called before every (re)connect: fetch a fresh token, etc."""
        return None

    @abstractmethod
    def url(self) -> str: ...

    @abstractmethod
    def subscribe_frames(self) -> list[str]:
        """Text frames sent right after connecting (may hold a token)."""

    def ping_frame(self) -> str | None:
        """An application ping, or None when the venue needs none."""
        return None

    @abstractmethod
    def parse(self, frame: str | bytes) -> list[StreamEvent]:
        """Events in one frame (heartbeats and acks: none). Raises
        StreamProtocolError on an error frame, StreamUnavailable when the
        venue says this account may not subscribe."""


class VenueConnector(_Throttled, ABC):
    """One instance per connected venue account."""

    venue: str
    # Which venue account this instance reads when one credential spans several
    # (SnapTrade user -> many brokerage accounts, IBKR Flex query -> several
    # accountIds, Coinbase portfolio uuid). None when key and account are 1:1.
    # Also how a second connect of the same venue account is detected.
    account_ref: str | None = None

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
        could not be asked. Key-based venues never need this.
        SIGNED_MESSAGE venues receive claim_payload(code, signature)."""
        return None

    # ---- wave2: ownership by signed message, unsupported activity ----

    def claim_text(self, code: str) -> str | None:
        """SIGNED_MESSAGE venues: the exact text the user signs for claim
        code `code`. None for venues that do not claim by signature."""
        return None

    def claim_payload(self, code: str, signature: str) -> str:
        """What verify_claim receives for a signature over claim_text(code).
        The caller passes the account's STORED claim code, never one the user
        submitted (replay protection). Default: the bare code."""
        return code

    def drain_unsupported(self) -> list["UnsupportedActivity"]:
        """Activities skipped by page methods since the last call, oldest
        first, then forgotten. The page runner persists them after every
        page (venue_activity_flags). Default: the venue skips nothing."""
        return []

    def stream_source(self) -> StreamingSource | None:
        """P7: this account's private stream (VenueProfile.stream_mode says
        how it feeds the ledger). None: the venue, or this credential, is
        polled only. Core code reaches it only through a connector built by
        the registry."""
        return None

    # ---- v2 (platform_plan 2.3): additive, with defaults for M7 venues ----

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


class MarketDataSource(_Throttled, ABC):
    """Public market data for one venue. Needs no credentials, so one
    instance serves every account and every follower. Public endpoints count
    against the venue's IP bucket, so they call throttle() like connectors."""

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

    PRICING_QUOTES: tuple[str, ...] = ("USD", "USDC", "USDT")
    STABLECOINS_AT_PAR: frozenset[str] = frozenset({"USD", "USDC", "USDT"})

    def pricing_symbol(
        self, asset: str, instruments: list[NormalizedInstrument] | None = None
    ) -> str | None:
        """The venue symbol whose daily close values one unit of `asset` in
        USD terms: the first spot/equity instrument with base_asset == asset
        and quote in PRICING_QUOTES order. None for cash/stablecoins (valued
        at 1.00, see STABLECOINS_AT_PAR) and for assets the venue cannot
        price (fall back to CoinGecko). One rule so every consumer values the
        same way."""
        if asset in self.STABLECOINS_AT_PAR:
            return None
        listed = instruments if instruments is not None else self.fetch_instruments()
        for quote in self.PRICING_QUOTES:
            for inst in listed:
                if (
                    inst.base_asset == asset
                    and inst.quote_asset == quote
                    and inst.type in (InstrumentType.SPOT, InstrumentType.EQUITY)
                ):
                    return inst.symbol
        return None

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

    def fetch_margin_tiers(self, symbol: str) -> list[MarginTier]:
        """Perp maintenance-margin tiers, ascending by lower_bound (wave2)."""
        return []

    # ---- copy strategies step 7: public leaderboards (discover.py) ----

    def fetch_leaderboards(
        self, periods: "tuple[LeaderboardPeriod, ...]"
    ) -> "dict[LeaderboardPeriod, list[LeaderboardEntry]]":
        """The venue's public leaderboard for each period, best first. One
        call may serve every period (Hyperliquid publishes all windows in one
        document); a venue without a leaderboard returns {}."""
        return {}

    # ---- on-chain perps scaffold: the venue's own privacy flag (S2) ----
    #
    # Some venues let an address owner hide themselves (Pacifica
    # `privacy_mode`). The flag is a leaderboard attribute, venue-wide, so it
    # lives here rather than on VenueConnector. Both hooks default to None:
    # "this venue offers no such signal", and cost nothing.

    def private_addresses(self) -> frozenset[str] | None:
        """The normalized addresses the LAST fetch_leaderboards call on this
        instance saw with the venue's privacy flag on (a connector collects
        them while dropping those rows; no extra request). None before a
        fetch, or when the venue has no flag. discover.refresh sweeps watches
        of these addresses once per refresh (watchlist.remove_private_watch).
        Each address MUST already be passed through
        `addresses.normalize(profile.address_kind, raw)`, never the raw board
        string: discover.sweep_private matches them against stored key_ids by
        exact equality, so an un-normalized EVM address would never match."""
        return None

    def owner_private(self, address: str) -> bool | None:
        """A one-off check of one address, already normalized by
        `addresses.normalize` (compare the board's rows only after normalizing
        them the same way), used only when a new watch is created
        (watchlist._track), never per sync. True: treat the address as opted
        out while the flag is on. False: the flag is off. None: unknown (no
        flag on this venue, or the address is absent from what the venue
        publishes)."""
        return None

    # Set by leaderboard_details when the venue said 429 or the budget ran
    # out part way: the caller tells its limiter, and the rows it got stand.
    enrichment_rate_limited: "RateLimited | None" = None

    def leaderboard_details(
        self,
        period: "LeaderboardPeriod",
        addresses: list[str],
        now: datetime | None = None,
    ) -> "dict[str, LeaderboardDetails]":
        """Optional extra figures for the KEPT leaderboard rows of one period
        (after opt-outs and the top-N cut), keyed by the normalized address.
        Default: none, so a venue whose board is complete needs nothing.
        Must never raise for one address's failure: a missing key or a None
        field means unknown, and the snapshot is stored regardless."""
        return {}


class LeaderboardPeriod(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    ALL = "all"


class LeaderboardEntry(BaseModel):
    """One row of a venue's public leaderboard: an address and the venue's own
    figures for the period. Deliberately no display name, username, avatar or
    any other identity field (copy_strategies_plan: never link an address to
    an identity); a connector drops them before building this."""

    # Normalized for the venue's `address_kind` (EVM lowercased, Solana as
    # given): connectors normalize before building the entry
    # (connectors.addresses.normalize).
    address: str
    rank: int  # the venue's 1-based rank in its own ordering for the period
    pnl: str  # decimal string in the venue's quote asset (USD/USDC)
    roi: str | None = None  # decimal fraction: "0.1234" = 12.34%
    volume: str | None = None  # decimal string, quote asset
    account_value: str | None = None  # decimal string, quote asset, at fetch time


class LeaderboardDetails(BaseModel):
    """Extra per-address figures a venue's public wallet stats give beyond its
    leaderboard row. Money fields are decimal strings (leaderboard_figure
    rounded); like LeaderboardEntry there is deliberately no identity field."""

    volume: str | None = None  # period volume in the quote asset (USD)
    positions_value: str | None = None  # open positions marked to market
    markets_traded: int | None = None  # distinct markets traded, lifetime
    biggest_win: str | None = None  # best single resolved win, lifetime
    active_since: int | None = None  # epoch seconds (UTC) of the venue's join date


LEADERBOARD_QUANTUM = Decimal("0.000001")


def leaderboard_figure(value: str | Decimal) -> str:
    """A venue's leaderboard money figure (pnl, volume, account value) as a
    plain decimal string of at most 6 dp, rounded half-even.

    These are DISPLAY figures the venue computed and Koine labels as the
    venue's own, never ledger money: Hyperliquid prints up to 10 dp and
    Polymarket sends floats, and money.to_micros rightly refuses anything
    finer than 1e-6. So connectors round here, once, explicitly, and
    discover.snapshot_row stores the result as integer micros (invariant 1).
    A ledger value must never come through this function."""
    rounded = Decimal(value).quantize(LEADERBOARD_QUANTUM, rounding=ROUND_HALF_EVEN)
    return format(rounded, "f")


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
    # Decimal string per funding interval, e.g. "0.0000125". Positive means
    # longs pay. Shorts receive it unless short_rate is set.
    rate: str
    mark_price: str | None = None
    # What shorts pay per interval (positive means shorts pay), for venues
    # where the two sides differ (GMX adaptive funding plus borrowing). None:
    # symmetric, shorts receive `rate` (methodology "Paper copying v2:
    # funding").
    short_rate: str | None = None


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


class UnsupportedActivity(BaseModel):
    """An activity Koine cannot yet put in the ledger. It is skipped (never
    guessed at) and surfaced so the account shows a visible "unsupported
    activity" flag: numbers after it may not reconcile. Persisted as
    venue_activity_flags rows (wave2; moved here from alpaca.py)."""

    venue_ref: str
    activity_type: str
    activity_sub_type: str | None = None
    symbol: str | None = None
    occurred_at: datetime
    reason: str  # short_sale | option | crypto | stock_dividend | canceled | unsupported_type
    description: str | None = None
