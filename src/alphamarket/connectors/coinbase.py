"""Coinbase Advanced Trade connector.

Auth: a CDP API key (`key_name` = "organizations/{org}/apiKeys/{id}", plus a
private key) signs a per-request JWT, NOT a persistent header like Kalshi's
RSA-PSS scheme. Per Coinbase's own SDK (`coinbase-advanced-py`
`jwt_generator.build_rest_jwt`), the JWT is:

  header: {"alg": "EdDSA" | "ES256", "kid": key_name, "nonce": <random hex>}
  claims: {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120,
           "uri": f"{METHOD} {HOST}{PATH}"}   # PATH excludes query string

signed and sent as `Authorization: Bearer <jwt>`. `HOST` is always
"api.coinbase.com" regardless of environment; there is no sandbox for
Advanced Trade, so `envs = ("prod",)`.

Key material: current CDP keys are Ed25519 (EdDSA), handed out as base64 of
64 raw bytes (32-byte seed + 32-byte public key) -- see
`docs.cdp.coinbase.com/api-reference/v2/authentication` (fetched 2026-09-26).
Legacy keys are ECDSA P-256 (ES256), handed out as a PEM. `load_private_key`
accepts either.

Payload shapes below are drawn from the official CDP API reference pages
(fetched 2026-09-26; see tests/fixtures/coinbase/README.md for the exact URLs
and dates) plus REAL, unauthenticated calls to the public market-data mirror
made once by `scripts/record_coinbase_market_fixtures.py` (invariant #6: no
live calls in tests, but public reads are allowed once to seed fixtures).
Nothing here has been checked against a real Coinbase account -- none exists
yet (see docs/venues/coinbase.md, requires_partner_approval).

Known simplifications ([VERIFY], see docs/venues/coinbase.md "known gaps"):
  - Per-asset decimals are NOT looked up from `base_increment` /
    `quote_increment` per product; a fixed 8dp (crypto base) / 6dp (USD,
    USDC) is assumed, matching the scale suggested in connectors/base.py.
    Refine via `fetch_instruments()` before trusting sub-8dp assets.
  - `fills` pagination direction (newest-first vs oldest-first) is not
    documented; each page is sorted by `trade_time` locally so the contract's
    "oldest first within the page" rule holds regardless.
  - `size_in_quote=True` fills: the exact base quantity should come from the
    matching `advanced_trade_fill` v2 transaction (task note); this
    connector instead rounds `size / price` down to an assumed 1e-8
    `base_increment`, with no signal downstream that the figure is an
    estimate (the contract's `NormalizedAssetFill` has no field for it).
  - The public product feed's `best_bid_price` / `best_ask_price` fields are
    empty (confirmed live 2026-09-26); `fetch_quote` returns only `last`
    (the `price` field), no bid/ask spread, until an authenticated
    `best_bid_ask` call is wired in behind a real key.
"""

import base64
import json
import secrets
import time
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt as pyjwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
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
    StreamEvent,
    StreamingSource,
    StreamProtocolError,
    StreamUnavailable,
)
from alphamarket.money import to_atoms

HOST = "api.coinbase.com"
BASE_URL = f"https://{HOST}"
ADVANCED_TRADE = "/api/v3/brokerage"
V2 = "/v2"

# Fixed per-asset precision (see module docstring "known simplifications").
CASH_ASSETS = frozenset({"USD", "USDC", "USDT"})
CASH_DECIMALS = 6
CRYPTO_DECIMALS = 8


def _decimals_for(asset: str) -> int:
    return CASH_DECIMALS if asset in CASH_ASSETS else CRYPTO_DECIMALS


def load_private_key(secret: str) -> tuple[str, Any]:
    """Parse a CDP secret into (jwt algorithm, key object).

    Accepts a PEM string (legacy ECDSA P-256 key -> ES256) or a base64 string
    of 32 or 64 raw bytes (Ed25519 seed, optionally followed by the public
    key -> EdDSA), per docs.cdp.coinbase.com/api-reference/v2/authentication.
    """
    secret = secret.strip()
    if secret.startswith("-----BEGIN"):
        key = serialization.load_pem_private_key(secret.encode(), password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise TypeError("Coinbase PEM keys must be ECDSA (legacy ES256) private keys")
        return "ES256", key
    try:
        raw = base64.b64decode(secret, validate=True)
    except Exception as exc:  # noqa: BLE001 - re-raised with venue context
        raise ValueError("Coinbase secret must be a PEM or base64 Ed25519 key") from exc
    if len(raw) not in (32, 64):
        raise ValueError(f"Coinbase Ed25519 secret must decode to 32 or 64 bytes, got {len(raw)}")
    return "EdDSA", Ed25519PrivateKey.from_private_bytes(raw[:32])


def build_jwt(key_name: str, private_key: Any, alg: str, method: str, path: str) -> str:
    """One CDP JWT, valid 2 minutes, bound to exactly this method+path
    (query string excluded, per the SDK's `format_jwt_uri`)."""
    now = int(time.time())
    payload = {
        "sub": key_name,
        "iss": "cdp",
        "nbf": now,
        "exp": now + 120,
        "uri": f"{method} {HOST}{path}",
    }
    headers = {"kid": key_name, "nonce": secrets.token_hex(16)}
    return pyjwt.encode(payload, private_key, algorithm=alg, headers=headers)


def build_ws_jwt(key_name: str, private_key: Any, alg: str) -> str:
    """The websocket JWT: the REST one without the `uri` claim (the SDK's
    `jwt_generator.build_ws_jwt`). Valid 2 minutes, so one is made for every
    subscribe frame; a subscription outlives it."""
    now = int(time.time())
    payload = {"sub": key_name, "iss": "cdp", "nbf": now, "exp": now + 120}
    headers = {"kid": key_name, "nonce": secrets.token_hex(16)}
    return pyjwt.encode(payload, private_key, algorithm=alg, headers=headers)


def _ts(value: str) -> datetime:
    """Coinbase timestamps are RFC3339 (trade_time '...Z', v2 '...-07:00')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _starting_after_from_next_uri(next_uri: str | None) -> str | None:
    """The v2 API's pagination is URI-based (`next_uri`, `previous_uri`), not
    a bare cursor id; pull `starting_after` out of it so the connector can
    keep sending the same query-param pagination it uses everywhere else."""
    if not next_uri:
        return None
    values = parse_qs(urlsplit(next_uri).query).get("starting_after")
    return values[0] if values else None


def _round_base_to_increment(quote_amount: Decimal, price: Decimal, increment: Decimal) -> Decimal:
    """`size_in_quote=True` fills report the quote amount, not the base
    quantity; round the implied base quantity DOWN to the lot size so the
    ledger never claims a fraction the venue could not have executed."""
    raw = quote_amount / price
    steps = (raw / increment).to_integral_value(rounding=ROUND_DOWN)
    return steps * increment


class CoinbaseConnector(AssetVenueConnector):
    venue = "coinbase"

    RETRY_STATUSES = (500, 502, 503, 504)
    MAX_ATTEMPTS = 4
    BACKOFF_BASE = 0.5
    BASE_INCREMENT_ASSUMED = Decimal("1E-8")  # see module docstring [VERIFY]

    def __init__(
        self, key_name: str, private_key: Any, alg: str, client: httpx.Client | None = None
    ):
        self.key_name = key_name
        self.private_key = private_key
        self.alg = alg
        self.client = client or httpx.Client(base_url=BASE_URL, timeout=15.0)
        self._account_ids: list[tuple[str, str]] | None = None

    # ---- transport ---------------------------------------------------------

    def _headers(self, method: str, path: str) -> dict[str, str]:
        token = build_jwt(self.key_name, self.private_key, self.alg, method, path)
        return {"Authorization": f"Bearer {token}"}

    def _request(
        self, method: str, path: str, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.throttle(endpoint)
        for attempt in range(self.MAX_ATTEMPTS):
            resp = self.client.request(
                method, path, params=params, headers=self._headers(method, path)
            )
            if resp.status_code in (429, 418):
                retry_after = resp.headers.get("Retry-After")
                raise RateLimited(float(retry_after) if retry_after else 1.0, endpoint)
            if resp.status_code in self.RETRY_STATUSES and attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(self.BACKOFF_BASE * 2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    def _get(
        self, path: str, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._request("GET", path, endpoint, params)

    def stream_source(self) -> "CoinbaseUserStream":
        """P7: the Advanced Trade `user` websocket channel (NUDGE mode)."""
        return CoinbaseUserStream(self)

    # ---- read-only proof ----------------------------------------------------

    def prove_read_only(self) -> ReadOnlyEvidence:
        """`GET /key_permissions`: `can_view` must be true and `can_trade` /
        `can_transfer` both false. `portfolio_uuid` becomes `account_ref` the
        first time it is seen (a Coinbase key is scoped to exactly one
        portfolio, so this never conflicts with a caller-supplied ref)."""
        try:
            data = self._get(f"{ADVANCED_TRADE}/key_permissions", "KeyPermissions")
        except httpx.HTTPError:
            return ReadOnlyEvidence(
                method=ReadOnlyProof.SCOPE_ENDPOINT, read_only=None, checked_at=datetime.now(UTC)
            )
        can_view = data.get("can_view")
        can_trade = data.get("can_trade")
        can_transfer = data.get("can_transfer")
        portfolio_uuid = data.get("portfolio_uuid")
        if portfolio_uuid and self.account_ref is None:
            self.account_ref = portfolio_uuid
        read_only: bool | None = (
            None if can_view is not True else not bool(can_trade) and not bool(can_transfer)
        )
        return ReadOnlyEvidence(
            method=ReadOnlyProof.SCOPE_ENDPOINT,
            read_only=read_only,
            checked_at=datetime.now(UTC),
            details={
                "can_view": can_view,
                "can_trade": can_trade,
                "can_transfer": can_transfer,
                "portfolio_type": data.get("portfolio_type"),
            },
        )

    def check_write_scope(self) -> bool | None:
        evidence = self.prove_read_only()
        return None if evidence.read_only is None else not evidence.read_only

    # ---- balances ------------------------------------------------------------

    def fetch_balances(self) -> NormalizedBalances:
        """`GET /accounts`: per-asset available + on-hold balance. No
        portfolio-level equity figure is returned by this endpoint, so
        `equity_usd_micros` stays None (M7's `fetch_balance` falls back to
        summing the USD/USDC cash asset, which is exactly what a Coinbase
        cash account is)."""
        assets: list[AssetAmount] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"{ADVANCED_TRADE}/accounts", "Accounts", params)
            for row in data.get("accounts", []):
                currency = row["currency"]
                decimals = _decimals_for(currency)
                available = Decimal(row["available_balance"]["value"])
                hold = Decimal((row.get("hold") or {}).get("value") or "0")
                total = available + hold
                assets.append(
                    AssetAmount(
                        asset=currency, atoms=to_atoms(str(total), decimals), decimals=decimals
                    )
                )
            cursor = data.get("cursor") or None
            if not cursor or not data.get("has_next"):
                break
        return NormalizedBalances(as_of=datetime.now(UTC), assets=assets)

    def _list_account_ids(self) -> list[tuple[str, str]]:
        """(account uuid, currency) pairs, for the v2 transactions walk. One
        v2 account exists per asset the portfolio has ever touched."""
        if self._account_ids is not None:
            return self._account_ids
        out: list[tuple[str, str]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"{ADVANCED_TRADE}/accounts", "Accounts", params)
            for row in data.get("accounts", []):
                out.append((row["uuid"], row["currency"]))
            cursor = data.get("cursor") or None
            if not cursor or not data.get("has_next"):
                break
        self._account_ids = out
        return out

    # ---- fills -----------------------------------------------------------

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        """`GET /orders/historical/fills`. Ordering is undocumented, so each
        page is sorted by `trade_time` locally (safe: it only reorders rows
        already fetched in this one request). `cursor` is a small JSON
        envelope: {"page": <coinbase cursor>} mid-backfill, or
        {"since": <iso timestamp>} as the opaque `resume_cursor` handed back
        for the next incremental sync (Coinbase's own cursor does not
        double as a "resume after this fill" token)."""
        params: dict[str, Any] = {"limit": 250}
        page_cursor: str | None = None
        if cursor:
            state = json.loads(cursor)
            page_cursor = state.get("page")
            if page_cursor is None and state.get("since"):
                since = datetime.fromisoformat(state["since"])
        if page_cursor:
            params["cursor"] = page_cursor
        elif since is not None:
            params["start_sequence_timestamp"] = since.astimezone(UTC).isoformat()
        data = self._get(f"{ADVANCED_TRADE}/orders/historical/fills", "Fills", params)
        raw = sorted(data.get("fills", []), key=lambda f: f["trade_time"])
        items = [self._to_asset_fill(f) for f in raw]
        next_page = data.get("cursor") or None
        if next_page:
            return Page(items=items, next_cursor=json.dumps({"page": next_page}))
        resume = json.dumps({"since": items[-1].filled_at.isoformat()}) if items else None
        return Page(items=items, next_cursor=None, resume_cursor=resume)

    def _to_asset_fill(self, f: dict[str, Any]) -> NormalizedAssetFill:
        product_id = f["product_id"]
        base_asset, _, quote_asset = product_id.partition("-")
        base_decimals = _decimals_for(base_asset)
        quote_decimals = _decimals_for(quote_asset)
        price = Decimal(f["price"])
        size = Decimal(f["size"])
        liquidity = str(f.get("liquidity_indicator") or "").lower() or None
        if f.get("size_in_quote"):
            # `size` is the quote amount actually spent/received; the base
            # quantity is estimated by rounding down to the lot size
            # (module docstring [VERIFY] -- the exact figure lives in the
            # matching `advanced_trade_fill` v2 transaction, not fetched here).
            base_qty = _round_base_to_increment(size, price, self.BASE_INCREMENT_ASSUMED)
            quote_qty = size
        else:
            base_qty = size
            quote_qty = price * size
        base_atoms = to_atoms(str(base_qty), base_decimals)
        # Quote leg rounds to quote precision (half-even, like Polymarket's
        # average-price fills): price x size routinely carries more decimal
        # places than the quote asset's own scale.
        quote_atoms_exact = (quote_qty * (10**quote_decimals)).quantize(Decimal(1))
        commission_atoms = to_atoms(f.get("commission") or "0", quote_decimals)
        action = Action.BUY if str(f["side"]).upper() == "BUY" else Action.SELL
        sign = 1 if action is Action.BUY else -1
        legs = [
            NormalizedLeg(
                asset=base_asset,
                delta_atoms=sign * base_atoms,
                decimals=base_decimals,
                kind=LegKind.FILL_BASE,
                instrument_symbol=product_id,
            ),
            NormalizedLeg(
                asset=quote_asset,
                delta_atoms=-sign * int(quote_atoms_exact),
                decimals=quote_decimals,
                kind=LegKind.FILL_QUOTE,
                instrument_symbol=product_id,
            ),
        ]
        if commission_atoms:
            legs.append(
                NormalizedLeg(
                    asset=quote_asset,
                    delta_atoms=-commission_atoms,
                    decimals=quote_decimals,
                    kind=LegKind.FEE,
                    instrument_symbol=product_id,
                )
            )
        return NormalizedAssetFill(
            venue_fill_id=f["entry_id"],  # stable + unique per account; trade_id is NOT (task note)
            order_id=f.get("order_id"),
            instrument=NormalizedInstrument(
                symbol=product_id,
                type=InstrumentType.SPOT,
                base_asset=base_asset,
                quote_asset=quote_asset,
                qty_decimals=base_decimals,
                qty_step_atoms=1,
            ),
            action=action,
            qty_atoms=base_atoms,
            price=f["price"],
            legs=legs,
            filled_at=_ts(f["trade_time"]),
            liquidity=liquidity,
        )

    # ---- transfers / events ------------------------------------------------

    # v2 transaction type -> ledger leg kind. "advanced_trade_fill" is
    # deliberately absent: it duplicates a row already produced by
    # fetch_asset_fills_page and is skipped (task note: it exists so the
    # exact base amount of a size_in_quote fill CAN be cross-checked there,
    # not so it becomes its own event).
    _SKIP_TYPES = frozenset({"advanced_trade_fill", "buy", "sell", "trade"})
    _TYPE_MAP: dict[str, LegKind] = {
        "send": LegKind.WITHDRAWAL,
        "receive": LegKind.DEPOSIT,
        "fiat_deposit": LegKind.DEPOSIT,
        "fiat_withdrawal": LegKind.WITHDRAWAL,
        "staking_transfer": LegKind.STAKING,
        "unstaking_transfer": LegKind.STAKING,
        "earn_payout": LegKind.STAKING,
        "wrap_asset": LegKind.CONVERT,
        "unwrap_asset": LegKind.CONVERT,
    }

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        """Walks `GET /v2/accounts/:id/transactions` account by account (one
        v2 account per asset the portfolio has held). `cursor` is a JSON
        envelope `{"acct": <index>, "next": <v2 starting_after id>}`; the v2
        API has no since/date filter, so `since` is applied client-side.

        On a cold connector instance (no cached account list yet -- e.g. a
        fresh worker process resuming a backfill from a persisted cursor)
        this makes two HTTP requests instead of one: `_list_account_ids()`'s
        own `Accounts` call, then this page's `Transactions` call. Safe
        either way a `RateLimited`/`BudgetExhausted` lands (no transactions
        page is lost; the cursor already in hand is still valid to retry),
        just not literally "one request per page" on that first call."""
        accounts = self._list_account_ids()
        state = json.loads(cursor) if cursor else {"acct": 0, "next": None}
        acct_idx: int = state["acct"]
        page_cursor: str | None = state.get("next")
        while acct_idx < len(accounts):
            acct_id, _currency = accounts[acct_idx]
            params: dict[str, Any] = {"limit": 100}
            if page_cursor:
                params["starting_after"] = page_cursor
            data = self._get(f"{V2}/accounts/{acct_id}/transactions", "Transactions", params)
            rows = data.get("data", [])
            events = []
            for row in rows:
                event = self._to_event(row)
                if event is not None and (since is None or event.occurred_at > since):
                    events.append(event)
            pagination = data.get("pagination") or {}
            next_id = _starting_after_from_next_uri(pagination.get("next_uri"))
            if next_id:
                return Page(
                    items=events, next_cursor=json.dumps({"acct": acct_idx, "next": next_id})
                )
            acct_idx += 1
            page_cursor = None
            if acct_idx < len(accounts):
                return Page(items=events, next_cursor=json.dumps({"acct": acct_idx, "next": None}))
            return Page(
                items=events, next_cursor=None, resume_cursor=json.dumps({"acct": 0, "next": None})
            )
        return Page(items=[], next_cursor=None, resume_cursor=json.dumps({"acct": 0, "next": None}))

    def _to_event(self, row: dict[str, Any]) -> NormalizedEvent | None:
        tx_type = str(row.get("type") or "")
        if tx_type in self._SKIP_TYPES:
            return None
        kind = self._TYPE_MAP.get(tx_type, LegKind.ADJUSTMENT)
        amount = row["amount"]
        asset = amount["currency"]
        decimals = _decimals_for(asset)
        return NormalizedEvent(
            venue_ref=row["id"],
            kind=kind,
            legs=[
                NormalizedLeg(
                    asset=asset,
                    delta_atoms=to_atoms(amount["amount"], decimals),
                    decimals=decimals,
                    kind=kind,
                )
            ],
            occurred_at=_ts(row["created_at"]),
            description=tx_type if kind is LegKind.ADJUSTMENT else None,
        )


# ---- public market data --------------------------------------------------------


class CoinbaseMarketData(AssetMarketData):
    """`GET /market/products*` -- unauthenticated (CONFIRMED live 2026-09-26,
    see tests/fixtures/coinbase/README.md), so one instance serves every
    strategy and every paper portfolio, like Kalshi's and Polymarket's."""

    venue = "coinbase"
    RETRY_STATUSES = CoinbaseConnector.RETRY_STATUSES
    MAX_ATTEMPTS = CoinbaseConnector.MAX_ATTEMPTS
    BACKOFF_BASE = CoinbaseConnector.BACKOFF_BASE

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(base_url=BASE_URL, timeout=15.0)

    def _get(
        self, path: str, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.throttle(endpoint)
        for attempt in range(self.MAX_ATTEMPTS):
            resp = self.client.get(path, params=params)
            if resp.status_code in (429, 418):
                retry_after = resp.headers.get("Retry-After")
                raise RateLimited(float(retry_after) if retry_after else 1.0, endpoint)
            if resp.status_code in self.RETRY_STATUSES and attempt < self.MAX_ATTEMPTS - 1:
                time.sleep(self.BACKOFF_BASE * 2**attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # pragma: no cover

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        out: list[NormalizedInstrument] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            data = self._get(f"{ADVANCED_TRADE}/market/products", "Products", params)
            for p in data.get("products", []):
                if p.get("product_type") != "SPOT":
                    continue
                base_increment = Decimal(p["base_increment"])
                qty_decimals = -base_increment.normalize().as_tuple().exponent
                out.append(
                    NormalizedInstrument(
                        symbol=p["product_id"],
                        type=InstrumentType.SPOT,
                        base_asset=p["base_currency_id"],
                        quote_asset=p["quote_currency_id"],
                        qty_decimals=max(qty_decimals, 0),
                        qty_step_atoms=to_atoms(p["base_increment"], max(qty_decimals, 0)) or 1,
                        min_qty_atoms=to_atoms(p["base_min_size"], max(qty_decimals, 0)),
                        min_notional=p.get("quote_min_size"),
                    )
                )
            pagination = data.get("pagination") or {}
            if not pagination.get("has_next"):
                break
            cursor = pagination.get("next_cursor")
            if not cursor:
                break
        return out

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """Candles are returned NEWEST first (confirmed live 2026-09-26);
        reversed here so callers always see oldest-first."""
        start_ts = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp())
        end_ts = int(datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC).timestamp())
        data = self._get(
            f"{ADVANCED_TRADE}/market/products/{symbol}/candles",
            "Candles",
            {"start": start_ts, "end": end_ts, "granularity": "ONE_DAY", "limit": 350},
        )
        candles = sorted(data.get("candles", []), key=lambda c: int(c["start"]))
        return [
            DailyClose(
                symbol=symbol,
                day=datetime.fromtimestamp(int(c["start"]), UTC).date(),
                close=c["close"],
            )
            for c in candles
        ]

    def fetch_quote(self, symbol: str) -> Quote | None:
        """The public product feed's `best_bid_price`/`best_ask_price` are
        empty (confirmed live 2026-09-26; see module docstring), so only
        `last` (the `price` field) is populated here."""
        try:
            data = self._get(f"{ADVANCED_TRADE}/market/products/{symbol}", "Products")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        price = data.get("price")
        if not price:
            return None
        return Quote(symbol=symbol, bid=None, ask=None, last=price, as_of=datetime.now(UTC))


# ---- P7: private websocket (StreamMode.NUDGE) ------------------------------------

USER_WS_URL = "wss://advanced-trade-ws-user.coinbase.com"
# Orders the stream remembers (cumulative quantity per order id); finished
# orders are forgotten at once, so this only bounds a flood of open ones.
_MAX_TRACKED_ORDERS = 1000
_DONE_STATUSES = frozenset({"FILLED", "CANCELLED", "EXPIRED", "FAILED"})


class CoinbaseUserStream(StreamingSource):
    """Advanced Trade websocket `user` channel, plus `heartbeats` to keep it
    open (docs: channels close after 60-90 s without updates).

    NUDGE mode: the channel reports ORDERS (cumulative_quantity, avg_price,
    total_fees, status), not individual fills, so it cannot produce the
    poller's fills (keyed by `trade_id`). A rise in an order's
    cumulative_quantity queues the account's sync instead.

    Auth: every subscribe frame carries a fresh CDP JWT (build_ws_jwt). A
    View-only key is expected to be enough for `user` [VERIFY with a real
    key], and which portfolio's orders it reports for a portfolio-scoped
    key is undocumented [VERIFY]; extra orders only cost an extra sync.

    Frames (docs examples, docs.cdp.coinbase.com advanced-trade websocket
    channels; never seen on a real key):
      {"channel":"user","client_id":"","timestamp":"..","sequence_num":0,
       "events":[{"type":"snapshot"|"update","orders":[{"order_id":"..",
       "cumulative_quantity":"0.001","status":"OPEN"|"FILLED"|..,...}]}]}
      {"channel":"heartbeats",...}  {"channel":"subscriptions",...}
      {"type":"error","message":".."}"""

    venue = "coinbase"
    ping_interval_seconds = 30.0  # protocol pings only (ping_frame is None)
    idle_timeout_seconds = 30.0  # heartbeats arrive every second

    def __init__(self, connector: "CoinbaseConnector", ws_url: str = USER_WS_URL) -> None:
        self.connector = connector
        self.ws_url = ws_url
        self._filled: dict[str, Decimal] = {}

    def __repr__(self) -> str:  # never show the key
        return f"CoinbaseUserStream(url={self.ws_url!r})"

    def url(self) -> str:
        return self.ws_url

    def subscribe_frames(self) -> list[str]:
        c = self.connector
        return [
            json.dumps(
                {
                    "type": "subscribe",
                    "channel": channel,
                    "jwt": build_ws_jwt(c.key_name, c.private_key, c.alg),
                }
            )
            for channel in ("heartbeats", "user")
        ]

    def parse(self, frame: str | bytes) -> list[StreamEvent]:
        try:
            msg = json.loads(frame)
        except ValueError:
            raise StreamProtocolError("coinbase: frame is not JSON") from None
        if not isinstance(msg, dict):
            return []
        if msg.get("type") == "error":
            text = str(msg.get("message") or msg.get("reason") or "error")[:200]
            if "auth" in text.lower() or "permission" in text.lower():
                raise StreamUnavailable(f"coinbase: user channel refused: {text}")
            raise StreamProtocolError(f"coinbase: {text}")
        if msg.get("channel") != "user":
            return []  # heartbeats, subscriptions
        nudge_ref: str | None = None
        nudged = False
        for event in msg.get("events") or []:
            snapshot = event.get("type") == "snapshot"
            for order in event.get("orders") or []:
                order_id = str(order.get("order_id") or "")
                try:
                    qty = Decimal(str(order.get("cumulative_quantity") or "0"))
                except ArithmeticError:
                    continue
                before = self._filled.get(order_id, Decimal(0))
                if not snapshot and qty > before:
                    nudged = True
                    nudge_ref = nudge_ref or order_id or None
                if str(order.get("status") or "").upper() in _DONE_STATUSES:
                    self._filled.pop(order_id, None)
                elif len(self._filled) < _MAX_TRACKED_ORDERS or order_id in self._filled:
                    self._filled[order_id] = qty
        return [StreamEvent(nudge=True, ref=nudge_ref)] if nudged else []
