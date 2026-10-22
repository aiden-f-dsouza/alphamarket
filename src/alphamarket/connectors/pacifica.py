"""Pacifica connector (Solana perp order book, watch only).

Pacifica is an on-chain order book whose per-account history is public: every
account is a Solana address and every read is an unauthenticated GET, so Koine
holds no credential (CredentialKind.ADDRESS, ReadOnlyProof.NONE). Ownership is
proved by an ed25519 signature over a Koine claim message (claim_message /
verify_claim below), checked with connectors.addresses.verify_ed25519.

Payload shapes are VERIFIED against public captures from 2026-10-22
(tests/fixtures/pacifica/, provenance in its README) and the docs
(https://docs.pacifica.fi/api-documentation/api). What matters:

  /trades/history      cursor paged, NEWEST first (`next_cursor`, `has_more`),
                       `history_id` is the unique id. side open_long /
                       close_long / open_short / close_short; event_type
                       fulfill_taker / fulfill_maker; `price` is the fill
                       price (the docs label it the other way round),
                       `entry_price` the position's average entry after the
                       fill. `pnl` is NET OF THE FEE: pnl = gross - fee, so an
                       open fill has pnl = -fee and the REALIZED_PNL leg is
                       pnl + fee (docs/venues/pacifica.md "Mapping").
  /funding/history     newest first, hourly, `payout` is the signed cash effect.
  /account/balance/    newest first, NO id field. deposit and deposit_release
  history              (a large deposit is credited in two parts), withdraw,
                       subaccount_transfer, payout. `balance` is the cash right
                       after the row INCLUDING trading booked since the last
                       row, so it is never used as balance_after_atoms.
  /account, /positions balance excludes unrealized PnL and isolated margin;
                       account_equity = balance + uPnL + isolated margin.
  /leaderboard         one document, all windows, `privacy_mode` per row.

Never send a body or a Content-Length on a GET: CloudFront answers 403.

Precision: perp quantities use the symbol's lot-size decimals (/info), USDC
uses 6. A value finer than that keeps its own precision up to 12 dp instead of
being rounded.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from alphamarket.connectors.addresses import (
    AddressKind,
    InvalidAddress,
    normalize,
    verify_ed25519,
)
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

ADDRESS_KIND = AddressKind.SOLANA
BASE_URLS = {"prod": "https://api.pacifica.fi/api/v1"}
BASE_URL = BASE_URLS["prod"]
# Docs "Websocket" (checked live 2026-10-22): one universal endpoint.
WS_URL = "wss://ws.pacifica.fi/ws"
# Docs "Rate Limits": at most 100 concurrent connections per IP; keep headroom.
WS_USERS_PER_IP = 90

USDC = "USDC"
USDC_DECIMALS = 6
FALLBACK_DECIMALS = 8  # a symbol /info no longer lists: widened per value
# One heavy history page costs the same 9 credits whatever its size (measured
# 2026-10-22 with limit 100, 500 and 4000; the server caps a page at 4000), so
# pages are large. Fill rows are ~400 bytes.
PAGE_LIMIT = 1000
# An incremental sync re-reads the last OVERLAP_MS before its high-water mark,
# so a row the venue stamped slightly out of order is not missed; ledger ids
# dedupe the repeats.
OVERLAP_MS = 10 * 60 * 1000
DEFAULT_RETRY_AFTER = 10.0

# ---- rate budget ------------------------------------------------------------
# https://docs.pacifica.fi/api-documentation/api/rate-limits (read 2026-10-22):
# 100 credits per 60 s rolling window for an unidentified IP (a config key,
# which Koine does not use, gives 300). Costs MEASURED from the `ratelimit`
# response header on 2026-10-22 (tenths of a credit): the leaderboard, every
# history page and the funding-rate history 9; kline 2; account, positions,
# info, prices, book and fees 1. Heavy calls are charged rounded up.
RATE_BUDGET = RateBudget(
    scope=RateScope.IP,
    capacity=100,
    refill_per_minute=100,
    costs=(
        ("leaderboard", 10),
        ("trades_history", 10),
        ("funding_history", 10),
        ("balance_history", 10),
        ("funding_rate_history", 10),
        ("kline", 3),
    ),
    default_cost=1,
)

# ---- trade vocabulary ---------------------------------------------------------

# side -> (action, sign of the position-quantity change)
SIDES = {
    "open_long": (Action.BUY, 1),
    "close_short": (Action.BUY, 1),
    "open_short": (Action.SELL, -1),
    "close_long": (Action.SELL, -1),
}
LIQUIDITY = {"fulfill_taker": "taker", "fulfill_maker": "maker"}
# `cause` values that are position-changing fills (docs, Get Trade History and
# the account_trades stream). Recorded 2026-10-22 (fixtures
# trades_history_liquidation_* and balance_history_liquidation_*): for market and
# backstop liquidations and settlement the fill's `pnl` is exactly the cash the
# balance history shows for it (a liquidation's `pnl` already carries the venue's
# liquidation penalty, about 0.7% of notional beyond gross - fee), so they are
# booked like any fill. insolvency_liquidation is documented, not recorded
# [VERIFY]. Everything else (game_settlement, fulfill_rfq, an unknown value) is
# flagged, never guessed.
FILL_CAUSES = frozenset(
    {
        "normal",
        "market_liquidation",
        "backstop_liquidation",
        "insolvency_liquidation",
        "settlement",
    }
)

# A backstop liquidation also forfeits whatever cash is left in the account: a
# second `backstop_liquidation` balance event (recorded: -125.72372 for the fill's
# pnl, then -146.762287 taking the balance to 0) that the default balance history
# does not return. The fill books, and the missing cash is flagged. Insolvency
# liquidations are treated the same way until a recording shows otherwise.
FORFEIT_CAUSES = frozenset({"backstop_liquidation", "insolvency_liquidation"})

# Balance-history event types (docs "Get Account Balance History").
FLOW_DEPOSITS = frozenset({"deposit", "deposit_release"})
# Rows only returned with include_trades=true, which Koine never requests: the
# fills and funding streams already carry them.
COVERED_ELSEWHERE = frozenset(
    {"trade", "funding", "market_liquidation", "backstop_liquidation", "adl_liquidation"}
)


# ---- small helpers ------------------------------------------------------------


def _s(value: Any) -> str:
    """Venue decimal as a clean string. Pacifica sends decimal strings; a bare
    float would already have lost precision, so it is refused."""
    if isinstance(value, bool | float):
        raise TypeError(f"expected a decimal string, got {value!r}")
    return str(value).strip()


def _dec(value: Decimal) -> str:
    return format(value, "f")  # never exponent notation


def _places(value: str) -> int:
    exponent = Decimal(value).normalize().as_tuple().exponent
    return max(0, -int(exponent))


def atoms(value: Any, decimals: int) -> tuple[int, int]:
    """(atoms, decimals) for a venue decimal string at the asset's scale. A
    value finer than `decimals` keeps its own precision (<= 12 dp)."""
    text = _s(value)
    used = max(decimals, _places(text))
    if used > MAX_DECIMALS:
        raise ValueError(f"{text!r} is finer than {MAX_DECIMALS} dp")
    return to_atoms(text, used), used


def _ms_to_dt(ms: int) -> datetime:
    ms = int(ms)
    return datetime.fromtimestamp(ms // 1000, UTC) + timedelta(milliseconds=ms % 1000)


def _dt_to_ms(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    delta = value.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def _now_ms() -> int:
    return _dt_to_ms(datetime.now(UTC))


def perp_asset(symbol: str) -> str:
    return f"{symbol}-PERP"


def _retry_after(resp: httpx.Response) -> float:
    """Seconds to wait after a 429: Retry-After, else the `t` (seconds until
    the credit window refreshes) of the `ratelimit` header."""
    raw = resp.headers.get("Retry-After")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    header = resp.headers.get("ratelimit") or ""
    for part in header.split(";"):
        if part.strip().startswith("t="):
            try:
                return max(0.0, float(part.strip()[2:]))
            except ValueError:
                break
    return DEFAULT_RETRY_AFTER


# ---- market metadata ----------------------------------------------------------


@dataclass(frozen=True)
class MarketInfo:
    """One /info row (https://docs.pacifica.fi/.../get-market-info)."""

    symbol: str
    kind: str  # "perpetual" | "spot"
    lot_size: str
    min_order_size: str | None
    execution_modes: tuple[str, ...]
    max_leverage: int | None

    @classmethod
    def parse(cls, row: dict[str, Any]) -> MarketInfo:
        return cls(
            symbol=str(row["symbol"]),
            kind=str(row.get("instrument_type") or "perpetual"),
            lot_size=_s(row["lot_size"]),
            min_order_size=_s(row["min_order_size"]) if row.get("min_order_size") else None,
            execution_modes=tuple(str(m) for m in row.get("execution_modes") or ()),
            max_leverage=int(row["max_leverage"]) if row.get("max_leverage") else None,
        )

    @property
    def qty_decimals(self) -> int:
        return min(_places(self.lot_size), MAX_DECIMALS)

    @property
    def copyable(self) -> bool:
        """/info carries no category. The `rfq` execution mode marks every
        real-world-asset market on 2026-10-22 (stocks, FX, metals, energy), and
        none of the crypto ones, so those are not paper-copyable until counsel
        Q3 answers (onchain_perps_plan section 5)."""
        return "rfq" not in self.execution_modes


def perp_instrument(
    symbol: str, info: MarketInfo | None, qty_decimals: int
) -> NormalizedInstrument:
    """The instrument of a perp fill or listing. A symbol /info does not list
    (delisted) is not copyable: nothing can classify it."""
    step = to_atoms(info.lot_size, qty_decimals) if info is not None else 1
    return NormalizedInstrument(
        symbol=symbol,
        type=InstrumentType.PERP,
        base_asset=perp_asset(symbol),
        quote_asset=USDC,
        qty_decimals=qty_decimals,
        qty_step_atoms=max(1, step),
        min_notional=info.min_order_size if info is not None else None,
        copyable=info.copyable if info is not None else False,
    )


# ---- transport ------------------------------------------------------------------


class PacificaError(Exception):
    """The API answered success=false (or an HTTP error with an envelope)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"pacifica {status}: {message}")
        self.status = status
        self.message = message


class AccountNotFound(ValueError):
    """Pacifica has never seen this address (404 "Account not found")."""


class _Api:
    """Shared transport and metadata cache for the connector and market data.
    Every request goes through `_get`, which charges the throttle first, sends
    NO body (CloudFront answers 403 to a GET with one) and turns 429 into
    RateLimited. (The throttle methods come from base._Throttled via the
    concrete classes.) Metadata is fetched at most once per instance; a budget
    stop on that call while mapping a page discards the page unreturned, so its
    cursor never advances and the retry refetches it."""

    base_url: str
    http: httpx.Client
    _markets: dict[str, MarketInfo] | None

    def _init_api(self, base_url: str, client: httpx.Client | None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.Client(timeout=20.0)
        self._markets = None

    def _get(self, endpoint: str, path: str, params: dict[str, Any] | None = None) -> Any:
        self.throttle(endpoint)
        resp = self.http.get(self.base_url + path, params=params)
        if resp.status_code == 429:
            raise RateLimited(_retry_after(resp), endpoint)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = None
            if resp.status_code == 404 and isinstance(body, dict) and body.get("error"):
                raise PacificaError(404, str(body["error"]))
            resp.raise_for_status()
        body = resp.json(parse_float=Decimal)
        if not isinstance(body, dict) or body.get("success") is not True:
            raise PacificaError(
                int((body or {}).get("code") or resp.status_code) if isinstance(body, dict) else 0,
                str((body or {}).get("error") if isinstance(body, dict) else "bad payload"),
            )
        return body

    def markets(self) -> dict[str, MarketInfo]:
        if self._markets is None:
            body = self._get("info", "/info")
            self._markets = {
                m.symbol: m for m in (MarketInfo.parse(r) for r in body.get("data") or [])
            }
        return self._markets

    def market(self, symbol: str) -> MarketInfo | None:
        return self.markets().get(symbol)


# ---- cursors ----------------------------------------------------------------------
#
# Pacifica pages newest first and has no "from" parameter on the funding and
# balance endpoints, so a sync walks from the newest row down until it passes
# the point the previous sync reached. Per stream the state is
#   s  stop: rows older than this (ms) are already ledgered
#   h  the newest created_at seen in this pass (set on the first page)
#   c  the venue's keyset cursor for the next (older) page
# and the resume cursor handed back on the last page keeps only the next stop:
# `h - OVERLAP_MS`, so the next sync re-reads a short overlap.


@dataclass
class _Walk:
    stop: int = 0
    hwm: int | None = None
    cursor: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"s": self.stop}
        if self.hwm is not None:
            out["h"] = self.hwm
        if self.cursor is not None:
            out["c"] = self.cursor
        return out

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> _Walk:
        data = data or {}
        return cls(
            stop=int(data.get("s", 0)),
            hwm=int(data["h"]) if data.get("h") is not None else None,
            cursor=str(data["c"]) if data.get("c") else None,
        )

    def resume(self) -> _Walk:
        """Where the next incremental sync starts: no cursor, stop a short
        overlap before the newest row seen (unchanged if the stream is empty)."""
        if self.hwm is None:
            return _Walk(stop=self.stop)
        return _Walk(stop=max(self.stop, self.hwm - OVERLAP_MS))


def _encode(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), sort_keys=True)


def _decode(cursor: str) -> dict[str, Any]:
    data = json.loads(cursor)
    if not isinstance(data, dict):
        raise ValueError("malformed Pacifica cursor")
    return data


# ---- ownership proof (ed25519 over a Koine message) ----------------------------------

CLAIM_SEPARATOR = "|"


def claim_message(address: str, code: str) -> str:
    """The exact text the trader signs with the wallet that holds the address
    (Phantom / Solflare "Sign Message" signs these UTF-8 bytes). The Koine claim
    code is the nonce: issued per account, single use. Signing it is not a
    transaction and cannot move funds. Solana addresses are case-sensitive, so
    the address is written exactly as stored."""
    return (
        "Koine ownership claim\n"
        "Venue: Pacifica\n"
        f"Address: {address}\n"
        f"Code: {code}\n"
        "Signing this message proves you control this address. It is not a "
        "transaction: it cannot trade, move funds or approve anything."
    )


def encode_claim_proof(code: str, signature: str) -> str:
    """What the claim flow passes to verify_claim: code and signature."""
    return f"{code}{CLAIM_SEPARATOR}{signature}"


# ---- connector -----------------------------------------------------------------------


class PacificaConnector(_Api, AssetVenueConnector):
    venue = "pacifica"

    def __init__(
        self, address: str, base_url: str = BASE_URL, client: httpx.Client | None = None
    ) -> None:
        self.address = normalize(ADDRESS_KIND, address)  # InvalidAddress is a ValueError
        self._init_api(base_url, client)
        self._unsupported: dict[str, UnsupportedActivity] = {}

    # ---- identity / proofs -----------------------------------------------------

    def account(self) -> dict[str, Any]:
        """GET /account. AccountNotFound when Pacifica has never seen the address."""
        try:
            body = self._get("account", "/account", {"account": self.address})
        except PacificaError as exc:
            if exc.status == 404:
                raise AccountNotFound("Pacifica has no account at this address") from None
            raise
        data = body.get("data")
        if not isinstance(data, dict):
            raise ValueError("unexpected /account payload")
        return data

    def check_connection(self) -> NormalizedBalance:
        data = self.account()
        return NormalizedBalance(
            balance_micros=to_micros(_s(data["account_equity"])),
            as_of=_ms_to_dt(data["updated_at"]) if data.get("updated_at") else datetime.now(UTC),
        )

    def check_write_scope(self) -> bool | None:
        return False  # no credential is held, so nothing can write

    def prove_read_only(self) -> ReadOnlyEvidence:
        """Nothing to prove: Koine stores only the public address."""
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
        """Contract hook (OwnershipProof.SIGNED_MESSAGE)."""
        return claim_message(self.address, code)

    def claim_payload(self, code: str, signature: str) -> str:
        """Contract hook: what verify_claim reads."""
        return encode_claim_proof(code, signature)

    def verify_claim(self, code: str) -> bool | None:
        """`code` is encode_claim_proof(claim_code, signature). A bare code (no
        signature yet) is False. The claim code inside `code` is the nonce and
        is NOT checked here: the caller must build `code` from the account's
        STORED claim code, never one the user submits, or an old signature over
        another code could be replayed. No network call, so never None."""
        claim, sep, signature = code.rpartition(CLAIM_SEPARATOR)
        if not sep or not claim or not signature:
            return False
        return verify_ed25519(self.address, claim_message(self.address, claim), signature)

    # ---- unsupported activity ---------------------------------------------------

    def _flag(self, item: UnsupportedActivity) -> None:
        if item.venue_ref not in self._unsupported:
            logger.warning(
                "pacifica: skipped %s %s (%s)", item.activity_type, item.venue_ref, item.reason
            )
        self._unsupported[item.venue_ref] = item

    def drain_unsupported(self) -> list[UnsupportedActivity]:
        """Contract hook: the page runner persists these per page."""
        items = sorted(self._unsupported.values(), key=lambda a: (a.occurred_at, a.venue_ref))
        self._unsupported.clear()
        return items

    # ---- fills -------------------------------------------------------------------

    def _walk_page(
        self, endpoint: str, path: str, walk: _Walk
    ) -> tuple[list[dict[str, Any]], _Walk, bool]:
        """One request. Returns (rows at or after the stop, newest first;
        the walk advanced one page; whether the pass is finished)."""
        params: dict[str, Any] = {"account": self.address, "limit": PAGE_LIMIT}
        if walk.cursor:
            params["cursor"] = walk.cursor
        body = self._get(endpoint, path, params)
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError(f"unexpected {path} payload")
        times = [int(r["created_at"]) for r in rows]
        newest = max(times, default=None)
        hwm = walk.hwm if newest is None else max(newest, walk.hwm or 0)
        wanted = [r for r, t in zip(rows, times, strict=True) if t >= walk.stop]
        next_cursor = body.get("next_cursor")
        passed_stop = bool(times) and min(times) < walk.stop
        done = not body.get("has_more") or not next_cursor or passed_stop
        return wanted, _Walk(walk.stop, hwm, None if done else str(next_cursor)), done

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        if cursor is not None:
            walk = _Walk.from_json(_decode(cursor))
        else:
            walk = _Walk(stop=_dt_to_ms(since) if since else 0)
        rows, after, done = self._walk_page("trades_history", "/trades/history", walk)
        rows.sort(key=lambda r: (int(r["created_at"]), int(r["history_id"])))
        items = [f for f in (self.to_fill(r) for r in rows) if f is not None]
        if done:
            return Page(items, resume_cursor=_encode(after.resume().to_json()))
        return Page(items, next_cursor=_encode(after.to_json()))

    def to_fill(self, r: dict[str, Any], *, flag: bool = True) -> NormalizedAssetFill | None:
        """One REST row (or a stream row renamed to the REST shape) as a fill.
        None, with an UnsupportedActivity queued when `flag`, for a row that is
        not a perp fill Koine can book: a cause outside FILL_CAUSES, or a spot
        fill. An event_type or side outside the documented set raises: it is
        never defaulted, so an unknown value cannot silently change the ledger."""
        fill_id = str(r["history_id"])
        symbol = str(r["symbol"])
        occurred = _ms_to_dt(r["created_at"])
        cause = r.get("cause")
        info = self.market(symbol)

        def skip(activity: str, reason_text: str, ref: str | None = None) -> None:
            if flag:
                self._flag(
                    UnsupportedActivity(
                        venue_ref=ref or f"fill:{fill_id}",
                        activity_type=activity,
                        symbol=symbol,
                        occurred_at=occurred,
                        reason="unsupported_type",
                        description=reason_text,
                    )
                )

        if cause not in FILL_CAUSES:
            skip(f"cause:{cause}", f"{symbol} fill with cause {cause!r}")
            return None
        if (
            r.get("spot_fee") is not None
            or r.get("_instrument_type") not in (None, 0)
            or (info is not None and info.kind != "perpetual")
        ):
            skip("spot_fill", f"{symbol} spot fill")
            return None
        side = str(r["side"])
        if side not in SIDES:
            raise ValueError(f"unknown Pacifica trade side {side!r}")
        event_type = str(r["event_type"])
        if event_type not in LIQUIDITY:
            raise ValueError(f"unknown Pacifica trade event_type {event_type!r}")
        action, sign = SIDES[side]
        qty, qty_dp = atoms(r["amount"], info.qty_decimals if info else FALLBACK_DECIMALS)
        # `pnl` is net of the fee (pnl = gross - fee; an open fill reports
        # pnl = -fee), so the realized profit is pnl + fee and the FEE leg is
        # -fee: together they sum to the venue's pnl exactly. A negative fee (a
        # maker rebate) is a positive FEE leg and the same arithmetic holds.
        fee = Decimal(_s(r.get("fee") or "0"))
        pnl = Decimal(_s(r.get("pnl") or "0"))
        realized = pnl + fee
        legs = [
            NormalizedLeg(
                asset=perp_asset(symbol),
                delta_atoms=sign * qty,
                decimals=qty_dp,
                kind=LegKind.FILL_BASE,
                instrument_symbol=symbol,
            )
        ]
        if realized:
            value, dp = atoms(_dec(realized), USDC_DECIMALS)
            legs.append(
                NormalizedLeg(
                    asset=USDC,
                    delta_atoms=value,
                    decimals=dp,
                    kind=LegKind.REALIZED_PNL,
                    instrument_symbol=symbol,
                )
            )
        if fee:
            value, dp = atoms(_dec(-fee), USDC_DECIMALS)
            legs.append(
                NormalizedLeg(
                    asset=USDC,
                    delta_atoms=value,
                    decimals=dp,
                    kind=LegKind.FEE,
                    instrument_symbol=symbol,
                )
            )
        if cause in FORFEIT_CAUSES:  # the fill books; the forfeited cash is flagged
            skip(
                f"{cause}_cash",
                f"{symbol} {cause}: the cash left in the account is forfeited and is not in "
                "the public balance history",
                ref=f"forfeit:{fill_id}",
            )
        return NormalizedAssetFill(
            venue_fill_id=fill_id,
            order_id=str(r["order_id"]) if r.get("order_id") is not None else None,
            instrument=perp_instrument(symbol, info, qty_dp),
            action=action,
            qty_atoms=qty,
            price=_s(r["price"]),
            legs=legs,
            filled_at=occurred,
            liquidity=LIQUIDITY[event_type],
        )

    def stream_source(self) -> PacificaFillStream:
        """P7: the `account_trades` subscription for this address."""
        return PacificaFillStream(self, WS_URL)

    # ---- events: funding stream, then balance-history stream ---------------------------

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        """Two venue streams behind one cursor: /funding/history pages first,
        then /account/balance/history pages, each one request per page. The
        final page's resume_cursor restarts both where they stopped."""
        if cursor is not None:
            state = _decode(cursor)
        else:
            start = _dt_to_ms(since) if since else 0
            state = {"p": "f", "f": {"s": start}, "b": {"s": start}}
        phase = str(state.get("p", "f"))
        if phase == "f":
            endpoint, path = "funding_history", "/funding/history"
        else:
            endpoint, path = "balance_history", "/account/balance/history"
        walk = _Walk.from_json(state.get(phase))
        rows, after, done = self._walk_page(endpoint, path, walk)
        if phase == "f":
            rows.sort(key=lambda r: (int(r["created_at"]), int(r["history_id"])))
            events = [self.to_funding_event(r) for r in rows]
        else:
            # No id, so ties keep the venue's order reversed (newest first on the wire).
            rows = sorted(reversed(rows), key=lambda r: int(r["created_at"]))
            events = [e for e in (self.to_balance_event(r) for r in rows) if e is not None]
        new_state = {**state, phase: after.to_json()}
        if not done:
            return Page(events, next_cursor=_encode(new_state))
        new_state[phase] = after.resume().to_json()
        if phase == "f":
            return Page(events, next_cursor=_encode({**new_state, "p": "b"}))
        resume = {"p": "f", "f": new_state["f"], "b": new_state["b"]}
        return Page(events, resume_cursor=_encode(resume))

    def to_funding_event(self, r: dict[str, Any]) -> NormalizedEvent:
        """`payout` is the signed cash effect (negative when paid); the same
        signed amount is on the venue's balance history."""
        symbol = str(r["symbol"])
        amount, dp = atoms(r["payout"], USDC_DECIMALS)
        return NormalizedEvent(
            venue_ref=str(r["history_id"]),
            kind=LegKind.FUNDING,
            legs=[
                NormalizedLeg(
                    asset=USDC,
                    delta_atoms=amount,
                    decimals=dp,
                    kind=LegKind.FUNDING,
                    instrument_symbol=symbol,
                )
            ],
            occurred_at=_ms_to_dt(r["created_at"]),
            description=(
                f"funding {symbol} rate {_s(r.get('rate', ''))} "
                f"{'long' if r.get('side') == 'bid' else 'short'} {_s(r.get('amount', ''))}"
            ),
        )

    def to_balance_event(self, r: dict[str, Any]) -> NormalizedEvent | None:
        """A balance-history row (no id field: the ref is built from the row's
        own venue values, which are stable). None for rows the fills and
        funding streams already carry."""
        kind_name = str(r["event_type"])
        amount_text = _s(r["amount"])
        ref = f"balance:{int(r['created_at'])}:{kind_name}:{amount_text}:{_s(r['balance'])}"
        occurred = _ms_to_dt(r["created_at"])
        if kind_name in COVERED_ELSEWHERE:
            return None
        amount = Decimal(amount_text)
        if kind_name in FLOW_DEPOSITS:
            kind = LegKind.DEPOSIT  # credited part, or the release of a pending part
        elif kind_name == "withdraw":
            kind = LegKind.WITHDRAWAL
        elif kind_name in ("subaccount_transfer", "payout"):
            kind = LegKind.TRANSFER_IN if amount >= 0 else LegKind.TRANSFER_OUT
        elif kind_name == "loan_interest_payout":
            kind = LegKind.INTEREST
        else:
            self._flag(
                UnsupportedActivity(
                    venue_ref=ref,
                    activity_type=kind_name,
                    occurred_at=occurred,
                    reason="unsupported_type",
                    description=f"balance event {kind_name} amount {amount_text}",
                )
            )
            return None
        value, dp = atoms(amount_text, USDC_DECIMALS)
        return NormalizedEvent(
            venue_ref=ref,
            kind=kind,
            legs=[NormalizedLeg(asset=USDC, delta_atoms=value, decimals=dp, kind=kind)],
            occurred_at=occurred,
            description=f"{kind_name} pending {_s(r.get('pending_balance') or '0')}",
        )

    # ---- balances ---------------------------------------------------------------

    def fetch_balances(self) -> NormalizedBalances:
        """Balance vector per base.NormalizedBalances.

        Cash USDC = `balance` (before settlement of unrealized PnL) plus the
        margin held in isolated positions: the venue's equity is balance + uPnL
        + isolated margin (verified on live accounts 2026-10-22), so isolated
        margin is still the trader's cash and the ledger replay includes it.
        Positions are "<SYM>-PERP", signed by side, with the average entry.
        equity_usd_micros = account_equity. A non-empty spot balance is not
        modelled: it is flagged and left out of the vector."""
        account = self.account()
        positions_body = self._get("positions", "/positions", {"account": self.address})
        held = positions_body.get("data")
        if not isinstance(held, list):
            raise ValueError("unexpected /positions payload")
        as_of = _ms_to_dt(account["updated_at"]) if account.get("updated_at") else datetime.now(UTC)
        cash = Decimal(_s(account["balance"]))
        positions: list[AssetAmount] = []
        for p in held:
            side = str(p["side"])
            if side not in ("bid", "ask"):
                raise ValueError(f"unknown Pacifica position side {side!r}")
            if p.get("isolated") and p.get("margin"):
                cash += Decimal(_s(p["margin"]))
            symbol = str(p["symbol"])
            info = self.market(symbol)
            qty, dp = atoms(p["amount"], info.qty_decimals if info else FALLBACK_DECIMALS)
            if qty:
                positions.append(
                    AssetAmount(
                        asset=perp_asset(symbol),
                        atoms=qty if side == "bid" else -qty,
                        decimals=dp,
                        entry_price=_s(p["entry_price"]) if p.get("entry_price") else None,
                    )
                )
        for spot in account.get("spot_balances") or []:
            if Decimal(_s(spot.get("amount") or "0")):
                self._flag(
                    UnsupportedActivity(
                        venue_ref=f"spot:{spot['symbol']}",
                        activity_type="spot_balance",
                        symbol=str(spot["symbol"]),
                        occurred_at=as_of,
                        reason="unsupported_type",
                        description=f"spot balance {_s(spot['amount'])} {spot['symbol']}",
                    )
                )
        assets: list[AssetAmount] = []
        if cash:
            value, dp = atoms(_dec(cash), USDC_DECIMALS)
            assets.append(AssetAmount(asset=USDC, atoms=value, decimals=dp))
        return NormalizedBalances(
            as_of=as_of,
            assets=assets + positions,
            equity_usd_micros=to_micros(_s(account["account_equity"])),
        )


# ---- public leaderboard (copy strategies step 7) -----------------------------------

# LeaderboardPeriod -> (pnl field, volume field) on a /leaderboard row.
BOARD_FIELDS = {
    LeaderboardPeriod.DAY: ("pnl_1d", "volume_1d"),
    LeaderboardPeriod.WEEK: ("pnl_7d", "volume_7d"),
    LeaderboardPeriod.MONTH: ("pnl_30d", "volume_30d"),
    LeaderboardPeriod.ALL: ("pnl_all_time", "volume_all_time"),
}
# Rows fetch_leaderboards returns per period when no caller says.
LEADERBOARD_KEEP = 1000


# ---- public market data ------------------------------------------------------------


class PacificaMarketData(_Api, AssetMarketData):
    """Public reads: instruments (/info), daily closes (/kline), top of book
    (/book), hourly funding history, margin tiers, the leaderboard and its
    privacy flag. Symbols are Pacifica's: "BTC" is the perp."""

    venue = "pacifica"

    def __init__(
        self,
        base_url: str = BASE_URL,
        client: httpx.Client | None = None,
        leaderboard_keep: int = LEADERBOARD_KEEP,
        leaderboard_min_account_value: str | None = None,
    ) -> None:
        self._init_api(base_url, client)
        self.leaderboard_keep = leaderboard_keep
        self.leaderboard_min_account_value = leaderboard_min_account_value
        self._private: frozenset[str] | None = None

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        return [
            perp_instrument(m.symbol, m, m.qty_decimals)
            for m in self.markets().values()
            if m.kind == "perpetual"
        ]

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """UTC daily candles; the still-open candle of today is not a close and
        is dropped (its `T` end time is in the future)."""
        start_ms = _dt_to_ms(datetime(start.year, start.month, start.day, tzinfo=UTC))
        end_day = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
        body = self._get(
            "kline",
            "/kline",
            {
                "symbol": symbol,
                "interval": "1d",
                "start_time": start_ms,
                "end_time": _dt_to_ms(end_day) - 1,
            },
        )
        now = _now_ms()
        out = []
        for c in sorted(body.get("data") or [], key=lambda c: int(c["t"])):
            day = _ms_to_dt(c["t"]).date()
            if int(c["T"]) >= now or not start <= day <= end:
                continue
            out.append(DailyClose(symbol=symbol, day=day, close=_s(c["c"])))
        return out

    def fetch_margin_tiers(self, symbol: str) -> list[MarginTier]:
        """One tier from the market's max leverage (/info). Pacifica documents
        the maintenance margin as 50% of the initial margin, 1 / (2 x leverage),
        the convention MarginTier assumes; its dynamic margin adjustment when
        open interest is high is not modelled [VERIFY]."""
        info = self.market(symbol)
        if info is None or info.max_leverage is None:
            return []
        return [MarginTier(lower_bound="0", max_leverage=info.max_leverage)]

    def fetch_quote(self, symbol: str) -> Quote | None:
        body = self._get("book", "/book", {"symbol": symbol})
        data = body.get("data") or {}
        levels = (data.get("l") or [[], []]) + [[], []]
        bids, asks = levels[0], levels[1]
        bid = _s(bids[0]["p"]) if bids else None
        ask = _s(asks[0]["p"]) if asks else None
        last = None
        if bid is None and ask is None:
            prices = self._get("prices", "/info/prices").get("data") or []
            last = next((_s(p["mark"]) for p in prices if p.get("symbol") == symbol), None)
            if last is None:
                return None
        as_of = _ms_to_dt(data["t"]) if data.get("t") else datetime.now(UTC)
        return Quote(symbol=symbol, bid=bid, ask=ask, last=last, as_of=as_of)

    def fetch_funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingRate]:
        """Hourly funding rates in [start, end], oldest first. The endpoint pages
        newest first (4,000 rows is about 5.5 months), so the walk stops once a
        page reaches back past `start`."""
        start_ms, end_ms = _dt_to_ms(start), _dt_to_ms(end)
        out: list[FundingRate] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"symbol": symbol, "limit": 4000}
            if cursor:
                params["cursor"] = cursor
            body = self._get("funding_rate_history", "/funding_rate/history", params)
            rows = body.get("data") or []
            for r in rows:
                at = int(r["created_at"])
                if start_ms <= at <= end_ms:
                    out.append(
                        FundingRate(
                            symbol=symbol,
                            at=_ms_to_dt(at),
                            rate=_s(r["funding_rate"]),
                            mark_price=_s(r["oracle_price"]) if r.get("oracle_price") else None,
                        )
                    )
            reached_start = bool(rows) and min(int(r["created_at"]) for r in rows) < start_ms
            cursor = body.get("next_cursor")
            if not body.get("has_more") or not cursor or reached_start:
                break
        out.sort(key=lambda f: f.at)
        return out

    # ---- leaderboard and the venue's privacy flag --------------------------------

    def _board(self) -> list[dict[str, Any]]:
        body = self._get("leaderboard", "/leaderboard")
        rows = body.get("data")
        if not isinstance(rows, list):
            raise ValueError("unexpected /leaderboard payload")
        return rows

    def fetch_leaderboards(
        self, periods: tuple[LeaderboardPeriod, ...]
    ) -> dict[LeaderboardPeriod, list[LeaderboardEntry]]:
        """The top of the public leaderboard per requested period, in rank
        order. One GET serves every window.

        Row shape (VERIFIED 2026-10-22; tests/fixtures/pacifica/README.md):
        address, username, pnl_1d / pnl_7d / pnl_30d / pnl_all_time,
        volume_1d / 7d / 30d / all_time, equity_current, oi_current,
        privacy_mode (decimal strings). `username` is DROPPED here: no identity
        field ever leaves the connector. A row with `privacy_mode` true is the
        owner's own opt-out: it is dropped from every period, and its
        normalized address is kept for private_addresses(). The board has no
        rank and no roi: rank = 1-based position among the PUBLIC rows after
        sorting the period by pnl descending (ties keep document order), and
        roi is None. A period returns the best `leaderboard_keep` rows by pnl
        among rows whose account value is at least
        `leaderboard_min_account_value` and that traded in the period; ranks
        still count every public row. Figures are rounded to 6 dp
        (leaderboard_figure), never ledger money. A malformed row is skipped."""
        wanted = [p for p in BOARD_FIELDS if p in periods]
        if not wanted:
            return {}
        floor = (
            to_micros(self.leaderboard_min_account_value)
            if self.leaderboard_min_account_value is not None
            else None
        )
        private: set[str] = set()
        public: list[tuple[str, str, dict[LeaderboardPeriod, tuple[str, str]]]] = []
        skipped = 0
        for row in self._board():
            try:
                address = normalize(ADDRESS_KIND, _s(row["address"]))
                if row.get("privacy_mode") is True:
                    private.add(address)
                    continue
                account_value = leaderboard_figure(_s(row["equity_current"]))
                figures = {
                    p: (
                        leaderboard_figure(_s(row[pnl_key])),
                        leaderboard_figure(_s(row[vol_key])),
                    )
                    for p in wanted
                    for pnl_key, vol_key in [BOARD_FIELDS[p]]
                }
            except (InvalidAddress, KeyError, TypeError, ValueError, ArithmeticError):
                skipped += 1
                continue
            public.append((address, account_value, figures))
        if skipped:
            logger.warning("pacifica leaderboard: skipped %d malformed rows", skipped)
        self._private = frozenset(private)
        out: dict[LeaderboardPeriod, list[LeaderboardEntry]] = {}
        for period in wanted:
            ranked = sorted(  # stable: ties keep document order
                enumerate(public), key=lambda item: -to_micros(item[1][2][period][0])
            )
            entries: list[LeaderboardEntry] = []
            for rank, (_, (address, account_value, figures)) in enumerate(ranked, start=1):
                pnl, volume = figures[period]
                eligible = (floor is None or to_micros(account_value) >= floor) and (
                    to_micros(volume) > 0
                )
                if not eligible:
                    continue
                entries.append(
                    LeaderboardEntry(
                        address=address,
                        rank=rank,
                        pnl=pnl,
                        volume=volume,
                        account_value=account_value,
                    )
                )
                if len(entries) >= self.leaderboard_keep:
                    break
            out[period] = entries
        return out

    def private_addresses(self) -> frozenset[str] | None:
        """The normalized addresses the last fetch_leaderboards saw with
        `privacy_mode` on (None before a fetch)."""
        return self._private

    def owner_private(self, address: str) -> bool | None:
        """One address's privacy flag, from one leaderboard download: True or
        False, None when the address is not on the board (the account endpoint
        carries no flag, so an absent address cannot be checked)."""
        try:
            wanted = normalize(ADDRESS_KIND, address)
        except InvalidAddress:
            return None
        for row in self._board():
            try:
                if normalize(ADDRESS_KIND, _s(row["address"])) == wanted:
                    return row.get("privacy_mode") is True
            except (InvalidAddress, KeyError, TypeError, ValueError):
                continue
        return None


# ---- P7: public websocket (StreamMode.FILLS) ------------------------------------------

# account_trades short names -> the REST /trades/history names (docs "Account
# Trades"; the frame fields are the REST fields renamed).
STREAM_FIELDS = {
    "h": "history_id",
    "i": "order_id",
    "I": "client_order_id",
    "s": "symbol",
    "p": "price",
    "o": "entry_price",
    "a": "amount",
    "te": "event_type",
    "ts": "side",
    "tc": "cause",
    "f": "fee",
    "n": "pnl",
    "t": "created_at",
}


def stream_row_to_rest(row: dict[str, Any]) -> dict[str, Any]:
    """An account_trades frame row in the shape of a REST trade row, so one
    `to_fill` normalizes both and ids and legs are identical. `it` (instrument
    type) is 0 for every perp seen; anything else is treated as a spot fill."""
    out = {rest: row.get(short) for short, rest in STREAM_FIELDS.items()}
    out["_instrument_type"] = row.get("it")
    return out


class PacificaFillStream(StreamingSource):
    """`account_trades` for one address on the public websocket (no credential).

    FILLS mode: each frame row is the REST row under short names, normalized by
    the same PacificaConnector.to_fill, so it lands with the same id
    (`history_id`) and legs as the poller's copy and ingest stores it once.

    Frames (VERIFIED live 2026-10-22; tests/fixtures/pacifica/):
      ack     {"channel":"subscribe","data":{"source":"account_trades","account":".."}}
      trades  {"channel":"account_trades","data":[{"h","i","I","u","s","p","o",
               "a","te","ts","tc","f","n","t","li","it","P"}, ...]}
      pong    {"channel":"pong"}
      error   {"code":400,"err":"Invalid subscription parameters.","t":...}
    There is NO snapshot on subscribe, so a gap after a reconnect is closed by
    the poller (history ids dedupe). The server closes a connection silent for
    60 s or alive for 24 h, so the core pings every 30 s."""

    venue = "pacifica"
    ping_interval_seconds = 30.0
    idle_timeout_seconds = 75.0  # pongs answer every ping

    def __init__(self, connector: PacificaConnector, ws_url: str = WS_URL) -> None:
        self.connector = connector
        self.ws_url = ws_url

    def url(self) -> str:
        return self.ws_url

    def subscribe_frames(self) -> list[str]:
        params = {"source": "account_trades", "account": self.connector.address}
        return [json.dumps({"method": "subscribe", "params": params})]

    def ping_frame(self) -> str | None:
        return json.dumps({"method": "ping"})

    def parse(self, frame: str | bytes) -> list[StreamEvent]:
        try:
            msg = json.loads(frame, parse_float=Decimal)
        except ValueError:
            raise StreamProtocolError("pacifica: frame is not JSON") from None
        if not isinstance(msg, dict):
            return []
        if "err" in msg:
            text = str(msg.get("err") or "")[:200]
            code = msg.get("code")
            # The per-IP connection cap and a restricted-region refusal are not
            # worth retrying soon; the account stays polled for the cool-off.
            if code in (403, 429) or "limit" in text.lower():
                raise StreamUnavailable(f"pacifica: {text}")
            raise StreamProtocolError(f"pacifica: {text}")
        if msg.get("channel") != "account_trades":
            return []  # subscribe ack, pong
        rows = [
            r
            for r in msg.get("data") or []
            if isinstance(r, dict) and r.get("u") in (None, self.connector.address)
        ]
        if not rows:
            return []
        rest = [stream_row_to_rest(r) for r in rows]
        rest.sort(key=lambda r: (int(r["created_at"]), int(r["history_id"])))
        # flag=False: the stream process never drains the queue; the poller
        # sees the same row and flags it.
        fills = [f for f in (self.connector.to_fill(r, flag=False) for r in rest) if f is not None]
        if not fills:
            return [StreamEvent(nudge=True, ref=str(rest[-1]["history_id"]))]
        return [StreamEvent(fills=fills, ref=fills[-1].venue_fill_id)]
