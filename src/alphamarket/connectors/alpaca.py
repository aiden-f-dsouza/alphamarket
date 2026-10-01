"""Alpaca connector (US stocks/ETFs, long only) over a read-only OAuth2 grant.

Auth: `Authorization: Bearer <access token>` from Alpaca's OAuth2 flow
(oauth.py). Koine requests NO scope: Alpaca's scopes are `account:write`,
`trading` and `data`, and "read-only endpoint access is assumed by default"
(docs.alpaca.markets/us/docs/using-oauth2-and-trading-api, checked
2026-09-26). A grant that includes a write scope is rejected.

Market data (bars, latest quotes, corporate actions, assets) does NOT use
the user's token: the `data` scope would be needed for that, and prices are
not per-user anyway. AlpacaMarketData authenticates with Koine's own Alpaca
API key pair (APCA-API-KEY-ID / APCA-API-SECRET-KEY) from settings.

Payload shapes come from the official docs' examples (no Alpaca account
exists yet); see tests/fixtures/alpaca/README.md. What matters for the ledger:

  /v2/account              cash (can be negative on margin), equity -> reconciled
  /v2/positions            qty (fractional, up to 9 dp), avg_entry_price, current_price
  /v2/account/activities   FILL rows (id "<ts>::<uuid>", qty, price, side,
                           transaction_time) and non-trade rows (NTA: net_amount,
                           date, symbol, qty, status). `id` is stable and unique
                           per account and doubles as the page_token. Default
                           direction is desc; we always ask for asc.

Precision: shares at 9 dp (Alpaca fractional shares), USD at 6 dp (micros).
A fill's FILL_QUOTE leg is qty x price rounded half-even to 1e-6 USD; that
is exact for every whole-share fill at a cent price and within half a micro
otherwise. Alpaca's own cash rounding of fractional fills is [VERIFY] against
a real account; reconciliation's tolerance absorbs a sub-cent residue.

Regulatory fees (SEC fee, FINRA TAF) are NOT on the FILL row: Alpaca books
them as separate FEE activities, usually the next day. So fills carry no FEE
leg and the FEE event carries it.

Unsupported activity (short sales, options, crypto, mergers, stock
dividends, symbol changes, canceled rows...) is skipped from the pages and
kept in `unsupported_activities()` so the ledger can flag the account.
"""

import json
import logging
import re
import secrets
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_HALF_EVEN, Decimal
from email.utils import parsedate_to_datetime
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    CorporateAction,
    CorporateActionKind,
    CredentialExpired,
    DailyClose,
    InstrumentType,
    LegKind,
    NormalizedAssetFill,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    Page,
    Quote,
    RateLimited,
    ReadOnlyEvidence,
    ReadOnlyProof,
)
from alphamarket.connectors.base import UnsupportedActivity as _BaseUnsupportedActivity
from alphamarket.money import to_atoms, to_micros

logger = logging.getLogger(__name__)

# Trading API hosts per environment (docs server list, checked 2026-09-26).
ALPACA_TRADING_URLS = {
    "paper": "https://paper-api.alpaca.markets",
    "live": "https://api.alpaca.markets",
}
ALPACA_DATA_URL = "https://data.alpaca.markets"
# OAuth (one app registration serves both envs; `env` picks the account).
ALPACA_OAUTH_AUTHORIZE_URL = "https://app.alpaca.markets/oauth/authorize"
ALPACA_OAUTH_TOKEN_URL = "https://api.alpaca.markets/oauth/token"

# OAuth scopes that can change account state. Anything else ("data", or no
# scope at all) is read-only per Alpaca's OAuth docs.
WRITE_SCOPES = frozenset({"account:write", "trading"})

SHARE_DECIMALS = 9  # Alpaca fractional shares
USD_DECIMALS = 6  # micros
PAGE_SIZE = 100  # activities: default and maximum without `date`
QUOTE = "USD"

_USD_QUANTUM = Decimal(1).scaleb(-USD_DECIMALS)
_NEW_YORK = ZoneInfo("America/New_York")

# OCC option symbol: root (1-6 letters) + YYMMDD + C/P + strike x1000 (8 digits).
_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
# Alpaca crypto pairs: "BTC/USD" (current) or "BTCUSD" (legacy). US equity
# tickers are at most 5 letters plus an optional class suffix ("BRK.B").
_CRYPTO_SYMBOL = re.compile(r"^[A-Z0-9]{2,10}/?(USD|USDT|USDC|BTC)$")


# ---- activity classification ---------------------------------------------------

CASH_TRANSFER_TYPES = frozenset({"CSD", "CSW", "TRANS"})  # deposits / withdrawals
CASH_JOURNAL_TYPES = frozenset({"JNL", "JNLC", "ACATC"})  # cash in/out of this account
SHARE_TRANSFER_TYPES = frozenset({"JNLS", "ACATS"})  # shares in/out of this account
DIVIDEND_TYPES = frozenset({"DIV", "DIVCGL", "DIVCGS", "DIVROC", "DIVTXEX", "CGD"})
TAX_TYPES = frozenset({"DIVNRA", "DIVTW", "DIVFT", "INTNRA", "INTTW"})
INTEREST_TYPES = frozenset({"INT"})
FEE_TYPES = frozenset({"FEE", "DIVFEE", "PTC"})
REBATE_TYPES = frozenset({"PTR"})
SPLIT_TYPES = frozenset({"SSP", "SPLIT"})
OPTION_TYPES = frozenset({"OPASN", "OPEXP", "OPXRC", "OPEXC", "OPTRD", "OPCA", "OPCSH"})
CRYPTO_TYPES = frozenset({"CFEE", "OCT"})


# Moved to the contract in wave2 (connectors.base); the name stays importable here.
UnsupportedActivity = _BaseUnsupportedActivity


# ---- parsing helpers -------------------------------------------------------------


def _loads(resp: httpx.Response) -> Any:
    """JSON with numbers kept as exact Decimals (market data sends floats)."""
    return json.loads(resp.text, parse_float=Decimal)


def parse_ts(value: str) -> datetime:
    """RFC 3339 -> aware UTC. Alpaca prints up to nanoseconds; Python keeps
    microseconds, so extra fraction digits are truncated (never rounded up)."""
    text = value.strip().replace("Z", "+00:00")
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)(\.\d+)?(.*)$", text)
    if m and m.group(2) and len(m.group(2)) > 7:
        text = m.group(1) + m.group(2)[:7] + m.group(3)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def activity_time(row: dict[str, Any]) -> datetime:
    """FILL rows carry `transaction_time`. Non-trade rows carry `date`, which
    is date-only ("2019-08-01") in every documented example: contract rule,
    23:59:59 UTC of that date. A full timestamp is used as given."""
    if row.get("transaction_time"):
        return parse_ts(str(row["transaction_time"]))
    raw = str(row.get("date") or row.get("created_at") or "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.combine(date.fromisoformat(raw), dtime(23, 59, 59), UTC)
    if raw:
        return parse_ts(raw)
    raise ValueError(f"activity {row.get('id')!r} has no timestamp")


def usd_atoms(value: str | Decimal) -> int:
    """A USD amount the venue reports (cash, net_amount) at 6 dp, exactly."""
    return to_atoms(str(value), USD_DECIMALS)


def share_atoms(value: str | Decimal) -> int:
    return to_atoms(str(value), SHARE_DECIMALS)


def fill_notional_atoms(qty: str, price: str) -> int:
    """qty x price in USD atoms (6 dp), rounded half-even.
    E.g. 2 x 174.78 = 349.56 -> 349_560_000 (exact);
    0.123456789 x 174.78 = 21.57777758... -> 21_577_778."""
    product = Decimal(qty) * Decimal(price)
    return int(product.quantize(_USD_QUANTUM, rounding=ROUND_HALF_EVEN).scaleb(USD_DECIMALS))


def equity_instrument(symbol: str) -> NormalizedInstrument:
    return NormalizedInstrument(
        symbol=symbol,
        type=InstrumentType.EQUITY,
        base_asset=symbol,
        quote_asset=QUOTE,
        qty_decimals=SHARE_DECIMALS,
    )


def asset_code(symbol: str, asset_class: str | None = None) -> str:
    """Canonical code for a position: the ticker for equities, the base coin
    for crypto pairs ("BTC/USD", "BTCUSD" -> "BTC")."""
    symbol = symbol.upper()
    if asset_class == "crypto" or (asset_class is None and "/" in symbol):
        m = _CRYPTO_SYMBOL.match(symbol)
        if m:
            return symbol[: -len(m.group(1))].rstrip("/")
    return symbol


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait after a 429: Retry-After (seconds or HTTP date), else
    X-RateLimit-Reset (unix epoch), else a minute (the budget window)."""
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return max(float(header), 0.0)
        except ValueError:
            try:
                return max((parsedate_to_datetime(header) - datetime.now(UTC)).total_seconds(), 0)
            except (TypeError, ValueError):
                pass
    reset = resp.headers.get("X-RateLimit-Reset")
    if reset:
        try:
            return max(float(reset) - time.time(), 0.0)
        except ValueError:
            pass
    return 60.0


class AlpacaError(RuntimeError):
    """Non-2xx answer from Alpaca. Carries status and Alpaca's message only."""

    def __init__(self, status: int, message: str, endpoint: str) -> None:
        super().__init__(f"alpaca {endpoint}: HTTP {status}: {message[:200]}")
        self.status = status
        self.message = message


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return str(body.get("message") or body.get("error") or body)[:200]
    return str(body)[:200]


# ---- the account connector -------------------------------------------------------


class AlpacaConnector(AssetVenueConnector):
    venue = "alpaca"

    def __init__(
        self,
        env: str,
        access_token: str,
        account_id: str = "",
        granted_scopes: tuple[str, ...] | None = None,
        base_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if env not in ALPACA_TRADING_URLS:
            raise ValueError(f"alpaca has no {env!r} environment")
        if not access_token:
            raise ValueError("alpaca needs an OAuth access token")
        self.env = env
        self.base_url = (base_url or ALPACA_TRADING_URLS[env]).rstrip("/")
        self._token = access_token
        self.account_id = account_id
        # The `scope` string Alpaca returned with the token, split. None when
        # the stored credential predates scope recording (unknown).
        self.granted_scopes = granted_scopes
        self.client = client or httpx.Client(timeout=15.0)
        self._unsupported: dict[str, UnsupportedActivity] = {}

    def __repr__(self) -> str:  # never show the token
        return f"AlpacaConnector(env={self.env!r}, account_id={self.account_id!r})"

    # ---- transport -------------------------------------------------------

    def _send(
        self,
        endpoint: str,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        self.throttle(endpoint)
        resp = self.client.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=body,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        if resp.status_code in (429, 418):
            raise RateLimited(_retry_after(resp), endpoint)
        return resp

    def _get(self, endpoint: str, path: str, params: dict[str, Any] | None = None) -> Any:
        resp = self._send(endpoint, "GET", path, params)
        if resp.status_code == 401:
            raise CredentialExpired("alpaca rejected the OAuth token (revoked or expired)")
        if resp.status_code >= 400:
            raise AlpacaError(resp.status_code, _error_message(resp), endpoint)
        return _loads(resp)

    # ---- account ---------------------------------------------------------

    def fetch_account(self) -> dict[str, Any]:
        account = self._get("Account", "/v2/account")
        if account.get("id"):
            self.account_id = str(account["id"])
        return account

    def fetch_account_id(self) -> str:
        """The Alpaca account uuid this token reads (CredentialKind.OAUTH2 key_id)."""
        return str(self.fetch_account()["id"])

    def fetch_balances(self) -> NormalizedBalances:
        account = self.fetch_account()
        as_of = datetime.now(UTC)
        positions = self._get("Positions", "/v2/positions")
        currency = str(account.get("currency") or QUOTE)
        if currency != QUOTE:
            raise ValueError(f"alpaca account currency {currency!r} is not USD")
        assets = [AssetAmount(asset=QUOTE, atoms=usd_atoms(account["cash"]), decimals=USD_DECIMALS)]
        by_code: dict[str, AssetAmount] = {}
        for row in positions:
            code = asset_code(str(row["symbol"]), row.get("asset_class"))
            atoms = share_atoms(row["qty"])
            if code in by_code:  # two rows for one symbol: merge, keep the first prices
                prev = by_code[code]
                by_code[code] = prev.model_copy(update={"atoms": prev.atoms + atoms})
                continue
            by_code[code] = AssetAmount(
                asset=code,
                atoms=atoms,
                decimals=SHARE_DECIMALS,
                entry_price=_str_or_none(row.get("avg_entry_price")),
                mark_price=_str_or_none(row.get("current_price")),
            )
        assets.extend(by_code.values())
        return NormalizedBalances(
            as_of=as_of,
            assets=assets,
            equity_usd_micros=to_micros(str(account["equity"])),
        )

    # ---- activities ------------------------------------------------------

    def _activities_page(
        self, params: dict[str, Any], cursor: str | None, since: datetime | None
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        """One request. Returns (rows, next_cursor, resume_cursor) per the
        Page rules: page_token = id of the last row seen; cursor beats since."""
        params = {**params, "direction": "asc", "page_size": PAGE_SIZE}
        if cursor is not None:
            params["page_token"] = cursor
        elif since is not None:
            params["after"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = self._get("Activities", "/v2/account/activities", params)
        if not isinstance(rows, list):
            raise ValueError("unexpected activities payload")
        last = str(rows[-1]["id"]) if rows else cursor
        if len(rows) >= PAGE_SIZE:
            return rows, last, None
        return rows, None, last

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        rows, next_cursor, resume = self._activities_page({"activity_types": "FILL"}, cursor, since)
        items: list[NormalizedAssetFill] = []
        for row in rows:
            mapped = map_fill(row)
            if isinstance(mapped, UnsupportedActivity):
                self._note_unsupported(mapped)
            else:
                items.append(mapped)
        return Page(items=items, next_cursor=next_cursor, resume_cursor=resume)

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        rows, next_cursor, resume = self._activities_page(
            {"category": "non_trade_activity"}, cursor, since
        )
        items: list[NormalizedEvent] = []
        for row in rows:
            mapped = map_non_trade(row)
            if isinstance(mapped, UnsupportedActivity):
                self._note_unsupported(mapped)
            else:
                items.append(mapped)
        return Page(items=items, next_cursor=next_cursor, resume_cursor=resume)

    def _note_unsupported(self, item: UnsupportedActivity) -> None:
        if item.venue_ref not in self._unsupported:
            logger.info(
                "alpaca: skipping unsupported %s activity %s (%s)",
                item.activity_type,
                item.venue_ref,
                item.reason,
            )
        self._unsupported[item.venue_ref] = item

    def unsupported_activities(self, clear: bool = True) -> list[UnsupportedActivity]:
        """Activities skipped by the page methods since the last call (oldest
        first). A page is fetched once per cursor, so the caller must persist
        these after each page, the same way it persists the cursor."""
        items = sorted(self._unsupported.values(), key=lambda a: (a.occurred_at, a.venue_ref))
        if clear:
            self._unsupported.clear()
        return items

    def drain_unsupported(self) -> list[UnsupportedActivity]:
        """Contract hook (wave2): the page runner persists these per page."""
        return self.unsupported_activities(clear=True)

    # ---- read-only proof -------------------------------------------------

    # Sent on PAPER only. It can never execute: the quantity is zero (Alpaca
    # requires qty > 0), the symbol is not a listed asset, and paper accounts
    # hold no real money. A token without the `trading` scope is refused
    # before validation (401/403); a token with it gets a 422 validation
    # error, which is how we learn it could trade.
    PROBE_SYMBOL = "KOINEPROBE0"

    def probe_order_body(self) -> dict[str, str]:
        return {
            "symbol": self.PROBE_SYMBOL,
            "qty": "0",
            "side": "buy",
            "type": "limit",
            "limit_price": "0.01",
            "time_in_force": "day",
            "client_order_id": f"koine-readonly-probe-{secrets.token_hex(6)}",
        }

    def prove_read_only(self) -> ReadOnlyEvidence:
        """ORDER_PROBE plus the granted scope list.

        1. The scope list Alpaca returned with the token: any write scope
           means writable, whatever else happens.
        2. GET /v2/account first, so a 401 on the probe cannot be a dead token.
        3. Paper only: POST the impossible order above. 401/403 (other than
           a buying-power refusal) = refused for lack of scope = read-only;
           400/404/422 or a buying-power 403 = the request got past
           authorization = writable. Live accounts are never probed; the
           scope list alone decides there."""
        now = datetime.now(UTC)
        scopes = self.granted_scopes
        write_granted = sorted(WRITE_SCOPES & set(scopes)) if scopes is not None else []
        details: dict[str, str | bool | int | None] = {
            "env": self.env,
            "granted_scopes": " ".join(scopes) if scopes is not None else None,
            "write_scopes_granted": " ".join(write_granted) or None,
        }
        if write_granted:
            details["probe"] = "skipped: write scope granted"
            return ReadOnlyEvidence(
                method=ReadOnlyProof.ORDER_PROBE, read_only=False, checked_at=now, details=details
            )

        account = self.fetch_account()
        trading_blocked = bool(account.get("trading_blocked") or account.get("account_blocked"))
        details["account_status"] = _str_or_none(account.get("status"))
        details["trading_blocked"] = trading_blocked

        if self.env != "paper":
            details["probe"] = "skipped: live account (never probed)"
            read_only = None if scopes is None else True
            return ReadOnlyEvidence(
                method=ReadOnlyProof.ORDER_PROBE,
                read_only=read_only,
                checked_at=now,
                details=details,
            )

        resp = self._send("OrderProbe", "POST", "/v2/orders", body=self.probe_order_body())
        message = _error_message(resp) if resp.status_code >= 400 else ""
        details["probe"] = "POST /v2/orders qty=0 symbol=KOINEPROBE0 (paper)"
        details["probe_status"] = resp.status_code
        details["probe_message"] = message or None
        if resp.is_success:
            # Cannot happen for a zero-quantity order on an unknown symbol.
            logger.error("alpaca read-only probe was ACCEPTED (status %s)", resp.status_code)
            details["order_accepted"] = True
            read_only = False
        elif resp.status_code in (400, 404, 422):
            read_only = False  # validated: the token may place orders
        elif resp.status_code == 403 and _is_business_refusal(message):
            read_only = False  # past authorization, refused on buying power
        elif resp.status_code in (401, 403):
            # Refused before validation. If the account itself cannot trade,
            # the refusal says nothing about the token: undetermined.
            read_only = None if trading_blocked else True
        else:
            read_only = None  # 5xx etc.: ask again tomorrow
        return ReadOnlyEvidence(
            method=ReadOnlyProof.ORDER_PROBE, read_only=read_only, checked_at=now, details=details
        )

    def check_write_scope(self) -> bool | None:
        evidence = self.prove_read_only()
        return None if evidence.read_only is None else not evidence.read_only


def _is_business_refusal(message: str) -> bool:
    text = message.lower()
    return "buying power" in text or "insufficient" in text


def _str_or_none(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


# ---- row mapping (pure functions, unit tested with hand-computed atoms) ----------


def _unsupported(row: dict[str, Any], reason: str) -> UnsupportedActivity:
    return UnsupportedActivity(
        venue_ref=str(row["id"]),
        activity_type=str(row.get("activity_type") or ""),
        activity_sub_type=_str_or_none(row.get("activity_sub_type")),
        symbol=_str_or_none(row.get("symbol")),
        occurred_at=activity_time(row),
        reason=reason,
        description=_str_or_none(row.get("description")),
    )


def _symbol_kind(symbol: str) -> str:
    if _OPTION_SYMBOL.match(symbol):
        return "option"
    if "/" in symbol or (len(symbol) >= 6 and _CRYPTO_SYMBOL.match(symbol)):
        return "crypto"
    return "equity"


def map_fill(row: dict[str, Any]) -> NormalizedAssetFill | UnsupportedActivity:
    """FILL -> FILL_BASE +/-qty shares (9 dp) and FILL_QUOTE -/+ qty x price USD (6 dp).
    Short sales ("sell_short"), options and crypto are unsupported."""
    symbol = str(row["symbol"]).upper()
    side = str(row.get("side") or "").lower()
    kind = _symbol_kind(symbol)
    if kind != "equity":
        return _unsupported(row, kind)
    if side == "sell_short":
        return _unsupported(row, "short_sale")
    if side not in ("buy", "sell"):
        return _unsupported(row, "unsupported_type")
    qty = str(row["qty"])
    price = str(row["price"])
    qty_atoms = share_atoms(qty)
    notional = fill_notional_atoms(qty, price)
    buy = side == "buy"
    legs = [
        NormalizedLeg(
            asset=symbol,
            delta_atoms=qty_atoms if buy else -qty_atoms,
            decimals=SHARE_DECIMALS,
            kind=LegKind.FILL_BASE,
            instrument_symbol=symbol,
        ),
        NormalizedLeg(
            asset=QUOTE,
            delta_atoms=-notional if buy else notional,
            decimals=USD_DECIMALS,
            kind=LegKind.FILL_QUOTE,
            instrument_symbol=symbol,
        ),
    ]
    return NormalizedAssetFill(
        venue_fill_id=str(row["id"]),
        order_id=_str_or_none(row.get("order_id")),
        instrument=equity_instrument(symbol),
        action=Action.BUY if buy else Action.SELL,
        qty_atoms=qty_atoms,
        price=price,
        legs=legs,
        filled_at=activity_time(row),
    )


def _cash_event(row: dict[str, Any], kind: LegKind, amount: int) -> NormalizedEvent:
    symbol = _str_or_none(row.get("symbol"))
    return NormalizedEvent(
        venue_ref=str(row["id"]),
        kind=kind,
        legs=[
            NormalizedLeg(
                asset=QUOTE,
                delta_atoms=amount,
                decimals=USD_DECIMALS,
                kind=kind,
                instrument_symbol=symbol.upper() if symbol else None,
            )
        ],
        occurred_at=activity_time(row),
        description=_description(row),
    )


def _description(row: dict[str, Any]) -> str:
    parts = [str(row.get("activity_type") or "")]
    if row.get("activity_sub_type"):
        parts.append(str(row["activity_sub_type"]))
    if row.get("description"):
        parts.append(str(row["description"]))
    return " ".join(parts)


def _share_event(row: dict[str, Any], kind: LegKind, qty_atoms: int, cash: int) -> NormalizedEvent:
    symbol = str(row["symbol"]).upper()
    legs = [
        NormalizedLeg(
            asset=symbol,
            delta_atoms=qty_atoms,
            decimals=SHARE_DECIMALS,
            kind=kind,
            instrument_symbol=symbol,
        )
    ]
    if cash:  # e.g. cash in lieu of a fractional share on a split
        legs.append(
            NormalizedLeg(
                asset=QUOTE,
                delta_atoms=cash,
                decimals=USD_DECIMALS,
                kind=kind,
                instrument_symbol=symbol,
            )
        )
    return NormalizedEvent(
        venue_ref=str(row["id"]),
        kind=kind,
        legs=legs,
        occurred_at=activity_time(row),
        description=_description(row),
    )


def map_non_trade(row: dict[str, Any]) -> NormalizedEvent | UnsupportedActivity:
    """One non-trade activity -> one event (see the mapping table in
    docs/venues/alpaca.md), or UnsupportedActivity. Amounts are the venue's
    signed `net_amount` at 6 dp; share quantities its signed `qty` at 9 dp."""
    kind_code = str(row.get("activity_type") or "").upper()
    sub_type = str(row.get("activity_sub_type") or "").upper()
    status = str(row.get("status") or "executed").lower()
    symbol = str(row.get("symbol") or "").upper()

    if status == "canceled":
        return _unsupported(row, "canceled")
    if kind_code in OPTION_TYPES or (symbol and _symbol_kind(symbol) == "option"):
        return _unsupported(row, "option")
    if kind_code in CRYPTO_TYPES or (symbol and _symbol_kind(symbol) == "crypto"):
        return _unsupported(row, "crypto")

    net = usd_atoms(row["net_amount"]) if row.get("net_amount") not in (None, "") else 0

    if kind_code in CASH_TRANSFER_TYPES:
        deposit = net > 0 or (net == 0 and kind_code == "CSD")
        return _cash_event(row, LegKind.DEPOSIT if deposit else LegKind.WITHDRAWAL, net)
    if kind_code in SHARE_TRANSFER_TYPES or (
        kind_code == "JNL" and symbol and row.get("qty") not in (None, "") and net == 0
    ):
        if not symbol or row.get("qty") in (None, ""):
            return _unsupported(row, "unsupported_type")
        qty = share_atoms(row["qty"])
        kind = LegKind.TRANSFER_IN if qty > 0 else LegKind.TRANSFER_OUT
        return _share_event(row, kind, qty, net)
    if kind_code in CASH_JOURNAL_TYPES:
        return _cash_event(row, LegKind.TRANSFER_IN if net > 0 else LegKind.TRANSFER_OUT, net)
    if kind_code in DIVIDEND_TYPES:
        if sub_type == "SDIV":
            return _unsupported(row, "stock_dividend")
        return _cash_event(row, LegKind.DIVIDEND, net)
    if kind_code in TAX_TYPES:
        return _cash_event(row, LegKind.TAX, net)
    if kind_code in INTEREST_TYPES:
        return _cash_event(row, LegKind.INTEREST, net)
    if kind_code in FEE_TYPES:
        # A FEE leg is always negative; a positive "fee" is a refund.
        return _cash_event(row, LegKind.FEE if net <= 0 else LegKind.ADJUSTMENT, net)
    if kind_code in REBATE_TYPES:
        return _cash_event(row, LegKind.ADJUSTMENT, net)
    if kind_code in SPLIT_TYPES:
        # qty is the signed change in shares [VERIFY against a real split:
        # the paper environment cannot simulate one]. Two rows (old shares
        # out, new shares in) also sum correctly under that reading.
        if not symbol or row.get("qty") in (None, ""):
            return _unsupported(row, "unsupported_type")
        return _share_event(row, LegKind.SPLIT, share_atoms(row["qty"]), net)
    # MA, NC, SC, REORG, REO, SSO, SPIN, FOPT, MISC and anything new.
    return _unsupported(row, "unsupported_type")


# ---- market data (Koine's own Alpaca key; public prices, not user data) ----------


class AlpacaMarketData(AssetMarketData):
    """Daily bars, latest quotes, corporate actions and the asset list.

    Authenticated with Koine's own API key pair, never a user's token. Bars
    are requested with adjustment=raw: the ledger applies splits as SPLIT
    legs, so closes must be in the same (unadjusted) share units."""

    venue = "alpaca"

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        data_url: str = ALPACA_DATA_URL,
        trading_url: str = ALPACA_TRADING_URLS["paper"],
        bars_feed: str = "sip",  # historical SIP older than 15 min is on the free plan [VERIFY]
        quote_feed: str = "iex",  # latest SIP quotes need a paid plan
        client: httpx.Client | None = None,
    ) -> None:
        self.data_url = data_url.rstrip("/")
        self.trading_url = trading_url.rstrip("/")
        self._key_id = key_id
        self._secret = secret_key
        self.bars_feed = bars_feed
        self.quote_feed = quote_feed
        self.client = client or httpx.Client(timeout=15.0)

    def __repr__(self) -> str:
        return f"AlpacaMarketData(data_url={self.data_url!r})"

    def _get(self, endpoint: str, url: str, params: dict[str, Any] | None = None) -> Any:
        if not self._key_id or not self._secret:
            raise RuntimeError("ALPACA_DATA_KEY_ID / ALPACA_DATA_SECRET_KEY are not set")
        self.throttle(endpoint)
        resp = self.client.get(
            url,
            params=params,
            headers={"APCA-API-KEY-ID": self._key_id, "APCA-API-SECRET-KEY": self._secret},
        )
        if resp.status_code in (429, 418):
            raise RateLimited(_retry_after(resp), endpoint)
        if resp.status_code >= 400:
            raise AlpacaError(resp.status_code, _error_message(resp), endpoint)
        return _loads(resp)

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        """Active, tradable US equities. Fractionable assets trade in 1e-9
        share steps with a $1 minimum order; the rest in whole shares."""
        rows = self._get(
            "Assets",
            f"{self.trading_url}/v2/assets",
            {"status": "active", "asset_class": "us_equity"},
        )
        out: list[NormalizedInstrument] = []
        for row in rows:
            if not row.get("tradable"):
                continue
            symbol = str(row["symbol"]).upper()
            fractionable = bool(row.get("fractionable"))
            whole_share = 10**SHARE_DECIMALS
            out.append(
                NormalizedInstrument(
                    symbol=symbol,
                    type=InstrumentType.EQUITY,
                    base_asset=symbol,
                    quote_asset=QUOTE,
                    qty_decimals=SHARE_DECIMALS,
                    qty_step_atoms=1 if fractionable else whole_share,
                    min_qty_atoms=1 if fractionable else whole_share,
                    min_notional="1" if fractionable else None,
                )
            )
        return out

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """1Day bars, raw. A daily bar is stamped at 00:00 New York time
        (04:00/05:00Z); its trading day is that New York date."""
        out: list[DailyClose] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "symbols": symbol,
                "timeframe": "1Day",
                "start": start.isoformat(),
                # One day past `end`: a date-only `end` may be read as 00:00Z,
                # which is before the end day's bar (stamped 04:00/05:00Z) and
                # would drop it [VERIFY]. The New York date filter below keeps
                # the result to [start, end] either way.
                "end": (end + timedelta(days=1)).isoformat(),
                "adjustment": "raw",
                "feed": self.bars_feed,
                "limit": 10000,
                "sort": "asc",
            }
            if page_token:
                params["page_token"] = page_token
            body = self._get("Bars", f"{self.data_url}/v2/stocks/bars", params)
            for bar in (body.get("bars") or {}).get(symbol, []) or []:
                day = parse_ts(str(bar["t"])).astimezone(_NEW_YORK).date()
                if start <= day <= end:
                    out.append(DailyClose(symbol=symbol, day=day, close=_decimal_str(bar["c"])))
            page_token = body.get("next_page_token")
            if not page_token:
                break
        out.sort(key=lambda c: c.day)
        return out

    def fetch_quote(self, symbol: str) -> Quote | None:
        body = self._get(
            "LatestQuote",
            f"{self.data_url}/v2/stocks/quotes/latest",
            {"symbols": symbol, "feed": self.quote_feed},
        )
        row = (body.get("quotes") or {}).get(symbol)
        if not row:
            return None
        return Quote(
            symbol=symbol,
            bid=_positive_or_none(row.get("bp")),
            ask=_positive_or_none(row.get("ap")),
            last=None,
            as_of=parse_ts(str(row["t"])),
        )

    CORPORATE_ACTION_TYPES = "forward_split,reverse_split,cash_dividend,name_change"

    def fetch_corporate_actions(
        self, start: date, end: date, symbols: list[str] | None = None
    ) -> list[CorporateAction]:
        """Splits (forward and reverse), cash dividends and symbol changes in
        [start, end] by process date. Unit splits, mergers, spin-offs and
        stock dividends are not modelled yet (their activities are flagged
        unsupported on the account side)."""
        out: list[CorporateAction] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "types": self.CORPORATE_ACTION_TYPES,
                "limit": 1000,
            }
            if symbols:
                params["symbols"] = ",".join(symbols)
            if page_token:
                params["page_token"] = page_token
            body = self._get("CorporateActions", f"{self.data_url}/v1/corporate-actions", params)
            actions = body.get("corporate_actions") or {}
            for key in ("forward_splits", "reverse_splits"):
                for row in actions.get(key) or []:
                    new, old = split_ratio(row["new_rate"], row["old_rate"])
                    out.append(
                        CorporateAction(
                            symbol=str(row["symbol"]),
                            kind=CorporateActionKind.SPLIT,
                            ex_date=date.fromisoformat(str(row["ex_date"])),
                            pay_date=_date_or_none(row.get("payable_date")),
                            ratio_new=new,
                            ratio_old=old,
                        )
                    )
            for row in actions.get("cash_dividends") or []:
                out.append(
                    CorporateAction(
                        symbol=str(row["symbol"]),
                        kind=CorporateActionKind.CASH_DIVIDEND,
                        ex_date=date.fromisoformat(str(row["ex_date"])),
                        pay_date=_date_or_none(row.get("payable_date")),
                        cash_per_share=_decimal_str(row["rate"]),
                    )
                )
            for row in actions.get("name_changes") or []:
                out.append(
                    CorporateAction(
                        symbol=str(row["old_symbol"]),
                        kind=CorporateActionKind.SYMBOL_CHANGE,
                        ex_date=date.fromisoformat(str(row["process_date"])),
                        new_symbol=str(row["new_symbol"]),
                    )
                )
            page_token = body.get("next_page_token")
            if not page_token:
                break
        out.sort(key=lambda a: (a.ex_date, a.symbol, a.kind))
        return out


def split_ratio(new_rate: Any, old_rate: Any) -> tuple[int, int]:
    """Integer (new, old) shares from Alpaca's decimal rates, reduced:
    (2, 1) -> 2:1; (1, 50) -> 1:50; (1.5, 1) -> 3:2."""
    ratio = Fraction(Decimal(str(new_rate))) / Fraction(Decimal(str(old_rate)))
    return ratio.numerator, ratio.denominator


def _decimal_str(value: Any) -> str:
    """A price as a plain decimal string ("178.08", never "1.7808E+2")."""
    return format(Decimal(str(value)), "f")


def _positive_or_none(value: Any) -> str | None:
    if value is None:
        return None
    dec = Decimal(str(value))
    return format(dec, "f") if dec > 0 else None


def _date_or_none(value: Any) -> date | None:
    return date.fromisoformat(str(value)) if value else None


def parse_token_secret(secret: str) -> tuple[str, tuple[str, ...] | None]:
    """The vault holds the OAuth token as JSON {"access_token", "scope", ...}
    (oauth.py); a bare token string is accepted too (scope then unknown)."""
    text = secret.strip()
    if text.startswith("{"):
        data = json.loads(text)
        token = str(data.get("access_token") or "")
        scope = data.get("scope")
        scopes = tuple(str(scope).split()) if scope is not None else None
        return token, scopes
    return text, None
