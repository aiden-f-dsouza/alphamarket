"""Lighter connector (perps and spot on Lighter's zk-rollup exchange), watch only.

Lighter accounts belong to an Ethereum (L1) address and every read Koine
makes is public and unauthenticated, so Koine holds no credential
(CredentialKind.ADDRESS, ReadOnlyProof.NONE). Ownership is an EIP-191
`personal_sign` signature by the address itself (claim_message below).
docs/venues/lighter.md has the terms position (PENDING COUNSEL), the rate
limits and every [VERIFY]; docs/onchain_perps_plan.md section 3 "W-L" is the
spec this module follows.

Hosts:
  API       https://mainnet.zklighter.elliot.ai/api/v1  accountsByL1Address,
            account, orderBookDetails, orderBookOrders, fundings, candles,
            pnlLeaderboard. 60 requests per minute per IP (Standard tier).
  explorer  https://explorer.elliot.ai/api  accounts/{index}/logs, every
            on-chain action of an account index back to genesis, newest
            first, offset paging. 90 weight per minute per IP, 2 per page.
  websocket wss://mainnet.zklighter.elliot.ai/stream?readonly=true (the plain
            URL refuses US IPs with code 20558; WS_URL is the only one built).

The REST `trades` endpoint (newest 3,000 fills, documented as auth-only) is
NOT used: the founder decision of 2026-10-22 keeps that accelerator off until
counsel Q1 clears, so the explorer is the one fills source (onchain plan
"W-L" L2; omission recorded as a deviation there).

Facts pinned from captures of 2026-10-22 (tests/fixtures/lighter/README.md):

  Composite account. One address owns a master index and sub-account
      indexes (accountsByL1Address, account_type 0 and 1). The Koine account
      is the address: each sync lists its indexes and walks each one's
      explorer log. Fill ids are f"{index}:{tx_hash}" (a trade between two of
      the address's own indexes appears in both logs, one fill per side).
      Transfers between the address's own indexes move nothing.
  Explorer status. Every Trade row is a real trade whatever its `status`
      ("executed", "nothing_to_execute", "committed", null): all 100 rows of
      a page matched a REST trade field for field, 43 of them
      "nothing_to_execute", and a "nothing_to_execute" deposit is in the
      account's cash. Rows are never filtered on status.
  Fees. Rates are integers in millionths of notional (maker 28, taker 196,
      liquidation 10000). price x size is exact at 6 dp on every market
      (size_decimals + price_decimals = 6), so fee = notional_micros x rate
      is exact at 12 dp, and Lighter keeps balances at 12 dp ("margin_balance":
      "17743905.004747396184"): FEE legs are exact, never rounded.
  Realized PnL (derived; the explorer has no PnL). Average cost over
      Lighter's own "entry quote" (|position| x average entry, at 6 dp):
      an increase adds price x size; a decrease keeps
      floor(entry_quote x |size after| / |size before|) and realizes
      (exit quote - closed entry quote) for a long, the reverse for a short;
      a flip closes the whole position at the fill price and opens the
      remainder at it. Pinned on 100 of 100 REST rows (`*_entry_quote_before`
      and `*_account_pnl`, exact to the micro); PnL is gross of the fee.
  Funding (derived; per-account funding is auth-only). Lighter pays funding
      every hour and the account's cash includes it at once; the
      `funding_rate_prefix_sum` on TradeWithFunding rows is only a
      checkpoint. So each hour's payment is -position x value, with `value`
      the market's published USD per base unit (fundings, 8 dp, direction
      "long" = longs pay) and the position replayed from the trades before
      that hour. The prefix sum is USD per base atom x 1e12, and its
      differences equal the hourly values summed (cross-check tests).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

import httpx

from alphamarket.connectors import addresses
from alphamarket.connectors.addresses import AddressKind
from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    DailyClose,
    FundingRate,
    InstrumentType,
    LeaderboardEntry,
    LeaderboardPeriod,
    LegKind,
    MarginTier,
    NormalizedAssetFill,
    NormalizedBalance,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    Page,
    Quote,
    RateBudget,
    RateLimited,
    RateScope,
    ReadOnlyEvidence,
    ReadOnlyProof,
    StreamEvent,
    StreamingSource,
    StreamProtocolError,
    StreamUnavailable,
    UnsupportedActivity,
    leaderboard_figure,
)
from alphamarket.money import MAX_DECIMALS, to_atoms, to_micros

logger = logging.getLogger(__name__)

VENUE = "lighter"
API_URL = "https://mainnet.zklighter.elliot.ai/api/v1"
EXPLORER_URL = "https://explorer.elliot.ai/api"
# The read-only socket is the one Lighter documents for restricted regions;
# the plain /stream URL answers HTTP 400 code 20558 from a US IP. This is the
# only websocket URL the module can build (onchain plan section 4 item 4).
WS_URL = "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
# docs "Rate limits": 255 websocket connections per IP; leave headroom.
WS_USERS_PER_IP = 200

USDC = "USDC"
USDC_DECIMALS = 6
DEFAULT_ASSET_DECIMALS = 8  # LIT and other spot assets print 8 dp
QUOTE_DECIMALS = 6  # every market's supported_quote_decimals
FEE_RATE_SCALE = 6  # fee rates are integers in millionths of notional
FUNDING_LONGS_PAY = "long"
DEFAULT_RETRY_AFTER = 60.0  # docs: a static 60 s firewall cooldown after 429/405

LOGS_LIMIT = 100  # explorer page size
LOGS_OVERLAP = 10  # rows of overlap between forward pages (absorbs new rows)
LOGS_STEP = LOGS_LIMIT - LOGS_OVERLAP
FUNDING_WINDOW_HOURS = 720  # one fundings request (the API serves up to 750 rows)
FUNDING_SEGMENT_CAP = 200  # pending funding segments before a funding page runs
# An hour is booked only once its trades are surely in the explorer.
FUNDING_LAG = timedelta(minutes=5)
HOUR = 3600
SUB_ACCOUNT_TYPES = frozenset({0, 1})  # 0 master, 1 sub-account; pools are others

# ---- rate budgets -----------------------------------------------------------
# https://apidocs.lighter.xyz/docs/rate-limits (read 2026-10-22). Two hosts,
# two IP-scoped buckets; each gives the other host's endpoints cost 0
# (base.RateBudget), so a request is charged only to its own host.
EXPLORER_ENDPOINTS = ("explorer_logs",)
API_BUDGET = RateBudget(
    scope=RateScope.IP,
    capacity=60,
    refill_per_minute=60,  # Standard (unauthenticated): 60 requests per rolling minute
    costs=tuple((endpoint, 0) for endpoint in EXPLORER_ENDPOINTS),
    default_cost=1,  # weights only bite on Premium/Builder budgets
)
EXPLORER_BUDGET = RateBudget(
    scope=RateScope.IP,
    capacity=90,
    refill_per_minute=90,  # 90 weight per minute
    costs=(("explorer_logs", 2),),  # accounts/* weigh 2
    default_cost=0,  # every API endpoint
)
RATE_BUDGETS = (API_BUDGET, EXPLORER_BUDGET)

# ---- leaderboard -------------------------------------------------------------
LEADERBOARD_WINDOWS = {
    LeaderboardPeriod.DAY: "24h",
    LeaderboardPeriod.WEEK: "7d",
    LeaderboardPeriod.MONTH: "30d",
    LeaderboardPeriod.ALL: "all",
}
LEADERBOARD_LIMIT = 100  # the endpoint's maximum page size
LEADERBOARD_PNL_PAGES = 3  # per window, by pnl
LEADERBOARD_ROI_PAGES = 2  # per window, by roi (tiny accounts top it)
LEADERBOARD_KEEP = 400  # eligible rows per window when no caller says

# ---- what paper copying skips (onchain plan S2, counsel Q3) --------------------
# Lighter's market list carries no category field, so the non-crypto markets
# are named: synthetic US and Asian equities and ETFs, indices, pre-IPO names,
# FX, rates and commodities (orderBookDetails, 2026-10-22) [VERIFY on every
# new listing; an unknown symbol defaults to copyable].
NON_CRYPTO_SYMBOLS = frozenset(
    {
        # equities and ETFs
        "AAOI", "AAPL", "ADI", "AMD", "AMZN", "ARM", "ASML", "AVGO", "AXTI", "BABA",
        "BE", "BMNR", "BOTZ", "BYD", "CHIP", "COIN", "CRCL", "CRWV", "DELL", "DIA",
        "DRAM", "EWY", "GEV", "GME", "GOOGL", "HANMI", "HOOD", "HYUNDAI", "HYUNDAIUSD",
        "IBM", "INTC", "IWM", "KIOXIA", "KORU", "MAGS", "META", "MRNA", "MRVL", "MSFT",
        "MSTR", "MU", "NBIS", "NOK", "NOW", "NVDA", "ORCL", "PLTR", "POPMART", "QCOM",
        "QQQ", "RKLB", "SAMSUNG", "SAMSUNGUSD", "SKHY", "SKHYNIX", "SKHYNIXUSD", "SMIC",
        "SNDK", "SOXL", "SOXS", "SOXX", "SPY", "STRC", "TENCENT", "TSLA", "TSM", "TTWO",
        "URA", "WDC", "XIAOMI",
        # indices and rates
        "KRCOMP", "SPX", "TUR30", "US100", "US10Y", "US500",
        # pre-IPO and private companies
        "ANTHROPIC", "CBRS", "CXMT", "MINIMAX", "OPENAI", "OURA", "SHEIN", "SPACEX",
        "SPCX", "UNITREE", "ZHIPU",
        # FX
        "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD", "USDCAD", "USDCHF", "USDHKD", "USDJPY",
        "USDKRW",
        # commodities (and compute rental prices)
        "BRENTOIL", "H100", "NATGAS", "PAXG", "WHEAT", "WTI", "XAG", "XAU", "XCU",
        "XPD", "XPT",
        # spot tokens of the same kind
        "rhSPY", "rhQQQ", "XAUT",
    }
)  # fmt: skip

# Explorer pubdata types that never move value in or out of the account.
# Margin moves between cross and isolated stay inside it (fetch_balances adds
# allocated margin back to cash).
NOOP_TYPES = frozenset(
    {
        "L2UpdateLeverage",
        "L2UpdateMargin",
        "L2UpdateAccountConfig",
        "L2CreateSubAccount",
        "L2ChangePubKey",
        "L1ChangePubKey",
        "L2CancelOrder",
        "L2CancelAllOrders",
        "L2ModifyOrder",
        "L2CreateOrder",
        "L2CreateGroupedOrders",
    }
)
DEPOSIT_TYPES = frozenset({"L1DepositV2", "L1Deposit"})
WITHDRAW_TYPES = frozenset({"WithdrawV2", "Withdraw"})
TRANSFER_TYPES = frozenset({"L2TransferV2", "L2Transfer"})
# Public-pool (LLP) shares and LIT staking: value leaves the trading account
# into a pool and comes back. Booked as transfers out and in (the holding is
# outside the reconciled vector) AND flagged, so the account says so.
POOL_OUT = {"L2MintShares": ("usdc_amount", USDC), "L2StakeAssets": ("lit_amount", "LIT")}
POOL_IN = {
    "BurnedShares": ("usdc_amount", USDC),
    "L2BurnShares": ("usdc_amount", USDC),
    "UnstakeAssets": ("lit_amount", "LIT"),
    "L2UnstakeAssets": ("lit_amount", "LIT"),
    "ExecutedPendingUnlock": ("lit_amount", "LIT"),
}
TRADE_PUBDATA_KEYS = ("trade_pubdata", "trade_pubdata_with_funding")


class LighterError(ValueError):
    """Lighter answered something the connector cannot use (an unknown
    shape, an explorer history that changed under a walk)."""


# ---- small helpers -------------------------------------------------------------


def _s(value: Any) -> str:
    """A venue number as a clean decimal string. JSON floats arrive as
    Decimal (every body is parsed with parse_float=Decimal), never float."""
    if isinstance(value, bool | float):
        raise TypeError(f"expected a decimal, got {value!r}")
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value).strip()


def _dec(value: Any) -> Decimal:
    return Decimal(_s(value))


def _places(value: str) -> int:
    exponent = Decimal(value).normalize().as_tuple().exponent
    return max(0, -int(exponent))


def atoms(value: Any, decimals: int) -> tuple[int, int]:
    """(atoms, decimals) for a venue decimal string, at `decimals` or the
    value's own finer precision (<= 12 dp, never rounded)."""
    text = _s(value)
    used = max(decimals, _places(text))
    if used > MAX_DECIMALS:
        raise LighterError(f"{text!r} is finer than {MAX_DECIMALS} dp")
    return to_atoms(text, used), used


def round_atoms(value: int, decimals: int) -> tuple[int, int]:
    """Atoms at `decimals`, brought to at most 12 dp half-even (only derived
    funding needs it: a 5-dp position times an 8-dp value has 13 dp)."""
    if decimals <= MAX_DECIMALS:
        return value, decimals
    exact = Decimal(value).scaleb(-decimals)
    rounded = exact.quantize(Decimal(1).scaleb(-MAX_DECIMALS), rounding=ROUND_HALF_EVEN)
    return to_atoms(rounded, MAX_DECIMALS), MAX_DECIMALS


def _parse_time(text: str) -> datetime:
    raw = str(text).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ts(value: datetime) -> int:
    """Whole seconds since the epoch (UTC)."""
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return int(value.astimezone(UTC).timestamp() // 1)


def _from_ts(seconds: int) -> datetime:
    return datetime.fromtimestamp(int(seconds), UTC)


def _ms_to_dt(ms: int) -> datetime:
    ms = int(ms)
    return datetime.fromtimestamp(ms // 1000, UTC) + timedelta(milliseconds=ms % 1000)


def floor_hour(seconds: int) -> int:
    return seconds - seconds % HOUR


def _now() -> datetime:
    """Wall clock; tests freeze it."""
    return datetime.now(UTC)


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After")
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_RETRY_AFTER
    except ValueError:
        return DEFAULT_RETRY_AFTER


def _loads(text: str) -> Any:
    """Lighter sends JSON floats on the leaderboard, pnl and candles: parse
    them straight to Decimal so no binary float ever exists."""
    return json.loads(text, parse_float=Decimal)


def copyable(symbol: str) -> bool:
    """False for Lighter's synthetic equities, ETFs, indices, pre-IPO names,
    FX and commodities (paper copying skips them: "not_copyable")."""
    base = symbol.split("/", 1)[0]
    return base not in NON_CRYPTO_SYMBOLS


def perp_asset(symbol: str) -> str:
    return f"{symbol}-PERP"


# ---- market metadata ------------------------------------------------------------


@dataclass(frozen=True)
class Market:
    """One row of orderBookDetails, as far as Koine uses it."""

    market_id: int
    symbol: str  # "BTC" (perp) | "LIT/USDC" (spot)
    kind: str  # "perp" | "spot" | anything else (binary options)
    size_decimals: int
    price_decimals: int
    min_base_amount: str | None = None
    min_quote_amount: str | None = None
    maintenance_margin_fraction: int | None = None  # 1e-4 units (120 = 1.2%)
    status: str = "active"

    @property
    def is_perp(self) -> bool:
        return self.kind == "perp"

    @property
    def is_spot(self) -> bool:
        return self.kind == "spot"

    @property
    def base(self) -> str:
        if self.is_spot:
            return self.symbol.split("/", 1)[0]
        return perp_asset(self.symbol)

    @property
    def quote(self) -> str:
        if self.is_spot and "/" in self.symbol:
            return self.symbol.split("/", 1)[1]
        return USDC

    @classmethod
    def parse(cls, row: dict[str, Any]) -> Market:
        mmf = row.get("maintenance_margin_fraction")
        return cls(
            market_id=int(row["market_id"]),
            symbol=str(row["symbol"]),
            kind=str(row.get("market_type") or ""),
            size_decimals=int(row["size_decimals"]),
            price_decimals=int(row["price_decimals"]),
            min_base_amount=_s(row["min_base_amount"]) if row.get("min_base_amount") else None,
            min_quote_amount=_s(row["min_quote_amount"]) if row.get("min_quote_amount") else None,
            maintenance_margin_fraction=int(mmf) if mmf is not None else None,
            status=str(row.get("status") or ""),
        )

    def qty_atoms(self, size: Any) -> int:
        """A trade or position size at the market's size decimals (exact)."""
        return to_atoms(_s(size), self.size_decimals)

    def quote_micros(self, price: Any, qty_atoms: int) -> int:
        """price x size in USDC micros, exactly (Lighter keeps quotes at 6 dp:
        size_decimals + price_decimals = 6 on every market)."""
        notional = _dec(price) * Decimal(qty_atoms).scaleb(-self.size_decimals)
        return to_micros(notional)


def instrument_of(market: Market) -> NormalizedInstrument:
    """The instrument for fills AND fetch_instruments (the stored row follows
    the latest description, so both must agree)."""
    if market.is_perp:
        kind = InstrumentType.PERP
    elif market.is_spot:
        kind = InstrumentType.SPOT
    else:
        kind = InstrumentType.OPTION  # binary up/down markets: rejected at ingest
    min_qty = 0
    if market.min_base_amount is not None:
        try:
            min_qty = to_atoms(market.min_base_amount, market.size_decimals)
        except ValueError:
            min_qty = 0
    return NormalizedInstrument(
        symbol=market.symbol,
        type=kind,
        base_asset=market.base,
        quote_asset=market.quote,
        qty_decimals=market.size_decimals,
        qty_step_atoms=1,
        min_qty_atoms=min_qty,
        min_notional=market.min_quote_amount,
        copyable=copyable(market.symbol),
    )


class _Http:
    """Shared transport and metadata cache for the connector and market data.
    Every request charges the throttle first (endpoint names match the
    RateBudget costs), turns 429/405 into RateLimited, and parses floats to
    Decimal. orderBookDetails is fetched at most once per instance; a budget
    stop on it while mapping a page discards the page unreturned, so its
    cursor never advances and nothing fetched is skipped."""

    api_url: str
    explorer_url: str
    http: httpx.Client
    _markets: dict[int, Market] | None

    def _init_http(self, api_url: str, explorer_url: str, client: httpx.Client | None) -> None:
        self.api_url = api_url.rstrip("/")
        self.explorer_url = explorer_url.rstrip("/")
        self.http = client or httpx.Client(timeout=20.0)
        self._markets = None

    def _get(
        self, url: str, endpoint: str, params: dict[str, Any] | None = None, ok=(200,)
    ) -> tuple[int, Any]:
        self.throttle(endpoint)  # type: ignore[attr-defined]
        resp = self.http.get(url, params=params)
        if resp.status_code in (429, 405):
            raise RateLimited(_retry_after(resp), endpoint)
        if resp.status_code not in ok:
            resp.raise_for_status()
        return resp.status_code, _loads(resp.text)

    def _api(self, path: str, params: dict[str, Any] | None = None, ok=(200,)) -> tuple[int, Any]:
        return self._get(f"{self.api_url}/{path}", path, params, ok)

    def markets(self) -> dict[int, Market]:
        if self._markets is None:
            _, data = self._api("orderBookDetails")
            rows = list(data.get("order_book_details") or []) + list(
                data.get("spot_order_book_details") or []
            )
            self._markets = {}
            for row in rows:
                market = Market.parse(row)
                self._markets[market.market_id] = market
        return self._markets

    def market(self, market_id: int) -> Market | None:
        return self.markets().get(int(market_id))

    def market_by_symbol(self, symbol: str) -> Market | None:
        return next((m for m in self.markets().values() if m.symbol == symbol), None)


# ---- ownership proof (EIP-191 personal_sign) ------------------------------------------

CLAIM_SEPARATOR = "|"


def claim_message(address: str, code: str) -> str:
    """The exact text the owner signs (personal_sign): the shared Koine claim
    format with "Venue: Lighter". The claim code is the nonce."""
    return (
        "Koine ownership claim\n"
        "Venue: Lighter\n"
        f"Address: {address.lower()}\n"
        f"Code: {code}\n"
        "Signing this message proves you control this address. It is not a "
        "transaction: it cannot trade, move funds or approve anything."
    )


def encode_claim_proof(code: str, signature: str) -> str:
    return f"{code}{CLAIM_SEPARATOR}{signature}"


def recover_signer(message: str, signature: str) -> str | None:
    """Lower-case address that produced an EIP-191 signature of `message`, or
    None when the signature is malformed."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:  # malformed hex, bad length, invalid curve point
        return None
    return str(signer).lower()


# ---- average-cost replay (derived realized PnL) ----------------------------------------


def apply_trade(
    position: int, entry_quote: int, signed_qty: int, quote: int, exit_quote_of
) -> tuple[int, int, int]:
    """One fill on Lighter's average-cost book for a perp market.

    position     signed size in atoms before the fill
    entry_quote  |position| x average entry, USDC micros (Lighter's
                 `*_entry_quote_before`)
    signed_qty   + bought, - sold, atoms
    quote        price x |signed_qty|, micros (exact)
    exit_quote_of(qty_atoms) -> micros: price x qty for part of the fill (a
                 flip closes |position| of it)

    Returns (position after, entry quote after, realized PnL micros):
    - increase (flat or same side): entry_quote + quote, nothing realized
    - decrease: keep floor(entry_quote x |after| / |before|); the closed
      entry quote is the rest; a long realizes exit - closed, a short
      closed - exit
    - flip: close all of `position` at the fill price, open the remainder
      at it (its entry quote is price x remainder)"""
    after = position + signed_qty
    if position == 0 or (position > 0) == (signed_qty > 0):
        return after, entry_quote + quote, 0
    held = abs(position)
    if abs(signed_qty) <= held:
        kept = entry_quote * abs(after) // held  # floor: entry_quote >= 0
        closed = entry_quote - kept
        realized = quote - closed if position > 0 else closed - quote
        return after, kept, realized
    exit_quote = exit_quote_of(held)
    realized = exit_quote - entry_quote if position > 0 else entry_quote - exit_quote
    return after, quote - exit_quote, realized


# ---- explorer walk ------------------------------------------------------------------------


@dataclass
class _Walk:
    """Where one account index's explorer walk stands.

    The explorer pages newest-first by offset, and new rows shift every
    offset, but the replays need oldest-first. So a first walk LOCATES the
    oldest page (doubling, then bisecting, on page fullness: a short,
    non-empty page holds the genesis row), then walks FORWARD toward offset
    0 in steps of LOGS_STEP, finding its anchor (the newest row applied) in
    each page: the rows above the anchor are exactly the next ones. Pages
    overlap by LOGS_OVERLAP rows, so up to 9 rows arriving between two
    requests cost nothing; more push the anchor out of the page, and the
    walk steps back (older) until it finds it again. Never a gap; a
    duplicate is impossible because only rows above the anchor are taken."""

    anchor: str | None = None  # hash of the newest row applied
    at: str | None = None  # its explorer time
    offset: int = 0
    mode: str = "head"  # head | locate | forward
    lo: int = 0  # locate: largest offset known to hold a full page
    hi: int | None = None  # locate: smallest offset known to be empty

    def to_json(self) -> dict[str, Any]:
        return {
            "a": self.anchor,
            "t": self.at,
            "o": self.offset,
            "m": self.mode,
            "lo": self.lo,
            "hi": self.hi,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> _Walk:
        data = data or {}
        hi = data.get("hi")
        return cls(
            anchor=data.get("a"),
            at=data.get("t"),
            offset=int(data.get("o", 0)),
            mode=str(data.get("m", "head")),
            lo=int(data.get("lo", 0)),
            hi=int(hi) if hi is not None else None,
        )


def _encode(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), sort_keys=True)


def _decode(cursor: str) -> dict[str, Any]:
    data = json.loads(cursor)
    if not isinstance(data, dict):
        raise LighterError("malformed Lighter cursor")
    return data


def row_refs(index: int, rows: list[dict[str, Any]]) -> list[str]:
    """f"{index}:{hash}" per row (oldest first). Every capture shows one row
    per hash; should a hash repeat within a page, the later rows get
    ":<n>" [VERIFY multi-row transactions]."""
    seen: dict[str, int] = {}
    out = []
    for row in rows:
        tx = str(row.get("hash") or "")
        n = seen.get(tx, 0)
        seen[tx] = n + 1
        out.append(f"{index}:{tx}" if n == 0 else f"{index}:{tx}:{n}")
    return out


def _pubdata(row: dict[str, Any]) -> dict[str, Any]:
    pub = row.get("pubdata") or {}
    if not isinstance(pub, dict) or not pub:
        return {}
    for key in TRADE_PUBDATA_KEYS:
        if key in pub:
            return pub[key] or {}
    value = next(iter(pub.values()))
    return value if isinstance(value, dict) else {}


def is_trade(row: dict[str, Any]) -> bool:
    pub = row.get("pubdata") or {}
    return isinstance(pub, dict) and any(key in pub for key in TRADE_PUBDATA_KEYS)


@dataclass(frozen=True)
class _Side:
    """This index's side of one trade row."""

    market: Market
    sells: bool
    role: str  # "maker" | "taker"
    qty: int  # atoms, > 0
    price: str
    rate: int  # fee rate, millionths

    @property
    def signed_qty(self) -> int:
        return -self.qty if self.sells else self.qty


# ---- connector -----------------------------------------------------------------------------


class LighterConnector(_Http, AssetVenueConnector):
    venue = VENUE

    def __init__(
        self,
        address: str,
        api_url: str = API_URL,
        explorer_url: str = EXPLORER_URL,
        client: httpx.Client | None = None,
    ) -> None:
        # InvalidAddress is a ValueError: build_connector refuses a bad key_id.
        self.address = addresses.normalize(AddressKind.EVM, address)
        self._init_http(api_url, explorer_url, client)
        self._unsupported: list[UnsupportedActivity] = []
        self._indexes: list[int] | None = None

    # ---- identity / proofs ---------------------------------------------------

    def account_indexes(self) -> list[int]:
        """The address's trading indexes (master and sub-accounts), ascending.
        [] when Lighter has no account at the address (HTTP 400, code 21100).
        Public pools the address operates are not its money: left out."""
        status, data = self._api("accountsByL1Address", {"l1_address": self.address}, ok=(200, 400))
        if status == 400:
            if isinstance(data, dict) and int(data.get("code") or 0) == 21100:
                return []
            raise LighterError(f"accountsByL1Address refused: {data!r}"[:200])
        out = []
        for sub in data.get("sub_accounts") or []:
            if int(sub.get("account_type", 0)) in SUB_ACCOUNT_TYPES:
                out.append(int(sub["index"]))
        self._indexes = sorted(set(out))
        return self._indexes

    def check_connection(self) -> NormalizedBalance:
        """Refuses an address Lighter has never seen; then the M7 balance."""
        if not self.account_indexes():
            raise LighterError("Lighter has no account at this address")
        return self.fetch_balance()

    def check_write_scope(self) -> bool | None:
        return False  # no credential is held, so nothing can write

    def prove_read_only(self) -> ReadOnlyEvidence:
        return ReadOnlyEvidence(
            method=ReadOnlyProof.NONE,
            read_only=True,
            checked_at=datetime.now(UTC),
            details={
                "credential": "none (public address)",
                "address": self.address,
                "can_trade": False,
                "can_withdraw": False,
            },
        )

    def claim_text(self, code: str) -> str | None:
        return claim_message(self.address, code)

    def claim_payload(self, code: str, signature: str) -> str:
        return encode_claim_proof(code, signature)

    def verify_claim(self, code: str) -> bool | None:
        """`code` is encode_claim_proof(stored claim code, signature). True
        when the signer is the address itself: a Lighter sub-account is an
        index of the same address, so there is no master or vault
        indirection. Needs no venue call. The caller builds `code` from the
        STORED claim code (replay protection)."""
        claim, sep, signature = code.rpartition(CLAIM_SEPARATOR)
        if not sep or not claim or not signature:
            return False
        return recover_signer(claim_message(self.address, claim), signature) == self.address

    def drain_unsupported(self) -> list[UnsupportedActivity]:
        out, self._unsupported = self._unsupported, []
        return out

    def _flag(self, ref: str, row: dict[str, Any], reason: str, description: str) -> None:
        self._unsupported.append(
            UnsupportedActivity(
                venue_ref=ref,
                activity_type=str(row.get("pubdata_type") or row.get("tx_type") or "unknown"),
                occurred_at=_parse_time(row["time"]),
                reason=reason,
                description=description[:200],
            )
        )

    # ---- explorer --------------------------------------------------------------

    def _logs(self, index: int, offset: int) -> list[dict[str, Any]]:
        _, data = self._get(
            f"{self.explorer_url}/accounts/{index}/logs",
            "explorer_logs",
            {"limit": LOGS_LIMIT, "offset": offset},
        )
        if not isinstance(data, list):
            raise LighterError("unexpected explorer logs payload")
        return data

    def _walk_page(self, index: int, walk: _Walk) -> tuple[list[dict[str, Any]], bool]:
        """ONE explorer request for `index`: (rows to apply, oldest first;
        True when the index is caught up). Mutates `walk`."""
        rows = self._logs(index, walk.offset)
        if walk.anchor is None and walk.mode == "head":
            if len(rows) < LOGS_LIMIT:  # the whole history is this page
                if rows:
                    walk.anchor, walk.at, walk.mode = rows[0]["hash"], rows[0]["time"], "forward"
                return list(reversed(rows)), True
            walk.mode, walk.lo, walk.hi, walk.offset = "locate", 0, None, LOGS_LIMIT
            return [], False
        if walk.mode == "locate":
            if len(rows) >= LOGS_LIMIT:
                walk.lo = walk.offset
            elif not rows:
                walk.hi = walk.offset
            else:  # short page: its last row is the oldest the index has
                walk.anchor, walk.at, walk.mode = rows[0]["hash"], rows[0]["time"], "forward"
                walk.offset = max(0, walk.offset - LOGS_STEP)
                return list(reversed(rows)), False
            walk.offset = walk.lo * 2 if walk.hi is None else (walk.lo + walk.hi) // 2
            return [], False
        at = next((i for i, r in enumerate(rows) if r.get("hash") == walk.anchor), None)
        if at is None:
            if not rows:
                raise LighterError(f"explorer: index {index} lost row {walk.anchor}")
            if walk.at is not None and _parse_time(rows[0]["time"]) < _parse_time(walk.at):
                if walk.offset == 0:
                    raise LighterError(f"explorer: index {index} lost row {walk.anchor}")
                walk.offset = max(0, walk.offset - LOGS_STEP)  # the anchor is newer
            else:
                walk.offset += LOGS_STEP  # rows arrived: the anchor moved older
            return [], False
        fresh = list(reversed(rows[:at]))
        if fresh:
            walk.anchor, walk.at = rows[0]["hash"], rows[0]["time"]
        if walk.offset == 0:
            return fresh, True
        walk.offset = max(0, walk.offset - LOGS_STEP)
        return fresh, False

    def _side(self, index: int, ref: str, row: dict[str, Any]) -> _Side | None:
        """This index's side of a trade row, or None (flagged) when it cannot
        be booked: an unknown or binary market, a self-trade."""
        pd = _pubdata(row)
        market = self.market(int(pd["market_index"]))
        if market is None or not (market.is_perp or market.is_spot):
            self._flag(ref, row, "unsupported_type", f"trade on market {pd.get('market_index')}")
            return None
        taker = int(pd["taker_account_index"])
        maker = int(pd["maker_account_index"])
        taker_sells = bool(int(pd["is_taker_ask"]))
        if taker == index and maker == index:
            self._flag(ref, row, "unsupported_type", "self-trade")
            return None
        if taker == index:
            role, sells, rate = "taker", taker_sells, int(pd.get("taker_fee") or 0)
        elif maker == index:
            role, sells, rate = "maker", not taker_sells, int(pd.get("maker_fee") or 0)
        else:
            self._flag(ref, row, "unsupported_type", f"trade of other indexes on index {index}")
            return None
        qty = market.qty_atoms(pd["size"])
        if qty <= 0:
            return None
        return _Side(market, sells, role, qty, _s(pd["price"]), rate)

    # ---- fills ------------------------------------------------------------------

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        """One request per page: the index list at the start of each pass,
        then explorer pages per index. The replay state (per index, per perp
        market: signed size and entry quote) rides in the cursor, so `since`
        is ignored: a None cursor always replays from genesis, and a restart
        reproduces identical legs."""
        state = _decode(cursor) if cursor is not None else {"p": "discover"}
        if state.get("p", "discover") == "discover":
            return self._discover(state)
        indexes = [int(i) for i in state.get("ix") or []]
        k = int(state.get("k", 0))
        index = indexes[k]
        walks = state.setdefault("w", {})
        books = state.setdefault("r", {})
        walk = _Walk.from_json(walks.get(str(index)))
        rows, done = self._walk_page(index, walk)
        book = {int(m): (int(v[0]), int(v[1])) for m, v in (books.get(str(index)) or {}).items()}
        items = []
        for ref, row in zip(row_refs(index, rows), rows, strict=True):
            if is_trade(row):
                fill = self._fill(index, ref, row, book)
                if fill is not None:
                    items.append(fill)
        walks[str(index)] = walk.to_json()
        books[str(index)] = {str(m): [p, e] for m, (p, e) in sorted(book.items()) if p or e}
        return self._advance(state, done, items)

    def _discover(self, state: dict[str, Any]) -> Page:
        indexes = self.account_indexes()
        state.update(p="walk", ix=indexes, k=0)
        if not indexes:
            state["p"] = "discover"
            return Page([], resume_cursor=_encode(state))
        return Page([], next_cursor=_encode(state))

    @staticmethod
    def _advance(state: dict[str, Any], done: bool, items: list) -> Page:
        if done:
            state["k"] = int(state.get("k", 0)) + 1
            if state["k"] >= len(state.get("ix") or []):
                state.update(p="discover", k=0)
                return Page(items, resume_cursor=_encode(state))
        return Page(items, next_cursor=_encode(state))

    def _fill(
        self, index: int, ref: str, row: dict[str, Any], book: dict[int, tuple[int, int]]
    ) -> NormalizedAssetFill | None:
        side = self._side(index, ref, row)
        if side is None:
            return None
        market = side.market
        quote = market.quote_micros(side.price, side.qty)
        symbol = market.symbol
        legs: list[NormalizedLeg] = []
        if market.is_perp:
            position, entry = book.get(market.market_id, (0, 0))
            position, entry, realized = apply_trade(
                position,
                entry,
                side.signed_qty,
                quote,
                lambda qty: market.quote_micros(side.price, qty),
            )
            book[market.market_id] = (position, entry)
            legs.append(
                NormalizedLeg(
                    asset=market.base,
                    delta_atoms=side.signed_qty,
                    decimals=market.size_decimals,
                    kind=LegKind.FILL_BASE,
                    instrument_symbol=symbol,
                )
            )
            if realized:
                legs.append(
                    NormalizedLeg(
                        asset=USDC,
                        delta_atoms=realized,
                        decimals=USDC_DECIMALS,
                        kind=LegKind.REALIZED_PNL,
                        instrument_symbol=symbol,
                    )
                )
            fee_asset, fee, fee_dp = USDC, quote * side.rate, QUOTE_DECIMALS + FEE_RATE_SCALE
        else:
            legs.append(
                NormalizedLeg(
                    asset=market.base,
                    delta_atoms=side.signed_qty,
                    decimals=market.size_decimals,
                    kind=LegKind.FILL_BASE,
                    instrument_symbol=symbol,
                )
            )
            legs.append(
                NormalizedLeg(
                    asset=market.quote,
                    delta_atoms=quote if side.sells else -quote,
                    decimals=QUOTE_DECIMALS,
                    kind=LegKind.FILL_QUOTE,
                    instrument_symbol=symbol,
                )
            )
            # Spot fees are taken from what the trade delivers: the base asset
            # on a buy, the quote on a sell [VERIFY on an account with spot
            # fees; every spot fee in the captures was charged to the other side].
            if side.sells:
                fee_asset, fee, fee_dp = market.quote, quote * side.rate, 12
            else:
                fee_asset = market.base
                fee, fee_dp = side.qty * side.rate, market.size_decimals + FEE_RATE_SCALE
        if fee:
            legs.append(
                NormalizedLeg(
                    asset=fee_asset,
                    delta_atoms=-fee,
                    decimals=fee_dp,
                    kind=LegKind.FEE,
                    instrument_symbol=symbol,
                )
            )
        return NormalizedAssetFill(
            venue_fill_id=ref,
            instrument=instrument_of(market),
            action=Action.SELL if side.sells else Action.BUY,
            qty_atoms=side.qty,
            price=side.price,
            legs=legs,
            filled_at=_parse_time(row["time"]),
            liquidity=side.role,
        )

    # ---- events: flows from the explorer, then derived hourly funding ---------

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        """Deposits, withdrawals, transfers and pool moves from each index's
        explorer log, plus one FUNDING event per (index, perp market, hour)
        with a position. The walk replays perp positions and records
        "segments" (index, market, position, first hour, last hour); funding
        pages then fetch the market's hourly values (one request per market
        per 720 hours) and book them. Like fills, `since` is ignored: a None
        cursor replays from genesis."""
        state = _decode(cursor) if cursor is not None else {"p": "discover"}
        phase = state.get("p", "discover")
        if phase == "discover":
            return self._discover(state)
        if phase == "fund":
            return self._funding_page(state)
        indexes = [int(i) for i in state.get("ix") or []]
        k = int(state.get("k", 0))
        index = indexes[k]
        own = set(indexes)
        walks = state.setdefault("w", {})
        positions = state.setdefault("q", {})
        funded = state.setdefault("ft", {})
        segments = state.setdefault("sg", [])
        walk = _Walk.from_json(walks.get(str(index)))
        rows, done = self._walk_page(index, walk)
        held = {int(m): int(p) for m, p in (positions.get(str(index)) or {}).items()}
        through = {int(m): int(t) for m, t in (funded.get(str(index)) or {}).items()}
        items: list[NormalizedEvent] = []
        for ref, row in zip(row_refs(index, rows), rows, strict=True):
            if is_trade(row):
                self._track_position(index, ref, row, held, through, segments)
                continue
            event = self._flow(index, ref, row, own)
            if event is not None:
                items.append(event)
        if done:  # the open positions' hours up to now (minus the lag)
            cutoff = floor_hour(_ts(_now() - FUNDING_LAG))
            for market_id, position in sorted(held.items()):
                last = through.get(market_id)
                if position and last is not None and cutoff > last:
                    segments.append([index, market_id, position, last + HOUR, cutoff])
                    through[market_id] = cutoff
        walks[str(index)] = walk.to_json()
        positions[str(index)] = {str(m): p for m, p in sorted(held.items()) if p}
        funded[str(index)] = {str(m): t for m, t in sorted(through.items())}
        if done:
            state["k"] = k + 1
            if state["k"] >= len(indexes):
                state.update(p="fund", then="end")
                return self._fund_or_finish(state, items)
        if len(segments) >= FUNDING_SEGMENT_CAP:
            state.update(p="fund", then="walk")
        return Page(items, next_cursor=_encode(state))

    def _fund_or_finish(self, state: dict[str, Any], items: list) -> Page:
        if state.get("sg"):
            return Page(items, next_cursor=_encode(state))
        return self._finish(state, items)

    @staticmethod
    def _finish(state: dict[str, Any], items: list) -> Page:
        then = state.pop("then", "end")
        if then == "walk":
            state["p"] = "walk"
            return Page(items, next_cursor=_encode(state))
        state.update(p="discover", k=0)
        return Page(items, resume_cursor=_encode(state))

    def _track_position(
        self,
        index: int,
        ref: str,
        row: dict[str, Any],
        held: dict[int, int],
        through: dict[int, int],
        segments: list[list[int]],
    ) -> None:
        """Replay a trade into the perp position and close the funding
        segment it ends. An hour's funding applies to the position held
        just before the hour: a trade at or after hh:00:00 counts after it."""
        pd = _pubdata(row)
        market = self.market(int(pd["market_index"]))
        if market is None or not market.is_perp:
            return  # the fills stream flags what it cannot book
        side = self._side(index, ref, row)
        if side is None:
            return
        market_id = market.market_id
        hour = floor_hour(_ts(_parse_time(row["time"])))
        before = held.get(market_id, 0)
        last = through.get(market_id)
        if before and last is not None and hour > last:
            segments.append([index, market_id, before, last + HOUR, hour])
        through[market_id] = max(hour, last) if last is not None else hour
        held[market_id] = before + side.signed_qty

    def _funding_page(self, state: dict[str, Any]) -> Page:
        """ONE fundings request: the market of the oldest pending segment,
        720 hours from that segment's first hour; every pending segment of
        that market inside the window is booked and trimmed."""
        segments: list[list[int]] = state.get("sg") or []
        if not segments:
            return self._finish(state, [])
        segments.sort(key=lambda s: (s[3], s[0], s[1]))
        market_id, start = segments[0][1], segments[0][3]
        end = start + (FUNDING_WINDOW_HOURS - 1) * HOUR
        market = self.market(market_id)
        if market is None:
            raise LighterError(f"funding for unknown market {market_id}")
        values = self._fundings(market_id, start, end)
        items: list[NormalizedEvent] = []
        kept = []
        for seg in segments:
            index, seg_market, position, first, last = seg
            if seg_market != market_id or first > end:
                kept.append(seg)
                continue
            for hour in range(first, min(last, end) + 1, HOUR):
                value = values.get(hour)
                if value is not None:
                    event = funding_event(index, market, position, hour, *value)
                    if event is not None:
                        items.append(event)
            if last > end:
                kept.append([index, seg_market, position, end + HOUR, last])
        state["sg"] = kept
        if kept:
            return Page(items, next_cursor=_encode(state))
        return self._finish(state, items)

    def _fundings(self, market_id: int, start: int, end: int) -> dict[int, tuple[str, str]]:
        """{hour: (value, direction)} for [start, end]. The endpoint returns the
        `count_back` rows ending at end_timestamp (its start_timestamp is not
        what bounds the answer), so count_back is the window's hour count."""
        hours = (end - start) // HOUR + 1
        _, data = self._api(
            "fundings",
            {
                "market_id": market_id,
                "resolution": "1h",
                "start_timestamp": start,
                "end_timestamp": end,
                "count_back": hours,
            },
        )
        out = {}
        for r in data.get("fundings") or []:
            hour = int(r["timestamp"])
            if start <= hour <= end:
                out[hour] = (_s(r["value"]), str(r["direction"]))
        return out

    def _flow(
        self, index: int, ref: str, row: dict[str, Any], own: set[int]
    ) -> NormalizedEvent | None:
        kind_name = str(row.get("pubdata_type") or "")
        pd = _pubdata(row)
        when = _parse_time(row["time"])
        if kind_name in NOOP_TYPES or not pd:
            return None
        if kind_name in DEPOSIT_TYPES:
            if int(pd.get("account_index", index)) != index:
                return None
            amount = pd.get("accepted_amount", pd.get("amount", pd.get("usdc_amount")))
            if amount is None:
                self._flag(ref, row, "unsupported_type", "deposit without an amount")
                return None
            asset = str(pd.get("asset_index") or USDC)
            return _event(
                ref, LegKind.DEPOSIT, [_leg(asset, amount, LegKind.DEPOSIT)], when, kind_name
            )
        if kind_name in WITHDRAW_TYPES:
            if int(pd.get("from_account_index", index)) != index:
                return None
            asset = str(pd.get("asset_index") or USDC)
            legs = [_leg(asset, pd["amount"], LegKind.WITHDRAWAL, -1)]
            return _event(ref, LegKind.WITHDRAWAL, legs, when, kind_name)
        if kind_name in TRANSFER_TYPES:
            return self._transfer(index, ref, pd, own, when, kind_name)
        if kind_name in POOL_OUT or kind_name in POOL_IN:
            return self._pool(index, ref, row, pd, when, kind_name)
        self._flag(ref, row, "unsupported_type", f"explorer {kind_name}")
        return None

    def _transfer(
        self,
        index: int,
        ref: str,
        pd: dict[str, Any],
        own: set[int],
        when: datetime,
        kind_name: str,
    ) -> NormalizedEvent | None:
        """Moves between the address's own indexes (and between an index's
        spot and perps routes) change nothing; the sender's walk books the
        fee once. Anything else enters or leaves the account."""
        sender = int(pd["from_account_index"])
        receiver = int(pd["to_account_index"])
        asset = str(pd.get("asset_index") or USDC)
        fee = pd.get("usdc_fee")
        fee_legs = [_leg(USDC, fee, LegKind.FEE, -1)] if fee and _dec(fee) else []
        route = f"{pd.get('from_route_type', '')}->{pd.get('to_route_type', '')}"
        if sender == index and receiver in own:
            if not fee_legs:
                return None
            return _event(ref, LegKind.CONVERT, fee_legs, when, f"{kind_name} {asset} {route}")
        if sender == index:
            legs = [_leg(asset, pd["amount"], LegKind.TRANSFER_OUT, -1), *fee_legs]
            text = f"{kind_name} {asset} to index {receiver}"
            return _event(ref, LegKind.TRANSFER_OUT, legs, when, text)
        if receiver == index and sender not in own:
            legs = [_leg(asset, pd["amount"], LegKind.TRANSFER_IN)]
            text = f"{kind_name} {asset} from index {sender}"
            return _event(ref, LegKind.TRANSFER_IN, legs, when, text)
        return None

    def _pool(
        self,
        index: int,
        ref: str,
        row: dict[str, Any],
        pd: dict[str, Any],
        when: datetime,
        kind_name: str,
    ) -> NormalizedEvent | None:
        """Public-pool shares and LIT staking: value leaving for the pool is a
        transfer out, value coming back a transfer in (the shares are outside
        the reconciled vector), and the account is flagged either way."""
        self._flag(ref, row, "pool_share", f"{kind_name} (pool or staking holding)")
        if int(pd.get("account_index", index)) != index:
            return None
        outgoing = kind_name in POOL_OUT
        field_name, asset = (POOL_OUT if outgoing else POOL_IN)[kind_name]
        amount = pd.get(field_name)
        if amount is None:
            return None
        kind = LegKind.TRANSFER_OUT if outgoing else LegKind.TRANSFER_IN
        legs = [_leg(asset, amount, kind, -1 if outgoing else 1)]
        return _event(ref, kind, legs, when, f"{kind_name} {asset}")

    # ---- balances -----------------------------------------------------------------

    def _account(self, index: int) -> dict[str, Any]:
        _, data = self._api("account", {"by": "index", "value": index})
        accounts = data.get("accounts") or []
        if not accounts:
            raise LighterError(f"account {index} not found")
        return accounts[0]

    def fetch_balances(self) -> NormalizedBalances:
        """The address's vector, summed over its indexes:

        - each asset: `balance` (spot) + `margin_balance` (cross collateral,
          12 dp), plus every position's `allocated_margin` (isolated) for
          USDC: cash EXCLUDING unrealized PnL
        - perps as "<SYMBOL>-PERP", signed by `sign`, with avg_entry_price
          (when one index holds the market)
        - equity_usd_micros: the sum of `total_asset_value` (collateral +
          isolated margin + unrealized PnL, perps only) plus spot USDC, when
          nothing else is held; None otherwise (Lighter values no spot asset)
        Public-pool shares and pending unlocks are not in the vector."""
        indexes = self.account_indexes()
        cash: dict[str, Decimal] = {}
        spot_usdc = Decimal(0)
        equity = Decimal(0)
        perps: dict[str, list[tuple[Decimal, str | None, int]]] = {}
        for index in indexes:
            acc = self._account(index)
            equity += _dec(acc.get("total_asset_value") or "0")
            for a in acc.get("assets") or []:
                symbol = str(a["symbol"])
                held = _dec(a.get("balance") or "0") + _dec(a.get("margin_balance") or "0")
                cash[symbol] = cash.get(symbol, Decimal(0)) + held
                if symbol == USDC:
                    spot_usdc += _dec(a.get("balance") or "0")
            for p in acc.get("positions") or []:
                allocated = _dec(p.get("allocated_margin") or "0")
                if allocated:
                    cash[USDC] = cash.get(USDC, Decimal(0)) + allocated
                size = _dec(p.get("position") or "0")
                if not size:
                    continue
                sign = -1 if int(p.get("sign", 1)) < 0 else 1
                market = self.market(int(p["market_id"]))
                dp = market.size_decimals if market else _places(_s(p["position"]))
                perps.setdefault(perp_asset(str(p["symbol"])), []).append(
                    (
                        sign * size,
                        _s(p["avg_entry_price"]) if p.get("avg_entry_price") else None,
                        dp,
                    )
                )
        assets: list[AssetAmount] = []
        for symbol, amount in sorted(cash.items()):
            if amount == 0:
                continue
            value, dp = atoms(amount, USDC_DECIMALS if symbol == USDC else DEFAULT_ASSET_DECIMALS)
            assets.append(AssetAmount(asset=symbol, atoms=value, decimals=dp))
        others = any(a.asset != USDC for a in assets)
        for asset, held in sorted(perps.items()):
            dp = max(d for _, _, d in held)
            total = sum((size for size, _, _ in held), Decimal(0))
            if not total:
                continue
            value, dp = atoms(total, dp)
            entry = held[0][1] if len(held) == 1 else None
            assets.append(AssetAmount(asset=asset, atoms=value, decimals=dp, entry_price=entry))
        equity_micros = None
        if not others:
            total_equity = (equity + spot_usdc).scaleb(6)
            equity_micros = int(total_equity.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        return NormalizedBalances(as_of=_now(), assets=assets, equity_usd_micros=equity_micros)

    # ---- streaming -------------------------------------------------------------------

    def stream_source(self) -> LighterTradeStream:
        return LighterTradeStream(self)


# ---- event helpers ----------------------------------------------------------------------


def _leg(asset: str, amount: Any, kind: LegKind, sign: int = 1) -> NormalizedLeg:
    value, dp = atoms(amount, USDC_DECIMALS if asset == USDC else DEFAULT_ASSET_DECIMALS)
    return NormalizedLeg(asset=asset, delta_atoms=sign * value, decimals=dp, kind=kind)


def _event(
    ref: str, kind: LegKind, legs: list[NormalizedLeg], when: datetime, description: str
) -> NormalizedEvent:
    return NormalizedEvent(
        venue_ref=ref, kind=kind, legs=legs, occurred_at=when, description=description
    )


def funding_payment(
    position: int, size_decimals: int, value: str, direction: str
) -> tuple[int, int]:
    """(atoms, decimals) of USDC one hourly funding pays the account:
    -position x value when longs pay ("long"), +position x value when shorts
    pay ("short"); `value` is USD per base unit (8 dp), so the product is
    exact at size_decimals + 8 dp and rounded half-even only past 12 dp."""
    if direction not in ("long", "short"):
        raise LighterError(f"unknown funding direction {direction!r}")
    per_unit, value_dp = atoms(value, 8)
    sign = -1 if direction == FUNDING_LONGS_PAY else 1
    return round_atoms(sign * position * per_unit, size_decimals + value_dp)


def funding_event(
    index: int, market: Market, position: int, hour: int, value: str, direction: str
) -> NormalizedEvent | None:
    amount, dp = funding_payment(position, market.size_decimals, value, direction)
    if not amount:
        return None
    return NormalizedEvent(
        venue_ref=f"funding:{index}:{market.market_id}:{hour}",
        kind=LegKind.FUNDING,
        legs=[
            NormalizedLeg(
                asset=USDC,
                delta_atoms=amount,
                decimals=dp,
                kind=LegKind.FUNDING,
                instrument_symbol=market.symbol,
            )
        ],
        occurred_at=_from_ts(hour),
        description=(
            f"derived funding {market.symbol} index {index}: position "
            f"{Decimal(position).scaleb(-market.size_decimals)} x {value} ({direction}s pay)"
        ),
    )


# ---- public market data -----------------------------------------------------------------


class LighterMarketData(_Http, AssetMarketData):
    """Public reads: instruments (orderBookDetails), top of book
    (orderBookOrders), hourly funding (fundings), daily closes (candles),
    margin tiers and the pnlLeaderboard. Symbols are Lighter market symbols:
    "BTC" for the perp, "LIT/USDC" for spot."""

    venue = VENUE

    def __init__(
        self,
        api_url: str = API_URL,
        client: httpx.Client | None = None,
        leaderboard_keep: int = LEADERBOARD_KEEP,
        leaderboard_min_account_value: str | None = None,
    ) -> None:
        self._init_http(api_url, EXPLORER_URL, client)
        self.leaderboard_keep = leaderboard_keep
        self.leaderboard_min_account_value = leaderboard_min_account_value

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        return [
            instrument_of(m)
            for m in sorted(self.markets().values(), key=lambda m: m.market_id)
            if m.is_perp or m.is_spot
        ]

    def _require(self, symbol: str) -> Market | None:
        return self.market_by_symbol(symbol)

    def fetch_quote(self, symbol: str) -> Quote | None:
        market = self._require(symbol)
        if market is None:
            return None
        _, book = self._api("orderBookOrders", {"market_id": market.market_id, "limit": 1})
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        bid = _s(bids[0]["price"]) if bids else None
        ask = _s(asks[0]["price"]) if asks else None
        if bid is None and ask is None:
            return None
        return Quote(symbol=symbol, bid=bid, ask=ask, as_of=_now())

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """UTC daily candles; today's still-open candle is not a close."""
        market = self._require(symbol)
        if market is None:
            return []
        start_ts = _ts(datetime(start.year, start.month, start.day, tzinfo=UTC))
        end_ts = _ts(datetime(end.year, end.month, end.day, tzinfo=UTC))
        _, data = self._api(
            "candles",
            {
                "market_id": market.market_id,
                "resolution": "1d",
                "start_timestamp": start_ts,
                "end_timestamp": end_ts + 86_400,
                "count_back": (end - start).days + 1,
            },
        )
        today = _now().date()
        out = []
        for c in sorted(data.get("c") or [], key=lambda c: int(c["t"])):
            day = _ms_to_dt(c["t"]).date()
            if day >= today or not start <= day <= end:
                continue
            out.append(DailyClose(symbol=symbol, day=day, close=_s(c["c"])))
        return out

    def fetch_funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingRate]:
        """Hourly rates, positive when longs pay. Lighter's `rate` is a percent
        printed to 4 dp ("0.0012" = 0.0012% per hour), so the stored fraction is
        rate / 100 signed by `direction`; the exact USD amount (`value`) is what
        the creator's ledger uses (fetch_events_page)."""
        market = self._require(symbol)
        if market is None or not market.is_perp:
            return []
        first = floor_hour(_ts(start)) + (HOUR if _ts(start) % HOUR else 0)
        last = floor_hour(_ts(end))
        out = []
        cursor = first
        while cursor <= last:
            window_end = min(last, cursor + (FUNDING_WINDOW_HOURS - 1) * HOUR)
            _, data = self._api(
                "fundings",
                {
                    "market_id": market.market_id,
                    "resolution": "1h",
                    "start_timestamp": cursor,
                    "end_timestamp": window_end,
                    "count_back": (window_end - cursor) // HOUR + 1,
                },
            )
            for r in sorted(data.get("fundings") or [], key=lambda r: int(r["timestamp"])):
                hour = int(r["timestamp"])
                if not cursor <= hour <= window_end:
                    continue
                rate = _dec(r["rate"]) / 100
                if str(r["direction"]) != FUNDING_LONGS_PAY:
                    rate = -rate
                out.append(FundingRate(symbol=symbol, at=_from_ts(hour), rate=format(rate, "f")))
            cursor = window_end + HOUR
        return out

    def fetch_margin_tiers(self, symbol: str) -> list[MarginTier]:
        """One tier from the market's maintenance margin fraction (1e-4
        units). Koine's paper engine models maintenance as 1 / (2 x max
        leverage), so max_leverage = floor(5000 / fraction) keeps Koine's
        rate at or above Lighter's (BTC 120 -> 41x -> 1.22% >= 1.20%).
        No request once orderBookDetails is cached."""
        market = self._require(symbol)
        if market is None or not market.is_perp or not market.maintenance_margin_fraction:
            return []
        return [
            MarginTier(
                lower_bound="0", max_leverage=max(1, 5000 // market.maintenance_margin_fraction)
            )
        ]

    # ---- leaderboard ------------------------------------------------------------------

    def _board(self, window: str, sort: str, page: int) -> list[dict[str, Any]]:
        _, data = self._api(
            "pnlLeaderboard",
            {
                "time_window": window,
                "sort_by": sort,
                "sort_dir": "desc",
                "limit": LEADERBOARD_LIMIT,
                "offset": page * LEADERBOARD_LIMIT,
            },
        )
        return list(data.get("entries") or [])

    def fetch_leaderboards(
        self, periods: tuple[LeaderboardPeriod, ...]
    ) -> dict[LeaderboardPeriod, list[LeaderboardEntry]]:
        """Per window (24h, 7d, 30d, all): the top pages by pnl, then by roi.
        Lighter ranks server side; a row keeps its pnl rank, and a row seen
        only in the roi pages is ranked after every fetched pnl row (its pnl
        rank is unknown; Koine shows no ranks). Pages stop early once
        `leaderboard_keep` rows clear the account-value floor.

        Row shape (VERIFIED 2026-10-22): l1_address (EIP-55 mixed case),
        account_value, pnl, roi (a PERCENT; its denominator is Lighter's own
        and not pnl / account_value), volume, as JSON floats; plus streak,
        longest_streak, net_position, biggest_position_*, long/short
        percentage, which are dropped. No names exist on the board. Money
        figures go through leaderboard_figure; roi is divided by 100 and
        kept verbatim otherwise. A malformed row is skipped."""
        floor = (
            to_micros(self.leaderboard_min_account_value)
            if self.leaderboard_min_account_value is not None
            else None
        )
        out: dict[LeaderboardPeriod, list[LeaderboardEntry]] = {}
        skipped = 0
        for period, window in LEADERBOARD_WINDOWS.items():
            if period not in periods:
                continue
            entries: dict[str, LeaderboardEntry] = {}
            eligible = 0
            fetched = 0
            for sort, pages in (("pnl", LEADERBOARD_PNL_PAGES), ("roi", LEADERBOARD_ROI_PAGES)):
                base = 0 if sort == "pnl" else fetched
                for page in range(pages):
                    rows = self._board(window, sort, page)
                    for row in rows:
                        entry = _board_entry(row, base)
                        if entry is None:
                            skipped += 1
                            continue
                        if sort == "pnl":
                            fetched = max(fetched, entry.rank)
                        if entry.address in entries:
                            continue
                        if floor is not None and to_micros(entry.account_value or "0") < floor:
                            continue
                        entries[entry.address] = entry
                        if entry.volume is None or to_micros(entry.volume) > 0:
                            eligible += 1
                    if len(rows) < LEADERBOARD_LIMIT or (
                        sort == "pnl" and eligible >= self.leaderboard_keep
                    ):
                        break
            out[period] = sorted(entries.values(), key=lambda e: e.rank)
        if skipped:
            logger.warning("lighter leaderboard: skipped %d malformed rows", skipped)
        return out


def _board_entry(row: dict[str, Any], rank_base: int) -> LeaderboardEntry | None:
    """A LeaderboardEntry from one board row (no identity field exists to
    drop beyond the trading-shape fields), or None when malformed."""
    try:
        address = addresses.normalize(AddressKind.EVM, str(row["l1_address"]))
        roi = row.get("roi")
        return LeaderboardEntry(
            address=address,
            rank=rank_base + int(row["rank"]),
            pnl=leaderboard_figure(_dec(row["pnl"])),
            roi=format((_dec(roi) / 100).normalize(), "f") if roi is not None else None,
            volume=leaderboard_figure(_dec(row["volume"]))
            if row.get("volume") is not None
            else None,
            account_value=(
                leaderboard_figure(_dec(row["account_value"]))
                if row.get("account_value") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, ArithmeticError, InvalidOperation):
        return None


# ---- P7: read-only websocket (StreamMode.NUDGE) ------------------------------------------


class LighterTradeStream(StreamingSource):
    """`account_all_trades/{index}` on the read-only socket, one subscription
    per index of the address. NUDGE mode: a frame with new trades queues the
    account's sync and the explorer path books them. Frames are REST-shaped
    trades, but the legs depend on replay state (derived realized PnL and
    funding, the composite-account transfer rule) the stream process does
    not hold, so they are never ingested directly.

    Frames (VERIFIED by a one-off public probe 2026-10-22):
      connected  {"session_id": "...", "type": "connected"}
      snapshot   {"channel": "account_all_trades:<index>", "trades": {"<market>": [...]},
                  "type": "subscribed/account_all_trades", ...volumes}
      update     the same with "type": "update/account_all_trades"
      ping/pong  {"type":"ping"} is answered {"type":"pong"}
    The snapshot nudges nothing (the poller covers history); an update with
    any trade nudges."""

    venue = VENUE
    ping_interval_seconds = 30.0
    idle_timeout_seconds = 90.0

    def __init__(self, connector: LighterConnector) -> None:
        self.connector = connector
        self.indexes: list[int] = []

    def prepare(self) -> None:
        self.indexes = self.connector.account_indexes()
        if not self.indexes:
            raise StreamUnavailable("lighter: no account at this address")

    def url(self) -> str:
        return WS_URL

    def subscribe_frames(self) -> list[str]:
        return [
            json.dumps({"type": "subscribe", "channel": f"account_all_trades/{index}"})
            for index in self.indexes
        ]

    def ping_frame(self) -> str | None:
        return json.dumps({"type": "ping"})

    def parse(self, frame: str | bytes) -> list[StreamEvent]:
        try:
            msg = json.loads(frame)
        except ValueError:
            raise StreamProtocolError("lighter: frame is not JSON") from None
        if not isinstance(msg, dict):
            return []
        kind = str(msg.get("type") or "")
        if kind == "error" or "error" in msg:
            error = msg.get("error") if isinstance(msg.get("error"), dict) else msg
            text = str(error.get("message") or error)[:200]
            if int(error.get("code") or 0) == 20558:  # restricted jurisdiction
                raise StreamUnavailable(f"lighter: {text}")
            raise StreamProtocolError(f"lighter: {text}")
        if kind != "update/account_all_trades":
            return []  # connected, pong, subscription snapshots
        channel = str(msg.get("channel") or "")
        _, _, index = channel.partition(":")
        if index and index.isdigit() and int(index) not in self.indexes:
            return []
        trades = msg.get("trades") or {}
        if not any(trades.get(market) for market in trades):
            return []
        return [StreamEvent(nudge=True, ref=channel or None)]


__all__ = [
    "API_URL",
    "EXPLORER_URL",
    "RATE_BUDGETS",
    "WS_URL",
    "WS_USERS_PER_IP",
    "LighterConnector",
    "LighterMarketData",
    "LighterTradeStream",
    "apply_trade",
    "claim_message",
    "copyable",
    "encode_claim_proof",
    "funding_payment",
    "recover_signer",
]
