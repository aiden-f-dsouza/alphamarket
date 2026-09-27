"""SnapTrade connector: the long-tail equities aggregator (Robinhood, Schwab,
Fidelity, Webull, Public, E*TRADE, Vanguard and 30+ others behind one API).

Auth / request signing -- reproduced from SnapTrade's own documented
algorithm (no numeric test vector is published; the worked example below is
computed by this module's own `sign()` so CI can assert against it):
    docs example https://docs.snaptrade.com/docs/request-signatures , checked
    2026-09-26 ("Build a signature payload containing the request body, path
    and query string ... sort object keys alphabetically at every level,
    remove unnecessary whitespace ... sign with HMAC-SHA256, base64-encode").
    Confirmed against the OpenAPI security schemes in
    https://github.com/passiv/snaptrade-api-docs/blob/main/docs/api.yaml :
    `Signature` is a request HEADER; `clientId`, `timestamp`, `userId` and
    `userSecret` are QUERY parameters (never the body).

    signature = base64(HMAC-SHA256(
        consumerKey,
        json.dumps({"content": body_or_None, "path": "/api/v1" + subpath,
                    "query": raw_query_string},
                   separators=(",", ":"), sort_keys=True).encode(),
    ))

`content` is the exact request body dict (None, never {}, when there is no
body); `path` always carries the "/api/v1" prefix even though it is also the
first segment of BASE_URL; `query` is the raw query string actually sent
(clientId & timestamp always, plus userId/userSecret on user-scoped calls,
plus any endpoint filters), built with `urlencode(sorted(...))` so the same
bytes are signed and sent.

Credentials (CredentialKind.AGGREGATOR): `key_id` = SnapTrade `userId`
(chosen by Koine, immutable), `secret` = SnapTrade `userSecret` (returned
once by `registerUser`, stored in the vault like any other secret --
invariant 4). `snaptrade_client_id` / `snaptrade_consumer_key` identify
*Koine* to SnapTrade (config.py) and are the same for every connected user.

Flows (docs example https://docs.snaptrade.com/reference , checked
2026-09-26):
  POST /snapTrade/registerUser        {userId} -> {userId, userSecret}
  POST /snapTrade/login               {broker?, connectionType: "read", ...}
                                       -> {redirectURI} (Connection Portal,
                                       5-minute link; `connectionType=read`
                                       is what proves the connection cannot
                                       trade -- ReadOnlyProof.AGGREGATOR_FLAG)
  GET  /accounts                      -> list of brokerage accounts (one
                                       `userId`+`userSecret` can span several;
                                       account_ref = SnapTrade account `id`)
  GET  /accounts/{id}/balances        -> per-currency cash vector
  GET  /accounts/{id}/holdings        -> positions + balances in one call
                                       (SnapTrade's per-endpoint
                                       positions/orders split is marked
                                       deprecated in favour of this one;
                                       used here for simplicity)
  GET  /accounts/{id}/activities      -> BUY, SELL, DIVIDEND, CONTRIBUTION,
                                       WITHDRAWAL, INTEREST, FEE, TAX, SPLIT,
                                       REI, STOCK_DIVIDEND, TRANSFER,
                                       EXTERNAL_ASSET_TRANSFER_IN/OUT,
                                       ADJUSTMENT, OPTIONEXPIRATION/
                                       ASSIGNMENT/EXERCISE, SUBSTITUTE_DIVIDEND
  GET  /authorizations/{id}           -> {type: "read"|"trade", disabled}:
                                       the read-only proof re-checked nightly
  DELETE /snapTrade/deleteUser        -> queues account deletion (each
                                       registered user costs Koine money --
                                       see docs/venues/snaptrade.md)

Activity ids are NOT stable ("docs warn it can change if the broker
reprocesses" -- tests/fixtures/snaptrade/README.md). So every fill and event
here uses `ref_is_content_hash=True` / a content-hash `venue_fill_id`
computed from the row's stable fields (type, dates, symbol, units, price,
amount, currency), never SnapTrade's own `id`. Two identical same-day
activities on one account collapse into one row: acceptable only on the
`daily_verified` tier (base.py NormalizedEvent docstring), and stated in
docs/venues/snaptrade.md.

Options activities (OPTIONEXPIRATION/ASSIGNMENT/EXERCISE) and
SUBSTITUTE_DIVIDEND (short-position stock-loan payments) are dropped: options
are out of scope this pass (InstrumentType.OPTION, "rejected at ingest") and
Koine does not model short positions. This is a known gap, not a bug --
see docs/venues/snaptrade.md.

Field precision: SnapTrade's JSON numbers (`units`, `price`,
`average_purchase_price`, `amount`, `fee`, `cash`) are plain JSON floats, not
decimal strings (unlike Kalshi/Polymarket). `str(float)` is Python's shortest
round-trip repr, which reproduces the digits SnapTrade's serializer printed
(same technique as polymarket.py's `_dec`), so every value is converted
through `str()` before `money.to_atoms` -- floats never reach a stored value
directly (invariant 1).
"""

import hashlib
import hmac as hmac_mod
import json
import re
import time
from base64 import b64encode
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from alphamarket.connectors.base import (
    Action,
    AssetMarketData,
    AssetVenueConnector,
    LegKind,
    NormalizedAssetFill,
    NormalizedBalances,
    NormalizedEvent,
    NormalizedInstrument,
    NormalizedLeg,
    Page,
    RateLimited,
    ReadOnlyEvidence,
    ReadOnlyProof,
)
from alphamarket.connectors.base import InstrumentType as InstType
from alphamarket.money import to_atoms

# docs example https://docs.snaptrade.com/ ; confirmed against the OpenAPI
# `servers:` entry in https://github.com/passiv/snaptrade-api-docs, checked
# 2026-09-26. SnapTrade has one production environment (no sandbox host of
# its own; SnapTrade's "sandbox" is a per-broker demo login inside the same
# API, not a different base URL).
BASE_URL = "https://api.snaptrade.com/api/v1"

PAGE_SIZE = 1000  # activities `limit` max, per the reference docs
CASH_DECIMALS = 6  # USD cash legs; matches money.MICROS_PER_UNIT's scale
QTY_DECIMALS = 6  # [VERIFY] SnapTrade does not document a fixed share
# precision; 6dp covers every fractional-share program seen in the research
# (Alpaca/Schwab go to thousandths-or-finer) with headroom, and to_atoms
# raises loudly instead of silently rounding if a real account ever reports
# finer than this.

FILL_TYPES = ("BUY", "SELL")
_UNSUPPORTED_TYPES = frozenset(
    {"OPTIONEXPIRATION", "OPTIONASSIGNMENT", "OPTIONEXERCISE", "SUBSTITUTE_DIVIDEND"}
)
EVENT_TYPES = (
    "DIVIDEND",
    "CONTRIBUTION",
    "WITHDRAWAL",
    "INTEREST",
    "FEE",
    "TAX",
    "SPLIT",
    "REI",
    "STOCK_DIVIDEND",
    "TRANSFER",
    "EXTERNAL_ASSET_TRANSFER_IN",
    "EXTERNAL_ASSET_TRANSFER_OUT",
    "ADJUSTMENT",
)


class SnapTradeUserExists(Exception):
    """registerUser called for a userId that already exists at SnapTrade."""


def _dec(value: Any) -> Decimal:
    """SnapTrade serializes numbers as JSON floats; `str()` is the shortest
    round-trip text, i.e. the digits actually printed (see module docstring).
    `None` (a null cash/fee field) is treated as zero."""
    if value is None:
        return Decimal(0)
    if isinstance(value, bool):
        raise TypeError("bool is not a quantity")
    if isinstance(value, float | int):
        return Decimal(str(value))
    return Decimal(value)


def _atoms(value: Any, decimals: int) -> int:
    """Strict conversion for a value the venue reported directly (raises
    loudly, per money.to_atoms, if it carries finer precision than
    `decimals` -- that would mean the assumed scale needs revisiting)."""
    return to_atoms(str(_dec(value)), decimals)


def _round_atoms(value: Decimal, decimals: int) -> int:
    """Rounding conversion for a value THIS module derives by arithmetic
    (price x units, or a sum of such products): Decimal multiplication of
    two venue-reported numbers routinely carries more digits than
    `decimals`, and unlike a raw venue field that is not a sign of a scale
    bug (same reasoning as polymarket.py's `micros()`, which rounds
    half-even instead of raising for exactly this reason)."""
    return int((value * (10**decimals)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _date_only(value: str) -> datetime:
    """SnapTrade activities carry date-only `trade_date`/`settlement_date`
    ("2026-09-10" or a midnight ISO timestamp) on this daily_resolution
    venue. base.py's date-only rule: 23:59:59 UTC of that day, so every
    consumer assigns the row to the same day regardless of local ingest time."""
    day = date.fromisoformat(value[:10])
    return datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=UTC)


def content_hash(*parts: Any) -> str:
    """SHA-256 hex digest of the row's stable fields. SnapTrade's own
    activity `id` can change when the broker reprocesses a transaction
    (docs example https://docs.snaptrade.com/reference/Account%20Information/
    AccountInformation_getAccountActivities , checked 2026-09-26), so
    identity has to come from content instead -- NormalizedEvent.ref_is_
    content_hash=True documents exactly this case."""
    text = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(text.encode()).hexdigest()


def sign(consumer_key: str, path: str, query: str, body: dict[str, Any] | None) -> str:
    """SnapTrade's documented signing algorithm, reproduced exactly (module
    docstring). `path` already carries the "/api/v1" prefix; `query` is the
    raw query string, in the exact bytes that will be sent on the wire."""
    sig_object = {"content": body if body else None, "path": path, "query": query}
    sig_content = json.dumps(sig_object, separators=(",", ":"), sort_keys=True)
    digest = hmac_mod.new(consumer_key.encode(), sig_content.encode(), hashlib.sha256).digest()
    return b64encode(digest).decode()


class SnapTradeConnector(AssetVenueConnector):
    """One instance per registered SnapTrade user; `account_ref` picks one of
    that user's (possibly several) connected brokerage accounts, chosen by
    the connect flow via `list_accounts()`."""

    venue = "snaptrade"
    MIN_INTERVAL = 0.05
    MAX_ATTEMPTS = 4

    def __init__(
        self,
        client_id: str,
        consumer_key: str,
        user_id: str,
        user_secret: str | None,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id
        self.consumer_key = consumer_key
        self.user_id = user_id
        self.user_secret = user_secret
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=20.0)
        self._last_request_at = 0.0

    # ---- signed transport ------------------------------------------------

    def _request(
        self,
        method: str,
        subpath: str,
        *,
        endpoint: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        needs_user: bool = True,
    ) -> httpx.Response:
        self.throttle(endpoint)
        params: dict[str, Any] = {"clientId": self.client_id, "timestamp": str(int(time.time()))}
        if needs_user:
            if not self.user_secret:
                raise ValueError("SnapTrade needs a userSecret for this call")
            params["userId"] = self.user_id
            params["userSecret"] = self.user_secret
        if query:
            params.update({k: v for k, v in query.items() if v is not None})
        raw_query = urlencode(sorted(params.items()))
        signed_path = "/api/v1" + subpath
        signature = sign(self.consumer_key, signed_path, raw_query, body)
        url = f"{self.base_url}{subpath}?{raw_query}"
        headers = {"Signature": signature, "Content-Type": "application/json"}

        for attempt in range(self.MAX_ATTEMPTS):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            resp = self.http.request(method, url, json=body, headers=headers)
            if resp.status_code in (429, 418):
                self.throttle_after(endpoint)
                raise RateLimited(_retry_after(resp), endpoint)
            if resp.status_code in (500, 502, 503, 504) and attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(0.5 * 2**attempt)
                continue
            return resp
        raise RuntimeError("unreachable")  # pragma: no cover

    def _get_json(self, subpath: str, *, endpoint: str, query: dict[str, Any] | None = None) -> Any:
        resp = self._request("GET", subpath, endpoint=endpoint, query=query)
        resp.raise_for_status()
        return resp.json()

    def _account_activities(
        self, account_ref: str, types: tuple[str, ...], since: datetime | None, offset: int
    ) -> list[dict[str, Any]]:
        query = {
            "type": ",".join(types),
            "offset": offset,
            "limit": PAGE_SIZE,
        }
        if since is not None:
            query["startDate"] = since.date().isoformat()
        return self._get_json(
            f"/accounts/{account_ref}/activities", endpoint="Activities", query=query
        )["data"]

    # ---- account selection -------------------------------------------------

    def list_accounts(self) -> list[dict[str, Any]]:
        """Every brokerage account this SnapTrade user has connected. The
        connect flow shows these and the user (or Koine, for a single-account
        connection) picks the one that becomes `account_ref`."""
        rows = self._get_json("/accounts", endpoint="ListAccounts")
        return [
            {
                "account_ref": r["id"],
                "institution_name": r.get("institution_name", ""),
                "name": r.get("name"),
                "number": r.get("number"),
                "brokerage_authorization": r.get("brokerage_authorization"),
                "is_paper": bool(r.get("is_paper", False)),
            }
            for r in rows
        ]

    def _require_account(self) -> str:
        if not self.account_ref:
            raise ValueError("SnapTrade connector has no account_ref selected")
        return self.account_ref

    # ---- VenueConnector / AssetVenueConnector -----------------------------

    def check_write_scope(self) -> bool | None:
        """SnapTrade has no scope-probe endpoint of its own; the read-only
        guarantee comes from `connectionType=read` at connect time, checked
        via `prove_read_only()` instead (D4: aggregator_flag)."""
        return None

    def prove_read_only(self) -> ReadOnlyEvidence:
        account_ref = self._require_account()
        accounts = {a["account_ref"]: a for a in self.list_accounts()}
        account = accounts.get(account_ref)
        auth_id = account.get("brokerage_authorization") if account else None
        if not auth_id:
            return ReadOnlyEvidence(
                method=ReadOnlyProof.AGGREGATOR_FLAG,
                read_only=None,
                checked_at=datetime.now(UTC),
                details={"reason": "no brokerage_authorization on this account"},
            )
        auth = self._get_json(f"/authorizations/{auth_id}", endpoint="Authorization")
        conn_type = auth.get("type")
        return ReadOnlyEvidence(
            method=ReadOnlyProof.AGGREGATOR_FLAG,
            read_only=(conn_type == "read") if conn_type else None,
            checked_at=datetime.now(UTC),
            details={
                "connection_type": conn_type,
                "disabled": bool(auth.get("disabled", False)),
            },
        )

    def fetch_balances(self) -> NormalizedBalances:
        account_ref = self._require_account()
        rows = self._get_json(f"/accounts/{account_ref}/balances", endpoint="Balances")
        assets = []
        for row in rows:
            code = (row.get("currency") or {}).get("code") or "USD"
            cash = row.get("cash")
            if cash is None:
                continue
            assets.append(
                {"asset": code, "atoms": _atoms(cash, CASH_DECIMALS), "decimals": CASH_DECIMALS}
            )
        return NormalizedBalances(as_of=datetime.now(UTC), assets=assets)

    def fetch_positions_raw(self) -> list[dict[str, Any]]:
        """`holdings` combines positions and balances in one call; SnapTrade
        marks the per-endpoint positions/orders split deprecated in favour of
        it (docs example checked 2026-09-26). Used by scripts/snaptrade_smoke.py."""
        account_ref = self._require_account()
        return self._get_json(f"/accounts/{account_ref}/holdings", endpoint="Holdings").get(
            "positions", []
        )

    # ---- asset fills (BUY / SELL) -----------------------------------------

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        account_ref = self._require_account()
        offset = int(cursor) if cursor else 0
        rows = self._account_activities(account_ref, FILL_TYPES, since, offset)
        items = [self._to_fill(account_ref, r) for r in rows]
        next_cursor = str(offset + PAGE_SIZE) if len(rows) == PAGE_SIZE else None
        return Page(items=items, next_cursor=next_cursor, resume_cursor=None)

    def _to_fill(self, account_ref: str, r: dict[str, Any]) -> NormalizedAssetFill:
        symbol = _symbol_of(r)
        currency = _currency_of(r)
        units = _dec(r["units"])
        price = _dec(r["price"])
        fee = abs(_dec(r.get("fee")))
        gross = units * price
        action = Action.BUY if r["type"] == "BUY" else Action.SELL
        qty_atoms = _atoms(units, QTY_DECIMALS)
        # FILL_QUOTE is the gross trade value only (base.py: "FILL_QUOTE
        # -(qty x price) quote, FEE -fee in the fee asset" -- two separate
        # legs). Folding the fee into cash_delta here as well as adding the
        # FEE leg below would charge every fee twice.
        cash_delta = -gross if action is Action.BUY else gross
        legs = [
            NormalizedLeg(
                asset=symbol,
                delta_atoms=qty_atoms if action is Action.BUY else -qty_atoms,
                decimals=QTY_DECIMALS,
                kind=LegKind.FILL_BASE,
                instrument_symbol=symbol,
            ),
            NormalizedLeg(
                asset=currency,
                delta_atoms=_round_atoms(cash_delta, CASH_DECIMALS),
                decimals=CASH_DECIMALS,
                kind=LegKind.FILL_QUOTE,
                instrument_symbol=symbol,
            ),
        ]
        if fee > 0:
            legs.append(
                NormalizedLeg(
                    asset=currency,
                    delta_atoms=-_atoms(fee, CASH_DECIMALS),
                    decimals=CASH_DECIMALS,
                    kind=LegKind.FEE,
                    instrument_symbol=symbol,
                )
            )
        ref = content_hash(
            account_ref,
            r["type"],
            r.get("trade_date") or r.get("settlement_date"),
            symbol,
            r["units"],
            r["price"],
            r.get("fee"),
            currency,
        )
        instrument = NormalizedInstrument(
            symbol=symbol,
            type=InstType.EQUITY,
            base_asset=symbol,
            quote_asset=currency,
            qty_decimals=QTY_DECIMALS,
        )
        return NormalizedAssetFill(
            venue_fill_id=ref,
            instrument=instrument,
            action=action,
            qty_atoms=qty_atoms,
            price=str(price),
            legs=legs,
            filled_at=_date_only(r.get("trade_date") or r["settlement_date"]),
        )

    # ---- events (everything else) ------------------------------------------

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        account_ref = self._require_account()
        offset = int(cursor) if cursor else 0
        rows = self._account_activities(account_ref, EVENT_TYPES, since, offset)
        items = [
            e
            for r in rows
            if r["type"] not in _UNSUPPORTED_TYPES
            for e in [self._to_event(account_ref, r)]
        ]
        next_cursor = str(offset + PAGE_SIZE) if len(rows) == PAGE_SIZE else None
        return Page(items=items, next_cursor=next_cursor, resume_cursor=None)

    def _to_event(self, account_ref: str, r: dict[str, Any]) -> NormalizedEvent:
        kind_type = r["type"]
        currency = _currency_of(r)
        occurred_at = _date_only(r.get("trade_date") or r["settlement_date"])
        symbol = _symbol_of(r) if _has_symbol(r) else None
        amount = _dec(r.get("amount"))
        units = _dec(r.get("units"))
        legs: list[NormalizedLeg] = []
        primary_kind = LegKind.ADJUSTMENT

        if kind_type == "CONTRIBUTION":
            primary_kind = LegKind.DEPOSIT
            legs = [_cash_leg(currency, amount, LegKind.DEPOSIT)]
        elif kind_type == "WITHDRAWAL":
            primary_kind = LegKind.WITHDRAWAL
            legs = [_cash_leg(currency, amount, LegKind.WITHDRAWAL)]
        elif kind_type == "DIVIDEND":
            primary_kind = LegKind.DIVIDEND
            legs = [_cash_leg(currency, amount, LegKind.DIVIDEND)]
        elif kind_type == "INTEREST":
            primary_kind = LegKind.INTEREST
            legs = [_cash_leg(currency, amount, LegKind.INTEREST)]
        elif kind_type == "FEE":
            primary_kind = LegKind.FEE
            legs = [_cash_leg(currency, -abs(amount), LegKind.FEE)]
        elif kind_type == "TAX":
            primary_kind = LegKind.TAX
            legs = [_cash_leg(currency, amount, LegKind.TAX)]
        elif kind_type == "REI":
            # Reinvested dividend: cash income immediately spent on shares.
            # Modelled as three legs so the cash curve nets to zero and the
            # position still grows by `units` (module docstring).
            primary_kind = LegKind.DIVIDEND
            if amount:
                div_atoms = _atoms(abs(amount), CASH_DECIMALS)  # raw venue field
            else:
                div_atoms = _round_atoms(units * _dec(r.get("price")), CASH_DECIMALS)  # derived
            legs = [
                NormalizedLeg(
                    asset=currency,
                    delta_atoms=div_atoms,
                    decimals=CASH_DECIMALS,
                    kind=LegKind.DIVIDEND,
                ),
                NormalizedLeg(
                    asset=currency,
                    delta_atoms=-div_atoms,
                    decimals=CASH_DECIMALS,
                    kind=LegKind.FILL_QUOTE,
                    instrument_symbol=symbol,
                ),
                NormalizedLeg(
                    asset=symbol,
                    delta_atoms=_atoms(units, QTY_DECIMALS),
                    decimals=QTY_DECIMALS,
                    kind=LegKind.FILL_BASE,
                    instrument_symbol=symbol,
                ),
            ]
        elif kind_type == "STOCK_DIVIDEND":
            # Shares granted at no cost: a quantity change with no cash leg,
            # same shape as SPLIT (base.py LegKind.SPLIT docstring).
            primary_kind = LegKind.SPLIT
            legs = [
                NormalizedLeg(
                    asset=symbol,
                    delta_atoms=_atoms(units, QTY_DECIMALS),
                    decimals=QTY_DECIMALS,
                    kind=LegKind.SPLIT,
                    instrument_symbol=symbol,
                )
            ]
        elif kind_type == "SPLIT":
            primary_kind = LegKind.SPLIT
            legs = [
                NormalizedLeg(
                    asset=symbol,
                    delta_atoms=_atoms(units, QTY_DECIMALS),
                    decimals=QTY_DECIMALS,
                    kind=LegKind.SPLIT,
                    instrument_symbol=symbol,
                )
            ]
        elif kind_type in ("TRANSFER", "EXTERNAL_ASSET_TRANSFER_IN", "EXTERNAL_ASSET_TRANSFER_OUT"):
            if symbol:
                if kind_type == "EXTERNAL_ASSET_TRANSFER_OUT":
                    delta, kind = -abs(units), LegKind.TRANSFER_OUT
                elif kind_type == "EXTERNAL_ASSET_TRANSFER_IN":
                    delta, kind = abs(units), LegKind.TRANSFER_IN
                else:
                    kind = LegKind.TRANSFER_IN if units >= 0 else LegKind.TRANSFER_OUT
                    delta = units
                primary_kind = kind
                legs = [
                    NormalizedLeg(
                        asset=symbol,
                        delta_atoms=_atoms(delta, QTY_DECIMALS),
                        decimals=QTY_DECIMALS,
                        kind=kind,
                        instrument_symbol=symbol,
                    )
                ]
            else:
                kind = LegKind.TRANSFER_IN if amount >= 0 else LegKind.TRANSFER_OUT
                primary_kind = kind
                legs = [_cash_leg(currency, amount, kind)]
        else:  # ADJUSTMENT and anything else not explicitly modelled
            primary_kind = LegKind.ADJUSTMENT
            if symbol and units != 0:
                legs = [
                    NormalizedLeg(
                        asset=symbol,
                        delta_atoms=_atoms(units, QTY_DECIMALS),
                        decimals=QTY_DECIMALS,
                        kind=LegKind.ADJUSTMENT,
                        instrument_symbol=symbol,
                    )
                ]
            else:
                legs = [_cash_leg(currency, amount, LegKind.ADJUSTMENT)]

        ref = content_hash(
            account_ref,
            kind_type,
            r.get("trade_date") or r.get("settlement_date"),
            symbol,
            r.get("units"),
            r.get("price"),
            r.get("amount"),
            currency,
            r.get("description"),
        )
        return NormalizedEvent(
            venue_ref=ref,
            kind=primary_kind,
            legs=legs,
            occurred_at=occurred_at,
            description=r.get("description"),
            ref_is_content_hash=True,
        )


def _cash_leg(currency: str, amount: Decimal, kind: LegKind) -> NormalizedLeg:
    return NormalizedLeg(
        asset=currency, delta_atoms=_atoms(amount, CASH_DECIMALS), decimals=CASH_DECIMALS, kind=kind
    )


def _has_symbol(r: dict[str, Any]) -> bool:
    sym = r.get("symbol")
    return bool(sym)


def _symbol_of(r: dict[str, Any]) -> str:
    """The activity/position `symbol` field is documented as a nested
    "security details" object; be defensive about a bare string too since
    the exact nesting is not confirmed for every account type [VERIFY]."""
    sym = r.get("symbol")
    if isinstance(sym, dict):
        inner = sym.get("symbol", sym)
        if isinstance(inner, dict):
            return str(inner.get("symbol") or inner.get("raw_symbol") or sym.get("id"))
        return str(inner)
    if sym:
        return str(sym)
    raise ValueError(f"activity has no symbol: {r!r}")


def _currency_of(r: dict[str, Any]) -> str:
    currency = r.get("currency")
    if isinstance(currency, dict):
        return str(currency.get("code") or "USD")
    return str(currency or "USD")


def _retry_after(resp: httpx.Response) -> float:
    header = resp.headers.get("Retry-After")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    for reset_header in ("X-RateLimit-Reset", "X-RateLimit-Account-Reset"):
        value = resp.headers.get(reset_header)
        if value:
            try:
                return float(value)
            except ValueError:
                pass
    try:
        body = resp.json()
        match = re.search(r"available in (\d+(?:\.\d+)?) seconds?", str(body.get("detail", "")))
        if match:
            return float(match.group(1))
    except Exception:
        pass
    return 5.0


# ---- registration / deletion (outside VenueConnector: account lifecycle) --


def register_user(
    client_id: str,
    consumer_key: str,
    user_id: str,
    base_url: str = BASE_URL,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """POST /snapTrade/registerUser -> (userId, userSecret). Called once when
    a trader chooses SnapTrade; the userSecret is stored in the vault and
    never shown again (invariant 4)."""
    http = client or httpx.Client(timeout=20.0)
    body = {"userId": user_id}
    params = {"clientId": client_id, "timestamp": str(int(time.time()))}
    raw_query = urlencode(sorted(params.items()))
    signature = sign(consumer_key, "/api/v1/snapTrade/registerUser", raw_query, body)
    resp = http.post(
        f"{base_url}/snapTrade/registerUser?{raw_query}",
        json=body,
        headers={"Signature": signature, "Content-Type": "application/json"},
    )
    if resp.status_code == 409:
        raise SnapTradeUserExists(user_id)
    resp.raise_for_status()
    data = resp.json()
    return data["userId"], data["userSecret"]


def generate_connection_portal_url(
    client_id: str,
    consumer_key: str,
    user_id: str,
    user_secret: str,
    *,
    custom_redirect: str | None = None,
    broker: str | None = None,
    base_url: str = BASE_URL,
    client: httpx.Client | None = None,
) -> str:
    """POST /snapTrade/login with connectionType="read" -- the Connection
    Portal link the user opens to link a brokerage. Read-only is requested
    here, not chosen by the user, and is what `prove_read_only()` later
    confirms via `/authorizations/{id}`."""
    http = client or httpx.Client(timeout=20.0)
    body: dict[str, Any] = {"connectionType": "read", "connectionPortalVersion": "v4"}
    if custom_redirect:
        body["customRedirect"] = custom_redirect
    if broker:
        body["broker"] = broker
    params = {
        "clientId": client_id,
        "timestamp": str(int(time.time())),
        "userId": user_id,
        "userSecret": user_secret,
    }
    raw_query = urlencode(sorted(params.items()))
    signature = sign(consumer_key, "/api/v1/snapTrade/login", raw_query, body)
    resp = http.post(
        f"{base_url}/snapTrade/login?{raw_query}",
        json=body,
        headers={"Signature": signature, "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()["redirectURI"]


def delete_user(
    client_id: str,
    consumer_key: str,
    user_id: str,
    base_url: str = BASE_URL,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """DELETE /snapTrade/deleteUser. Each registered user costs Koine
    $1-2/month (docs/venues/snaptrade.md); call this on disconnect so we stop
    being billed for accounts nobody is tracking any more. Deletion is
    asynchronous (SnapTrade queues it and sends a USER_DELETED webhook)."""
    http = client or httpx.Client(timeout=20.0)
    params = {"clientId": client_id, "timestamp": str(int(time.time())), "userId": user_id}
    raw_query = urlencode(sorted(params.items()))
    signature = sign(consumer_key, "/api/v1/snapTrade/deleteUser", raw_query, None)
    resp = http.delete(
        f"{base_url}/snapTrade/deleteUser?{raw_query}",
        headers={"Signature": signature, "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    return resp.json()


# ---- market data ------------------------------------------------------------


class SnapTradeMarketData(AssetMarketData):
    """SnapTrade has no public, unauthenticated market-data endpoint: quotes
    and symbol search both require a registered user's credentials
    (`/accounts/{id}/quotes`), which is not a shape a shared, credential-free
    MarketDataSource can use (base.py: "one instance serves every account").
    Equities are priced from Alpaca daily closes elsewhere in the platform
    plan; this class intentionally returns the AssetMarketData defaults
    (nothing available) rather than faking a public endpoint that does not
    exist. Documented in docs/venues/snaptrade.md."""

    venue = "snaptrade"
