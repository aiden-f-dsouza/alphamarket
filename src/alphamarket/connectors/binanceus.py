"""Binance.US spot connector (contract v2, asset-shaped venue).

Auth: HMAC-SHA256 over the request's query string, header `X-MBX-APIKEY:
<api key>`, trailing `&signature=<hex hmac>` param. Ed25519 key-signing
exists on Binance.com's newer API-key type but is not confirmed on
docs.binance.us as of this writing -- [VERIFY] before offering it as a
`key_secret_passphrase`/other credential kind here; HMAC is what every
Binance.US doc excerpt and the 2026-09-16 venue research describe, so that
is all this module implements.

Base host and every endpoint path below are checked against
https://docs.binance.us/ (mirrors the api.binance.com REST shape at
api.binance.us) and https://github.com/binance-us/binance-us-api-docs
(rest-api.md), both fetched 2026-09-26. Paths marked [VERIFY] were not in
the fetched excerpt and are carried over from Binance's global docs / the
2026-09-16 crypto-venues research, which states "Binance.US: same shapes at
api.binance.us" -- they need confirming against a live account before this
connector reads real user data (scripts/binanceus_smoke.py is exactly that
check).

Endpoint quirks that shape this module:
  - `/api/v3/myTrades` is PER SYMBOL, so there is no "give me every fill"
    call: the connector must discover which symbols an account has touched
    and page each one independently (see `_discover_symbols`).
  - `myTrades` supports `symbol+fromId` (no time bound, ascending by id)
    and `symbol+startTime+endTime` (bound to <=24h apart) but not both with
    an unbounded range. This connector uses `fromId` once a symbol's first
    trade id is known (cheap, unbounded, resumable) and falls back to
    day-by-day `startTime`/`endTime` windows to discover the FIRST trade id
    of a symbol we have never scanned before (see `Page.resume_cursor` on
    `fetch_asset_fills_page`).
  - deposit/withdraw/convert/dust histories are windowed to <=90 days per
    call (a known Binance.US quirk: some deployments silently clamp to the
    last ~90 days even when a wider range is requested -- see
    docs/venues/binanceus.md "known gaps"), so `fetch_events_page` walks
    89-day chunks forward from `since` (or venue launch) to now, one chunk
    (one HTTP call) per page, one source per pass.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    BalanceUnavailable,
    DailyClose,
    InstrumentType,
    LegKind,
    NormalizedAssetFill,
    NormalizedBalance,
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
from alphamarket.money import from_atoms, to_atoms, to_micros

# ---- module constants (base URL + endpoint paths; NOT in config.py) --------

BASE_URL = "https://api.binance.us"

EP_ACCOUNT = "/api/v3/account"  # SIGNED, weight 10 (docs.binance.us rest-api.md, 2026-09-26)
EP_MY_TRADES = "/api/v3/myTrades"  # SIGNED, weight 10 with symbol
EP_API_RESTRICTIONS = "/sapi/v1/account/apiRestrictions"  # SIGNED; [VERIFY] exact path on
# docs.binance.us -- confirmed for api.binance.com (developers.binance.com/docs/wallet/
# account/api-key-permission); the 2026-09-16 research says Binance.US serves "the same
# shapes" at api.binance.us, but a live account has not confirmed this path specifically.
EP_DEPOSIT_HISTORY = "/sapi/v1/capital/deposit/hisrec"  # SIGNED; 90d window [VERIFY window
# behaviour -- a documented Binance.US GitHub issue says this endpoint has ignored
# startTime/endTime in the past and only returns a recent slice regardless.
EP_WITHDRAW_HISTORY = "/sapi/v1/capital/withdraw/history"  # SIGNED; same [VERIFY] as above
EP_CONVERT_HISTORY = "/sapi/v1/convert/tradeFlow"  # SIGNED; [VERIFY] path on binance.us
# (carried over from Binance global docs cited in 2026-09-16 crypto-venues.md)
EP_DUST_LOG = "/sapi/v1/asset/dust-log"  # SIGNED; [VERIFY] path on binance.us (Binance
# global research doc cites GET /sapi/v1/asset/dribblet for the same report)
EP_EXCHANGE_INFO = "/api/v3/exchangeInfo"  # public, weight 20
EP_KLINES = "/api/v3/klines"  # public, weight 1
EP_BOOK_TICKER = "/api/v3/ticker/bookTicker"  # public, weight 1 (single symbol)

# Assets a discovered trading pair is allowed to quote in, checked in this order
# when crossing an account's held/deposited assets against exchangeInfo symbols.
COMMON_QUOTE_ASSETS = ("USD", "USDT", "USDC", "BUSD", "BTC")

DAY_MS = 86_400_000
MY_TRADES_PAGE_LIMIT = 1000
# Binance.US launched in 2019; used only as the lower bound for a symbol's very
# first backfill window when the caller passes no `since`.
VENUE_EPOCH_MS = int(datetime(2019, 9, 1, tzinfo=UTC).timestamp() * 1000)
# Deposit/withdraw/convert/dust windows must be < 90 days; 89 gives headroom.
EVENTS_WINDOW_MS = 89 * DAY_MS

BNB_ASSET = "BNB"  # dust conversions always credit BNB


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def _raise_if_rate_limited(resp: httpx.Response, endpoint: str) -> None:
    if resp.status_code in (429, 418):
        retry_after = resp.headers.get("Retry-After")
        try:
            seconds = float(retry_after) if retry_after else 60.0
        except ValueError:
            seconds = 60.0
        raise RateLimited(seconds, endpoint=endpoint)


def _merge_legs(rows: list[tuple[str, LegKind, int, int]]) -> list[NormalizedLeg]:
    """rows: (asset, kind, delta_atoms, decimals). Sums deltas that share an
    (asset, kind) pair before constructing legs, so a batch with more than one
    row touching the same asset (e.g. two dust conversions of the same coin
    in one operateTime batch) never trips `_no_duplicate_legs`. Decimals for
    a merged pair must agree; the venue reports one precision per asset, so
    they always do in practice, but we keep the finer one just in case."""
    merged: dict[tuple[str, LegKind], tuple[int, int]] = {}
    for asset, kind, delta, decimals in rows:
        key = (asset, kind)
        if key in merged:
            prev_delta, prev_decimals = merged[key]
            merged[key] = (prev_delta + delta, max(prev_decimals, decimals))
        else:
            merged[key] = (delta, decimals)
    return [
        NormalizedLeg(asset=asset, delta_atoms=delta, decimals=decimals, kind=kind)
        for (asset, kind), (delta, decimals) in merged.items()
    ]


@dataclass
class _Instrument:
    symbol: str
    base_asset: str
    quote_asset: str
    base_decimals: int
    quote_decimals: int
    qty_step_atoms: int = 1
    min_qty_atoms: int = 0
    min_notional: str | None = None


class BinanceUSConnector(AssetVenueConnector):
    venue = "binanceus"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_secret = api_secret
        self.client = client or httpx.Client(timeout=15.0)
        self._instrument_cache: dict[str, _Instrument] | None = None

    # ---- signing + transport ---------------------------------------------

    def _sign(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params)
        params.setdefault("timestamp", _now_ms())
        params.setdefault("recvWindow", 5000)
        query = urlencode(params)
        signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = signature
        return params

    def _get(
        self, endpoint: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> httpx.Response:
        self.throttle(_throttle_name(endpoint))
        req_params = dict(params or {})
        headers = {}
        if signed:
            req_params = self._sign(req_params)
            headers["X-MBX-APIKEY"] = self.api_key
        resp = self.client.get(f"{self.base_url}{endpoint}", params=req_params, headers=headers)
        used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        self.throttle_after(
            _throttle_name(endpoint), used=int(used) if used and used.isdigit() else None
        )
        _raise_if_rate_limited(resp, endpoint)
        resp.raise_for_status()
        return resp

    def _get_json(
        self, endpoint: str, params: dict[str, Any] | None = None, *, signed: bool = False
    ) -> Any:
        return self._get(endpoint, params, signed=signed).json()

    # ---- read-only proof ---------------------------------------------------

    def check_write_scope(self) -> bool | None:
        try:
            data = self._get_json(EP_API_RESTRICTIONS, signed=True)
        except httpx.HTTPError:
            return None
        if not isinstance(data, dict):
            return None
        writable_flags = (
            data.get("enableSpotAndMarginTrading"),
            data.get("enableWithdrawals"),
            data.get("enableMargin"),
            data.get("enableFutures"),
            data.get("enableInternalTransfer"),
        )
        if all(flag is None for flag in writable_flags):
            return None
        return any(bool(flag) for flag in writable_flags)

    def prove_read_only(self) -> ReadOnlyEvidence:
        try:
            data = self._get_json(EP_API_RESTRICTIONS, signed=True)
        except httpx.HTTPError:
            return ReadOnlyEvidence(
                method=ReadOnlyProof.SCOPE_ENDPOINT, read_only=None, checked_at=datetime.now(UTC)
            )
        if not isinstance(data, dict):
            return ReadOnlyEvidence(
                method=ReadOnlyProof.SCOPE_ENDPOINT, read_only=None, checked_at=datetime.now(UTC)
            )
        details: dict[str, str | bool | int | None] = {
            "enableReading": data.get("enableReading"),
            "enableSpotAndMarginTrading": data.get("enableSpotAndMarginTrading"),
            "enableWithdrawals": data.get("enableWithdrawals"),
            "enableMargin": data.get("enableMargin"),
            "enableFutures": data.get("enableFutures"),
            "enableInternalTransfer": data.get("enableInternalTransfer"),
            "ipRestrict": data.get("ipRestrict"),
        }
        writable = any(
            bool(details[k])
            for k in (
                "enableSpotAndMarginTrading",
                "enableWithdrawals",
                "enableMargin",
                "enableFutures",
                "enableInternalTransfer",
            )
        )
        return ReadOnlyEvidence(
            method=ReadOnlyProof.SCOPE_ENDPOINT,
            read_only=not writable,
            checked_at=datetime.now(UTC),
            details=details,
        )

    # ---- balances ------------------------------------------------------------

    def _instruments(self) -> dict[str, _Instrument]:
        if self._instrument_cache is None:
            self._instrument_cache = _fetch_instrument_map(self._get_json, EP_EXCHANGE_INFO)
        return self._instrument_cache

    def _asset_decimals(self, asset: str) -> int:
        best = 8
        for inst in self._instruments().values():
            if inst.base_asset == asset:
                best = max(best, inst.base_decimals)
            if inst.quote_asset == asset:
                best = max(best, inst.quote_decimals)
        return best

    def fetch_balances(self) -> NormalizedBalances:
        data = self._get_json(EP_ACCOUNT, signed=True)
        assets = []
        for row in data.get("balances", []):
            asset = row["asset"]
            free = Decimal(row.get("free", "0"))
            locked = Decimal(row.get("locked", "0"))
            total = free + locked
            if total == 0:
                continue
            decimals = self._asset_decimals(asset)
            assets.append(
                AssetAmount(asset=asset, atoms=to_atoms(total, decimals), decimals=decimals)
            )
        return NormalizedBalances(as_of=datetime.now(UTC), assets=assets)

    def fetch_balance(self) -> NormalizedBalance:
        """Override AssetVenueConnector's default: that helper only accepts a
        USD/USDC asset reported at <=6 decimals, but Binance.US reports every
        balance (fiat included) at the same precision as exchangeInfo declares
        for it, which can be finer than 6dp. Convert exactly via Decimal
        instead of assuming a fixed scale."""
        balances = self.fetch_balances()
        for amount in balances.assets:
            if amount.asset in ("USD", "USDC"):
                value = from_atoms(amount.atoms, amount.decimals)
                return NormalizedBalance(balance_micros=to_micros(value), as_of=balances.as_of)
        raise BalanceUnavailable(f"{self.venue}: no USD/USDC cash asset in balances")

    # ---- symbol discovery ------------------------------------------------

    def _discover_symbols(self) -> list[str]:
        balances = self._get_json(EP_ACCOUNT, signed=True).get("balances", [])
        held = {
            b["asset"]
            for b in balances
            if Decimal(b.get("free", "0")) + Decimal(b.get("locked", "0")) > 0
        }
        try:
            deposits = self._get_json(
                EP_DEPOSIT_HISTORY,
                {"startTime": VENUE_EPOCH_MS, "endTime": _now_ms()},
                signed=True,
            )
        except httpx.HTTPError:
            deposits = []
        deposited = {d["coin"] for d in deposits if isinstance(d, dict) and "coin" in d}
        candidates = held | deposited
        symbols = []
        for inst in self._instruments().values():
            if inst.base_asset in candidates and inst.quote_asset in COMMON_QUOTE_ASSETS:
                symbols.append(inst.symbol)
        return sorted(set(symbols))

    # ---- fills -------------------------------------------------------------

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        since_ms = int(since.timestamp() * 1000) if since is not None else VENUE_EPOCH_MS
        if cursor is None:
            state = {
                "symbols": self._discover_symbols(),
                "from_ids": {},
                "queue": [],
                "window_start_ms": None,
            }
            state["queue"] = list(state["symbols"])
        else:
            state = json.loads(cursor)

        if not state["queue"]:
            # Previous pass finished (resume_cursor was returned); start a
            # fresh pass, re-discovering symbols so newly-traded pairs join
            # the queue. Symbols we already have a from_id for resume via
            # fromId; brand-new ones fall back to window scanning below.
            discovered = self._discover_symbols()
            state["symbols"] = sorted(set(state["symbols"]) | set(discovered))
            state["queue"] = list(state["symbols"])
            state["window_start_ms"] = None
            if not state["queue"]:
                return Page(items=[], next_cursor=None, resume_cursor=json.dumps(state))

        symbol = state["queue"][0]
        instruments = self._instruments()
        inst = instruments.get(symbol)
        from_id = state["from_ids"].get(symbol)

        if from_id is not None:
            params = {"symbol": symbol, "fromId": from_id + 1, "limit": MY_TRADES_PAGE_LIMIT}
            raw = self._get_json(EP_MY_TRADES, params, signed=True)
            raw.sort(key=lambda t: (t["time"], t["id"]))
            if raw:
                state["from_ids"][symbol] = raw[-1]["id"]
            if len(raw) < MY_TRADES_PAGE_LIMIT:
                state["queue"].pop(0)
        else:
            window_start = state["window_start_ms"]
            if window_start is None:
                window_start = since_ms
            window_end = min(window_start + DAY_MS - 1, _now_ms())
            params = {
                "symbol": symbol,
                "startTime": window_start,
                "endTime": window_end,
                "limit": MY_TRADES_PAGE_LIMIT,
            }
            raw = self._get_json(EP_MY_TRADES, params, signed=True)
            raw.sort(key=lambda t: (t["time"], t["id"]))
            if raw:
                state["from_ids"][symbol] = raw[-1]["id"]
            if window_end >= _now_ms() - 1:
                state["queue"].pop(0)
                state["window_start_ms"] = None
            else:
                state["window_start_ms"] = window_end + 1

        items = [_to_asset_fill(symbol, inst, t) for t in raw] if inst else []

        if state["queue"]:
            return Page(items=items, next_cursor=json.dumps(state), resume_cursor=None)
        return Page(items=items, next_cursor=None, resume_cursor=json.dumps(state))

    # ---- events (deposit/withdraw/convert/dust) ---------------------------

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        since_ms = int(since.timestamp() * 1000) if since is not None else VENUE_EPOCH_MS
        sources = ["deposit", "withdraw", "convert", "dust"]
        if cursor is None:
            state = {"sources": sources, "i": 0, "window_start_ms": since_ms}
        else:
            state = json.loads(cursor)
            if state["i"] >= len(state["sources"]):
                state = {"sources": sources, "i": 0, "window_start_ms": since_ms}

        source = state["sources"][state["i"]]
        window_start = state["window_start_ms"]
        window_end = min(window_start + EVENTS_WINDOW_MS, _now_ms())
        params = {"startTime": window_start, "endTime": window_end}

        events: list[NormalizedEvent] = []
        try:
            if source == "deposit":
                raw = self._get_json(EP_DEPOSIT_HISTORY, params, signed=True)
                events = [
                    _deposit_event(d, self._asset_decimals) for d in raw if isinstance(d, dict)
                ]
            elif source == "withdraw":
                raw = self._get_json(EP_WITHDRAW_HISTORY, params, signed=True)
                events = [
                    _withdraw_event(w, self._asset_decimals) for w in raw if isinstance(w, dict)
                ]
            elif source == "convert":
                raw = self._get_json(EP_CONVERT_HISTORY, params, signed=True)
                rows = raw.get("list", raw) if isinstance(raw, dict) else raw
                events = [
                    _convert_event(c, self._asset_decimals) for c in rows if isinstance(c, dict)
                ]
            else:  # dust
                raw = self._get_json(EP_DUST_LOG, params, signed=True)
                rows = raw.get("userAssetDribbletDetails", raw) if isinstance(raw, dict) else raw
                events = [_dust_event(d, self._asset_decimals) for d in rows if isinstance(d, dict)]
        except httpx.HTTPStatusError:
            events = []

        events.sort(key=lambda e: e.occurred_at)

        if window_end >= _now_ms():
            state["i"] += 1
            state["window_start_ms"] = since_ms
        else:
            state["window_start_ms"] = window_end + 1

        if state["i"] >= len(state["sources"]):
            return Page(items=events, next_cursor=None, resume_cursor=json.dumps(state))
        return Page(items=events, next_cursor=json.dumps(state), resume_cursor=None)


def _throttle_name(endpoint: str) -> str:
    return {
        EP_ACCOUNT: "account",
        EP_MY_TRADES: "myTrades",
        EP_API_RESTRICTIONS: "apiRestrictions",
        EP_DEPOSIT_HISTORY: "depositHistory",
        EP_WITHDRAW_HISTORY: "withdrawHistory",
        EP_CONVERT_HISTORY: "convertHistory",
        EP_DUST_LOG: "dustLog",
        EP_EXCHANGE_INFO: "exchangeInfo",
        EP_KLINES: "klines",
        EP_BOOK_TICKER: "bookTicker",
    }.get(endpoint, endpoint)


def _fetch_instrument_map(get_json, endpoint: str) -> dict[str, _Instrument]:
    """Includes every symbol exchangeInfo returns regardless of `status`
    (TRADING, BREAK, ...): a delisted pair still needs its precision to
    replay historical fills, and `_discover_symbols` is what decides which
    symbols are worth *querying* (it crosses held/deposited assets, so a
    long-dead pair with no balance never gets polled anyway)."""
    data = get_json(endpoint)
    out: dict[str, _Instrument] = {}
    for s in data.get("symbols", []):
        base, quote = s["baseAsset"], s["quoteAsset"]
        base_decimals = int(s.get("baseAssetPrecision", 8))
        quote_decimals = int(s.get("quoteAssetPrecision", s.get("quotePrecision", 8)))
        qty_step_atoms = 1
        min_qty_atoms = 0
        min_notional: str | None = None
        for f in s.get("filters", []):
            ftype = f.get("filterType")
            if ftype == "LOT_SIZE":
                step = f.get("stepSize")
                if step is not None:
                    qty_step_atoms = to_atoms(step, base_decimals)
                min_qty = f.get("minQty")
                if min_qty is not None:
                    min_qty_atoms = to_atoms(min_qty, base_decimals)
            elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                min_notional = f.get("minNotional")
        out[s["symbol"]] = _Instrument(
            symbol=s["symbol"],
            base_asset=base,
            quote_asset=quote,
            base_decimals=base_decimals,
            quote_decimals=quote_decimals,
            qty_step_atoms=max(qty_step_atoms, 1),
            min_qty_atoms=min_qty_atoms,
            min_notional=min_notional,
        )
    return out


def _to_asset_fill(symbol: str, inst: _Instrument, t: dict[str, Any]) -> NormalizedAssetFill:
    is_buyer = bool(t["isBuyer"])
    qty_atoms = to_atoms(t["qty"], inst.base_decimals)
    quote_atoms = to_atoms(t["quoteQty"], inst.quote_decimals)
    fee_asset = t.get("commissionAsset") or inst.quote_asset
    # The fee asset is usually base or quote (occasionally BNB, a third
    # asset, when fee-in-BNB is enabled); only base/quote decimals are known
    # here without another exchangeInfo lookup, so a third asset falls back
    # to 8dp (matches every crypto asset this venue lists in exchangeInfo).
    if fee_asset == inst.base_asset:
        fee_decimals = inst.base_decimals
    elif fee_asset == inst.quote_asset:
        fee_decimals = inst.quote_decimals
    else:
        fee_decimals = 8
    fee_atoms = to_atoms(t.get("commission") or "0", fee_decimals)

    rows: list[tuple[str, LegKind, int, int]] = [
        (
            inst.base_asset,
            LegKind.FILL_BASE,
            qty_atoms if is_buyer else -qty_atoms,
            inst.base_decimals,
        ),
        (
            inst.quote_asset,
            LegKind.FILL_QUOTE,
            -quote_atoms if is_buyer else quote_atoms,
            inst.quote_decimals,
        ),
    ]
    if fee_atoms:
        rows.append((fee_asset, LegKind.FEE, -fee_atoms, fee_decimals))
    legs = _merge_legs(rows)
    for leg in legs:
        leg.instrument_symbol = symbol

    return NormalizedAssetFill(
        venue_fill_id=f"{symbol}:{t['id']}",
        order_id=str(t["orderId"]) if t.get("orderId") is not None else None,
        instrument=NormalizedInstrument(
            symbol=symbol,
            type=InstrumentType.SPOT,
            base_asset=inst.base_asset,
            quote_asset=inst.quote_asset,
            qty_decimals=inst.base_decimals,
            qty_step_atoms=inst.qty_step_atoms,
            min_qty_atoms=inst.min_qty_atoms,
            min_notional=inst.min_notional,
        ),
        action=Action.BUY if is_buyer else Action.SELL,
        qty_atoms=qty_atoms,
        price=str(t["price"]),
        legs=legs,
        filled_at=_ms_to_dt(int(t["time"])),
        liquidity="maker" if t.get("isMaker") else "taker",
    )


def _deposit_event(d: dict[str, Any], decimals_for) -> NormalizedEvent:
    asset = d["coin"]
    decimals = decimals_for(asset)
    atoms = to_atoms(d.get("amount") or "0", decimals)
    return NormalizedEvent(
        venue_ref=f"deposit:{d.get('id') or d.get('txId') or d['insertTime']}",
        kind=LegKind.DEPOSIT,
        legs=[
            NormalizedLeg(asset=asset, delta_atoms=atoms, decimals=decimals, kind=LegKind.DEPOSIT)
        ],
        occurred_at=_ms_to_dt(int(d["insertTime"])),
        description=f"Deposit {d.get('network', '')}".strip(),
    )


def _withdraw_event(w: dict[str, Any], decimals_for) -> NormalizedEvent:
    asset = w["coin"]
    decimals = decimals_for(asset)
    atoms = to_atoms(w.get("amount") or "0", decimals)
    rows: list[tuple[str, LegKind, int, int]] = [(asset, LegKind.WITHDRAWAL, -atoms, decimals)]
    fee = w.get("transactionFee")
    if fee and Decimal(fee) != 0:
        rows.append((asset, LegKind.FEE, -to_atoms(fee, decimals), decimals))
    # `applyTime` on the withdraw-history endpoint is a naive "YYYY-MM-DD HH:MM:SS"
    # string (UTC, undocumented as such but consistent with every other Binance
    # timestamp) rather than epoch millis like every other history endpoint here
    # [VERIFY on a live account]; `insertTime`, when present instead, IS epoch ms.
    occurred_raw = w.get("applyTime") or w.get("insertTime")
    if isinstance(occurred_raw, str) and not occurred_raw.isdigit():
        occurred_at = datetime.fromisoformat(occurred_raw).replace(tzinfo=UTC)
    else:
        occurred_at = _ms_to_dt(int(occurred_raw))
    return NormalizedEvent(
        venue_ref=f"withdraw:{w.get('id') or w['txId']}",
        kind=LegKind.WITHDRAWAL,
        legs=_merge_legs(rows),
        occurred_at=occurred_at,
        description=f"Withdrawal {w.get('network', '')}".strip(),
    )


def _convert_event(c: dict[str, Any], decimals_for) -> NormalizedEvent:
    from_asset, to_asset = c["fromAsset"], c["toAsset"]
    from_decimals, to_decimals = decimals_for(from_asset), decimals_for(to_asset)
    rows: list[tuple[str, LegKind, int, int]] = [
        (from_asset, LegKind.CONVERT, -to_atoms(c["fromAmount"], from_decimals), from_decimals),
        (to_asset, LegKind.CONVERT, to_atoms(c["toAmount"], to_decimals), to_decimals),
    ]
    return NormalizedEvent(
        venue_ref=f"convert:{c.get('orderId') or c.get('quoteId')}",
        kind=LegKind.CONVERT,
        legs=_merge_legs(rows),
        occurred_at=_ms_to_dt(int(c["createTime"])),
        description=f"Convert {from_asset}->{to_asset}",
    )


def _dust_event(batch: dict[str, Any], decimals_for) -> NormalizedEvent:
    """One dust-conversion batch: several small assets swept into one BNB
    credit at `operateTime`. `_merge_legs` collapses any source asset that
    appears more than once in `userAssetDribbletDetails` (same batch)."""
    details = batch.get("userAssetDribbletDetails", [])
    rows: list[tuple[str, LegKind, int, int]] = []
    for row in details:
        asset = row["fromAsset"]
        decimals = decimals_for(asset)
        rows.append((asset, LegKind.CONVERT, -to_atoms(row["amount"], decimals), decimals))
    credited = batch.get("totalTransferedAmount") or batch.get("transferedAmount")
    if credited:
        rows.append(
            (
                BNB_ASSET,
                LegKind.CONVERT,
                to_atoms(credited, decimals_for(BNB_ASSET)),
                decimals_for(BNB_ASSET),
            )
        )
    return NormalizedEvent(
        venue_ref=f"dust:{batch.get('transId') or batch.get('operateTime')}",
        kind=LegKind.CONVERT,
        legs=_merge_legs(rows),
        occurred_at=_ms_to_dt(int(batch["operateTime"])),
        description="Dust conversion",
    )


# ---- public market data --------------------------------------------------


class BinanceUSMarketData(AssetMarketData):
    venue = "binanceus"

    def __init__(self, base_url: str, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=15.0)
        self._instrument_cache: dict[str, _Instrument] | None = None

    def _get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        self.throttle(_throttle_name(endpoint))
        resp = self.client.get(f"{self.base_url}{endpoint}", params=params or {})
        used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
        self.throttle_after(
            _throttle_name(endpoint), used=int(used) if used and used.isdigit() else None
        )
        _raise_if_rate_limited(resp, endpoint)
        resp.raise_for_status()
        return resp.json()

    def _instruments(self) -> dict[str, _Instrument]:
        if self._instrument_cache is None:
            self._instrument_cache = _fetch_instrument_map(self._get_json, EP_EXCHANGE_INFO)
        return self._instrument_cache

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        return [
            NormalizedInstrument(
                symbol=inst.symbol,
                type=InstrumentType.SPOT,
                base_asset=inst.base_asset,
                quote_asset=inst.quote_asset,
                qty_decimals=inst.base_decimals,
                qty_step_atoms=inst.qty_step_atoms,
                min_qty_atoms=inst.min_qty_atoms,
                min_notional=inst.min_notional,
            )
            for inst in self._instruments().values()
        ]

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        start_ms = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp() * 1000)
        end_ms = int(
            datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=UTC).timestamp() * 1000
        )
        out: list[DailyClose] = []
        cursor_ms = start_ms
        while cursor_ms <= end_ms:
            raw = self._get_json(
                EP_KLINES,
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": cursor_ms,
                    "endTime": end_ms,
                    "limit": 1000,
                },
            )
            if not raw:
                break
            for bar in raw:
                open_time_ms = int(bar[0])
                out.append(
                    DailyClose(
                        symbol=symbol,
                        day=_ms_to_dt(open_time_ms).date(),
                        close=str(bar[4]),
                    )
                )
            last_open = int(raw[-1][0])
            if last_open <= cursor_ms:
                break
            cursor_ms = last_open + DAY_MS
            if len(raw) < 1000:
                break
        return out

    def fetch_quote(self, symbol: str) -> Quote | None:
        try:
            data = self._get_json(EP_BOOK_TICKER, {"symbol": symbol})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 400:
                return None
            raise
        if not isinstance(data, dict) or "bidPrice" not in data:
            return None
        return Quote(
            symbol=symbol,
            bid=str(data.get("bidPrice")),
            ask=str(data.get("askPrice")),
            last=None,
            as_of=datetime.now(UTC),
        )
