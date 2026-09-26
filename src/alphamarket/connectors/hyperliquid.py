"""Hyperliquid connector (perps on every perp dex, plus spot balances and fills).

Hyperliquid is an on-chain order book: every account is a public 0x address
and every read is an unauthenticated POST to the info endpoint, so Koine
holds no credential at all (CredentialKind.ADDRESS, ReadOnlyProof.NONE).
Ownership is proved by an EIP-191 `personal_sign` signature over a Koine
claim message (claim_message / verify_claim below): Hyperliquid has no
profile bio, so the Polymarket claim-code flow does not fit.

Payload shapes are VERIFIED against public captures from 2026-09-26
(tests/fixtures/hyperliquid/, provenance in its README). What matters:

  userFillsByTime   oldest first, <= 2000 rows per response, only the 10,000
                    most recent fills exist at all (history_depth
                    "10k_fills"; anchors cover the rest). `tid` is the unique
                    trade id, side "B" buy / "A" sell, sz/px decimal strings,
                    closedPnl (perps: realized P&L in the dex collateral),
                    fee + feeToken (fee INCLUDES builderFee when present; a
                    negative fee is a maker rebate). HIP-3 perps are
                    "<dex>:<COIN>"; spot pairs are "@<index>" or "PURR/USDC"
                    and resolve through spotMeta. Spot buys pay the fee in
                    the base token.
  userFunding       oldest first, <= 500 rows; one row per (time, coin);
                    delta.usdc is signed (negative = paid); hash is the zero
                    hash, so the id is f"{time}:{coin}".
  userNonFunding    oldest first, <= 500 rows; delta.type is deposit,
  LedgerUpdates     withdraw, send, spotTransfer, internalTransfer,
                    subAccountTransfer, accountClassTransfer, vault*,
                    liquidation, spotGenesis, rewardsClaim, cStakingTransfer,
                    borrowLend... (see _ledger_legs for each mapping).
  clearinghouseState per perp dex: positions (szi signed, entryPx,
                    unrealizedPnl) and marginSummary.accountValue.
  spotClearinghouseState  token totals. Under the unified-account and
                    portfolio-margin modes this is the ONLY cash truth
                    ("individual perp dex user states are not meaningful",
                    docs: Account abstraction modes), so fetch_balances asks
                    userAbstraction first.

Precision: perp positions use the coin's szDecimals (meta), spot tokens use
the token's weiDecimals (spotMeta), USDC uses 8 (its weiDecimals; perp
fields print 6, spot and transfers print up to 8). A value finer than that
(never seen) keeps its own precision up to 12 dp instead of being rounded.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
    DailyClose,
    FundingRate,
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
    RateBudget,
    RateLimited,
    RateScope,
    ReadOnlyEvidence,
    ReadOnlyProof,
)
from alphamarket.money import MAX_DECIMALS, MICROS_PER_UNIT, to_atoms

INFO_URLS = {
    "prod": "https://api.hyperliquid.xyz/info",
    "testnet": "https://api.hyperliquid-testnet.xyz/info",
}
INFO_URL = INFO_URLS["prod"]

FILLS_PAGE_LIMIT = 2000  # userFillsByTime: "at most 2000 fills per response"
TIME_RANGE_PAGE_LIMIT = 500  # userFunding / ledger updates / fundingHistory
USDC = "USDC"
USDC_DECIMALS = 8  # spotMeta weiDecimals for token 0
FALLBACK_DECIMALS = 8  # unknown coin/token (delisted from meta): widened per value
MIN_ORDER_NOTIONAL = "10"  # $10 minimum order value on perps and spot [VERIFY per HIP-3 dex]
DEFAULT_RETRY_AFTER = 10.0  # seconds when a 429 carries no Retry-After
ZERO_HASH = "0x" + "0" * 64
UNIFIED_MODES = frozenset({"unifiedAccount", "portfolioMargin"})

# ---- rate budget ------------------------------------------------------------
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
# (checked 2026-09-26): 1200 weight per minute per IP shared by every REST
# request; these info types weigh 2, userRole 60, every other info type 20.
RATE_BUDGET = RateBudget(
    scope=RateScope.IP,
    capacity=1200,
    refill_per_minute=1200,
    costs=(
        ("l2Book", 2),
        ("allMids", 2),
        ("clearinghouseState", 2),
        ("orderStatus", 2),
        ("spotClearinghouseState", 2),
        ("exchangeStatus", 2),
        ("userRole", 60),
    ),
    default_cost=20,
)
# Extra weight per N items returned. The docs say "an additional rate limit
# weight per 20 items"; whether a partial block of items counts is not
# stated, so we charge whole blocks (floor) [VERIFY].
PER_ITEM_WEIGHT = {
    "userFills": 20,
    "userFillsByTime": 20,
    "userFunding": 20,
    "fundingHistory": 20,
    "historicalOrders": 20,
    "recentTrades": 20,
    "candleSnapshot": 60,
}


def extra_weight(endpoint: str, items: int) -> int:
    """Response-dependent weight for `items` rows: floor(items / per)."""
    per = PER_ITEM_WEIGHT.get(endpoint)
    return items // per if per else 0


# ---- small helpers ------------------------------------------------------------


def is_address(value: str) -> bool:
    if len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _s(value: Any) -> str:
    """Venue decimal as a clean string. The API sends strings; the docs'
    own examples carry stray spaces (" -3.625312", " USDC")."""
    if isinstance(value, bool | float):
        raise TypeError(f"expected a decimal string, got {value!r}")
    return str(value).strip()


def _places(value: str) -> int:
    exponent = Decimal(value).normalize().as_tuple().exponent
    return max(0, -int(exponent))


def atoms(value: Any, decimals: int) -> tuple[int, int]:
    """(atoms, decimals) for a venue decimal string at the asset's scale.
    A value finer than `decimals` keeps its own precision (<= 12 dp) rather
    than being rounded; the ledger rescales per asset before summing."""
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


def _retry_after(resp: httpx.Response) -> float:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return DEFAULT_RETRY_AFTER
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def is_spot_coin(coin: str) -> bool:
    return coin.startswith("@") or "/" in coin


def perp_dex_of(coin: str) -> str:
    """HIP-3 perps are named "<dex>:<COIN>"; the first perp dex is ""."""
    return coin.split(":", 1)[0] if ":" in coin else ""


def perp_asset(coin: str) -> str:
    return f"{coin}-PERP"


# ---- metadata ---------------------------------------------------------------


@dataclass(frozen=True)
class SpotToken:
    index: int
    name: str
    sz_decimals: int
    wei_decimals: int


@dataclass(frozen=True)
class SpotPair:
    name: str  # "@107" | "PURR/USDC" (what fills and candles call the coin)
    index: int
    base: SpotToken
    quote: SpotToken


@dataclass
class SpotInfo:
    tokens: dict[int, SpotToken] = field(default_factory=dict)
    by_name: dict[str, SpotToken] = field(default_factory=dict)
    pairs: dict[str, SpotPair] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> SpotInfo:
        info = cls()
        for t in payload.get("tokens") or []:
            token = SpotToken(
                index=int(t["index"]),
                name=str(t["name"]),
                sz_decimals=int(t["szDecimals"]),
                wei_decimals=int(t["weiDecimals"]),
            )
            info.tokens[token.index] = token
            info.by_name.setdefault(token.name, token)
        for u in payload.get("universe") or []:
            base_i, quote_i = (int(i) for i in u["tokens"])
            if base_i in info.tokens and quote_i in info.tokens:
                pair = SpotPair(
                    name=str(u["name"]),
                    index=int(u["index"]),
                    base=info.tokens[base_i],
                    quote=info.tokens[quote_i],
                )
                info.pairs[pair.name] = pair
        return info


@dataclass
class PerpDexInfo:
    dex: str
    sz_decimals: dict[str, int]
    collateral_token: int  # spot token index margining this dex (0 = USDC)
    universe: list[dict[str, Any]] = field(default_factory=list)
    margin_tables: dict[int, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, dex: str, payload: dict[str, Any]) -> PerpDexInfo:
        universe = list(payload.get("universe") or [])
        return cls(
            dex=dex,
            sz_decimals={str(u["name"]): int(u["szDecimals"]) for u in universe},
            collateral_token=int(payload.get("collateralToken") or 0),
            universe=universe,
            margin_tables={int(k): v for k, v in payload.get("marginTables") or []},
        )


class _Info:
    """Shared transport + metadata cache for the connector and market data.
    Every request goes through `_info`, which charges the throttle first,
    turns 429/418 into RateLimited and charges per-item weight afterwards.
    Metadata (meta per dex, spotMeta) is fetched at most once per instance.
    A budget stop on a metadata call made while mapping a page discards that
    page unreturned, so its cursor never advances and the retry refetches
    it: nothing fetched is ever skipped. (The throttle methods come from
    base._Throttled via the concrete classes.)"""

    info_url: str
    http: httpx.Client
    _spot: SpotInfo | None
    _dexes: dict[str, PerpDexInfo]

    def _init_info(self, info_url: str, client: httpx.Client | None) -> None:
        self.info_url = info_url
        self.http = client or httpx.Client(timeout=20.0)
        self._spot = None
        self._dexes = {}

    def _info(self, body: dict[str, Any]) -> Any:
        endpoint = str(body["type"])
        self.throttle(endpoint)
        resp = self.http.post(self.info_url, json=body)
        if resp.status_code in (429, 418):
            raise RateLimited(_retry_after(resp), endpoint)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            extra = extra_weight(endpoint, len(data))
            if extra:
                self.throttle_after(endpoint, extra)
        return data

    def spot_info(self) -> SpotInfo:
        if self._spot is None:
            self._spot = SpotInfo.parse(self._info({"type": "spotMeta"}))
        return self._spot

    def perp_dex(self, dex: str) -> PerpDexInfo:
        if dex not in self._dexes:
            body: dict[str, Any] = {"type": "meta"}
            if dex:
                body["dex"] = dex
            self._dexes[dex] = PerpDexInfo.parse(dex, self._info(body))
        return self._dexes[dex]

    def token_name(self, index: int) -> str:
        if index == 0:
            return USDC
        token = self.spot_info().tokens.get(index)
        if token is None:
            raise ValueError(f"unknown spot token index {index}")
        return token.name

    def collateral_of(self, coin: str) -> str:
        """Asset that margins a perp coin (USDC on the first dex; a HIP-3
        dex may use another token, e.g. USDT0)."""
        dex = perp_dex_of(coin)
        if not dex:
            return USDC
        return self.token_name(self.perp_dex(dex).collateral_token)

    def token_decimals(self, name: str) -> int:
        if name == USDC:
            return USDC_DECIMALS
        token = self.spot_info().by_name.get(name)
        return token.wei_decimals if token else FALLBACK_DECIMALS

    def coin_decimals(self, coin: str) -> int:
        return self.perp_dex(perp_dex_of(coin)).sz_decimals.get(coin, FALLBACK_DECIMALS)


# ---- cursors ----------------------------------------------------------------


@dataclass
class _Stream:
    """Position in one time-paginated stream: next startTime (inclusive, as
    the API treats it) and the ids already emitted AT that millisecond, so a
    boundary shared by two pages is neither lost nor duplicated."""

    start: int = 0
    seen: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"s": self.start, "x": self.seen}

    @classmethod
    def from_json(cls, data: dict[str, Any] | None) -> _Stream:
        data = data or {}
        return cls(start=int(data.get("s", 0)), seen=[str(x) for x in data.get("x", [])])

    def fresh(self, rows: list[tuple[int, str, Any]]) -> list[tuple[int, str, Any]]:
        skip = set(self.seen)
        return [r for r in rows if not (r[0] == self.start and r[1] in skip)]

    def advanced(self, rows: list[tuple[int, str, Any]], full: bool) -> _Stream:
        """State after consuming `rows` (sorted by time). A full page made of
        a single millisecond cannot advance by time, so it steps 1 ms on
        (more than one page of rows in one ms never happens in practice)."""
        if not rows:
            return _Stream(self.start, list(self.seen))
        last = rows[-1][0]
        ids = [r[1] for r in rows if r[0] == last]
        if last == self.start:
            if full and rows[0][0] == last:
                return _Stream(last + 1, [])
            ids = sorted(set(self.seen) | set(ids))
        return _Stream(last, ids)


def _encode(state: dict[str, Any]) -> str:
    return json.dumps(state, separators=(",", ":"), sort_keys=True)


def _decode(cursor: str) -> dict[str, Any]:
    data = json.loads(cursor)
    if not isinstance(data, dict):
        raise ValueError("malformed Hyperliquid cursor")
    return data


# ---- ownership proof (EIP-191 personal_sign) ------------------------------------

CLAIM_SEPARATOR = "|"


def claim_message(address: str, code: str) -> str:
    """The exact text the trader signs with the wallet (personal_sign). The
    Koine claim code is the nonce: issued per account, single use. Signing
    it is not a transaction and cannot move funds."""
    return (
        "Koine ownership claim\n"
        "Venue: Hyperliquid\n"
        f"Address: {address.lower()}\n"
        f"Code: {code}\n"
        "Signing this message proves you control this address. It is not a "
        "transaction: it cannot trade, move funds or approve anything."
    )


def encode_claim_proof(code: str, signature: str) -> str:
    """What the claim flow passes to verify_claim: code and signature."""
    return f"{code}{CLAIM_SEPARATOR}{signature}"


def recover_signer(message: str, signature: str) -> str | None:
    """Lower-case address that produced an EIP-191 signature of `message`,
    or None if the signature is malformed."""
    from eth_account import Account
    from eth_account.messages import encode_defunct

    try:
        signer = Account.recover_message(encode_defunct(text=message), signature=signature)
    except Exception:  # malformed hex, bad length, invalid curve point
        return None
    return str(signer).lower()


# ---- connector --------------------------------------------------------------------


class HyperliquidConnector(_Info, AssetVenueConnector):
    venue = "hyperliquid"

    def __init__(
        self, address: str, info_url: str = INFO_URL, client: httpx.Client | None = None
    ) -> None:
        if not is_address(address):
            raise ValueError("Hyperliquid accounts are identified by a 0x address")
        self.address = address.lower()
        self._init_info(info_url, client)

    # ---- identity / proofs -----------------------------------------------------

    def user_role(self) -> dict[str, Any]:
        """{"role": "user" | "agent" | "vault" | "subAccount" | "missing", "data": ...}"""
        data = self._info({"type": "userRole", "user": self.address})
        return data if isinstance(data, dict) else {"role": str(data)}

    def check_connection(self) -> NormalizedBalance:
        """Rejects addresses Hyperliquid has never seen and agent (API)
        wallets, whose own state is always empty; then returns the M7 view
        of the balance."""
        role = self.user_role()
        kind = role.get("role")
        if kind == "missing":
            raise ValueError("Hyperliquid has no account at this address")
        if kind == "agent":
            master = (role.get("data") or {}).get("user")
            raise ValueError(f"this is an API wallet; connect the account address {master}")
        return self.fetch_balance()

    def check_write_scope(self) -> bool | None:
        return False  # no credential is held, so nothing can write

    def prove_read_only(self) -> ReadOnlyEvidence:
        """Nothing to prove: Koine stores only the public address. The
        details record which kind of address it is (a vault or sub-account
        changes who can claim it)."""
        details: dict[str, str | bool | int | None] = {
            "credential": "none (public address)",
            "address": self.address,
            "can_trade": False,
            "can_withdraw": False,
            "role": None,
        }
        try:
            role = self.user_role()
        except httpx.HTTPError as exc:
            details["role_error"] = type(exc).__name__
        else:
            details["role"] = str(role.get("role"))
            data = role.get("data") or {}
            if isinstance(data, dict):
                for key in ("master", "user"):
                    if data.get(key):
                        details["master" if key == "master" else "agent_of"] = str(data[key])
        return ReadOnlyEvidence(
            method=ReadOnlyProof.NONE,
            read_only=True,
            checked_at=datetime.now(UTC),
            details=details,
        )

    def verify_signed_claim(self, code: str, signature: str) -> bool:
        """True if `signature` is an EIP-191 signature of claim_message(address,
        code) by this address, or by the address that controls it: the
        master of a sub-account or the leader of a vault (neither has a key
        of its own)."""
        signer = recover_signer(claim_message(self.address, code), signature)
        if signer is None:
            return False
        if signer == self.address:
            return True
        role = self.user_role()
        data = role.get("data") or {}
        if role.get("role") == "subAccount":
            return str(data.get("master", "")).lower() == signer
        if role.get("role") == "vault":
            details = self._info({"type": "vaultDetails", "vaultAddress": self.address})
            return str((details or {}).get("leader", "")).lower() == signer
        return False

    def verify_claim(self, code: str) -> bool | None:
        """`code` is encode_claim_proof(claim_code, signature). A bare code
        (no signature yet) is False; None only when the venue could not be
        asked about a vault/sub-account controller."""
        claim, sep, signature = code.rpartition(CLAIM_SEPARATOR)
        if not sep or not claim or not signature:
            return False
        try:
            return self.verify_signed_claim(claim, signature)
        except httpx.HTTPError:
            return None

    # ---- fills -----------------------------------------------------------------

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        if cursor is not None:
            state = _decode(cursor)
            stream = _Stream.from_json(state.get("f"))
            end = state.get("e")
        else:
            stream = _Stream(start=_dt_to_ms(since) if since else 0)
            end = None
        end = int(end) if end is not None else _now_ms()
        rows = self._info(
            {
                "type": "userFillsByTime",
                "user": self.address,
                "startTime": stream.start,
                "endTime": end,
            }
        )
        if not isinstance(rows, list):
            raise ValueError("unexpected userFillsByTime payload")
        keyed = sorted(((int(r["time"]), fill_id(r), r) for r in rows), key=lambda k: (k[0], k[1]))
        items = [self.to_fill(r) for _, _, r in stream.fresh(keyed)]
        full = len(rows) >= FILLS_PAGE_LIMIT
        after = stream.advanced(keyed, full)
        if full:
            return Page(items, next_cursor=_encode({"f": after.to_json(), "e": end}))
        return Page(items, resume_cursor=_encode({"f": after.to_json()}))

    def to_fill(self, r: dict[str, Any]) -> NormalizedAssetFill:
        coin = str(r["coin"])
        side = str(r["side"])
        if side not in ("B", "A"):
            raise ValueError(f"unknown fill side {side!r}")
        action = Action.BUY if side == "B" else Action.SELL
        sign = 1 if action is Action.BUY else -1
        legs: list[NormalizedLeg] = []
        if is_spot_coin(coin):
            pair = self.spot_info().pairs.get(coin)
            if pair is None:
                raise ValueError(f"unknown spot pair {coin!r}")
            qty, qty_dp = atoms(r["sz"], pair.base.sz_decimals)
            base_qty, base_dp = atoms(r["sz"], pair.base.wei_decimals)
            # Exact product of the two strings; Hyperliquid prices carry at
            # most 8 - szDecimals places, so this fits the quote's 8 dp.
            quote_amount = Decimal(_s(r["sz"])) * Decimal(_s(r["px"]))
            quote_atoms, quote_dp = atoms(str(quote_amount), self.token_decimals(pair.quote.name))
            legs.append(
                NormalizedLeg(
                    asset=pair.base.name,
                    delta_atoms=sign * base_qty,
                    decimals=base_dp,
                    kind=LegKind.FILL_BASE,
                    instrument_symbol=coin,
                )
            )
            legs.append(
                NormalizedLeg(
                    asset=pair.quote.name,
                    delta_atoms=-sign * quote_atoms,
                    decimals=quote_dp,
                    kind=LegKind.FILL_QUOTE,
                    instrument_symbol=coin,
                )
            )
            instrument = spot_instrument(pair)
            instrument = instrument.model_copy(update={"qty_decimals": qty_dp})
            default_fee_token = pair.quote.name
        else:
            qty, qty_dp = atoms(r["sz"], self.coin_decimals(coin))
            collateral = self.collateral_of(coin)
            legs.append(
                NormalizedLeg(
                    asset=perp_asset(coin),
                    delta_atoms=sign * qty,
                    decimals=qty_dp,
                    kind=LegKind.FILL_BASE,
                    instrument_symbol=coin,
                )
            )
            pnl, pnl_dp = atoms(r.get("closedPnl") or "0", self.token_decimals(collateral))
            if pnl:
                legs.append(
                    NormalizedLeg(
                        asset=collateral,
                        delta_atoms=pnl,
                        decimals=pnl_dp,
                        kind=LegKind.REALIZED_PNL,
                        instrument_symbol=coin,
                    )
                )
            instrument = perp_instrument(coin, qty_dp, collateral)
            default_fee_token = collateral
        # `fee` already includes builderFee (docs: "the total fee, inclusive
        # of builderFee"), so builderFee is never added again. A negative fee
        # is a maker rebate and becomes a positive FEE leg.
        fee_token = _s(r.get("feeToken") or "") or default_fee_token
        fee, fee_dp = atoms(r.get("fee") or "0", self.token_decimals(fee_token))
        if fee:
            legs.append(
                NormalizedLeg(
                    asset=fee_token,
                    delta_atoms=-fee,
                    decimals=fee_dp,
                    kind=LegKind.FEE,
                    instrument_symbol=coin,
                )
            )
        return NormalizedAssetFill(
            venue_fill_id=fill_id(r),
            order_id=str(r["oid"]) if r.get("oid") is not None else None,
            instrument=instrument,
            action=action,
            qty_atoms=qty,
            price=_s(r["px"]),
            legs=legs,
            filled_at=_ms_to_dt(r["time"]),
            liquidity="taker" if r.get("crossed") else "maker",
        )

    # ---- events: funding stream, then ledger-update stream ----------------------

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        """Two venue streams behind one cursor: userFunding pages first, then
        userNonFundingLedgerUpdates pages, each one request per page with the
        upper bound pinned in the cursor. The final page's resume_cursor
        restarts both streams where they stopped."""
        if cursor is not None:
            state = _decode(cursor)
        else:
            start = _dt_to_ms(since) if since else 0
            state = {"p": "funding", "f": {"s": start, "x": []}, "l": {"s": start, "x": []}}
        end = int(state["e"]) if state.get("e") is not None else _now_ms()
        phase = state.get("p", "funding")
        if phase == "funding":
            key, endpoint = "f", "userFunding"
        else:
            key, endpoint = "l", "userNonFundingLedgerUpdates"
        stream = _Stream.from_json(state.get(key))
        rows = self._info(
            {"type": endpoint, "user": self.address, "startTime": stream.start, "endTime": end}
        )
        if not isinstance(rows, list):
            raise ValueError(f"unexpected {endpoint} payload")
        rows = sorted(rows, key=lambda r: int(r["time"]))
        if phase == "funding":
            events = [self.to_funding_event(r) for r in rows]
        else:
            events = self._ledger_events(rows)
        keyed = [(_dt_to_ms(e.occurred_at), e.venue_ref, e) for e in events]
        items = [e for _, _, e in stream.fresh(keyed)]
        full = len(rows) >= TIME_RANGE_PAGE_LIMIT
        after = stream.advanced(keyed, full)
        new_state = {**state, key: after.to_json(), "e": end}
        if full:
            return Page(items, next_cursor=_encode(new_state))
        if phase == "funding":
            return Page(items, next_cursor=_encode({**new_state, "p": "ledger"}))
        resume = {"p": "funding", "f": new_state["f"], "l": new_state["l"]}
        return Page(items, resume_cursor=_encode(resume))

    def to_funding_event(self, r: dict[str, Any]) -> NormalizedEvent:
        delta = r["delta"]
        coin = str(delta["coin"])
        collateral = self.collateral_of(coin)
        amount, dp = atoms(delta["usdc"], self.token_decimals(collateral))
        return NormalizedEvent(
            venue_ref=f"{int(r['time'])}:{coin}",
            kind=LegKind.FUNDING,
            legs=[
                NormalizedLeg(
                    asset=collateral,
                    delta_atoms=amount,
                    decimals=dp,
                    kind=LegKind.FUNDING,
                    instrument_symbol=coin,
                )
            ],
            occurred_at=_ms_to_dt(r["time"]),
            description=(
                f"funding {coin} rate {_s(delta.get('fundingRate', ''))} "
                f"szi {_s(delta.get('szi', ''))}"
            ),
        )

    def _ledger_events(self, rows: list[dict[str, Any]]) -> list[NormalizedEvent]:
        """Map rows, then suffix exact duplicates of one ref (#1, #2...)."""
        out: list[NormalizedEvent] = []
        seen: dict[str, int] = {}
        for r in rows:
            event = self.to_ledger_event(r)
            n = seen.get(event.venue_ref, 0)
            seen[event.venue_ref] = n + 1
            if n:
                event = event.model_copy(update={"venue_ref": f"{event.venue_ref}#{n}"})
            out.append(event)
        return out

    def to_ledger_event(self, r: dict[str, Any]) -> NormalizedEvent:
        delta = r["delta"]
        kind_name = str(delta.get("type"))
        tx = str(r.get("hash") or "")
        ref = f"{int(r['time'])}:{kind_name}:{tx}"
        if not tx or tx == ZERO_HASH:
            digest = hashlib.sha256(
                json.dumps(delta, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()[:16]
            ref = f"{ref}:{digest}"
        kind, legs, description = self._ledger_legs(delta)
        return NormalizedEvent(
            venue_ref=ref,
            kind=kind,
            legs=_merge(legs),
            occurred_at=_ms_to_dt(r["time"]),
            description=description,
        )

    def _leg(self, asset: str, amount: Any, kind: LegKind, sign: int = 1) -> NormalizedLeg:
        value, dp = atoms(amount, self.token_decimals(asset))
        return NormalizedLeg(asset=asset, delta_atoms=sign * value, decimals=dp, kind=kind)

    def _fee_legs(self, delta: dict[str, Any]) -> list[NormalizedLeg]:
        legs = []
        fee = _s(delta.get("fee") or "0")
        if Decimal(fee):
            token = _s(delta.get("feeToken") or "") or USDC
            legs.append(self._leg(token, fee, LegKind.FEE, -1))
        native = _s(delta.get("nativeTokenFee") or "0")
        if Decimal(native):
            legs.append(self._leg("HYPE", native, LegKind.FEE, -1))
        return legs

    def _ledger_legs(self, d: dict[str, Any]) -> tuple[LegKind, list[NormalizedLeg], str]:
        """(primary kind, legs, description) for one ledger update.

        Scope: the venue account is the address across the spot and every perp
        dex balance, so moving value between them (accountClassTransfer, a
        `send` to yourself between dexes) changes nothing: CONVERT, no legs.
        Value entering or leaving the address is TRANSFER_IN/OUT (or DEPOSIT/
        WITHDRAWAL for the bridge). Liquidations carry no legs: the
        liquidation fills (with their closedPnl) move the cash; the event
        records that it happened. Unknown types become an ADJUSTMENT with no
        legs, so nothing is invented and reconciliation surfaces any effect."""
        t = str(d.get("type"))
        me = self.address
        if t == "deposit":
            return LegKind.DEPOSIT, [self._leg(USDC, d["usdc"], LegKind.DEPOSIT)], "bridge deposit"
        if t == "withdraw":
            # `usdc` is what reaches the bridge; the $1 fee is charged on
            # top (9999.0 + 1.0 for a 10,000 request) [VERIFY on own account].
            legs = [self._leg(USDC, d["usdc"], LegKind.WITHDRAWAL, -1), *self._fee_legs(d)]
            return LegKind.WITHDRAWAL, legs, "bridge withdrawal"
        if t == "accountClassTransfer":
            return LegKind.CONVERT, [], f"perp<->spot class transfer toPerp={d.get('toPerp')}"
        if t in ("send", "spotTransfer", "internalTransfer", "subAccountTransfer"):
            token = _s(d.get("token") or USDC)
            amount = d.get("amount") if d.get("amount") is not None else d.get("usdc")
            sender = str(d.get("user", "")).lower()
            receiver = str(d.get("destination", "")).lower()
            route = f"{d.get('sourceDex', '')}->{d.get('destinationDex', '')}"
            if sender == me and receiver == me:
                return LegKind.CONVERT, self._fee_legs(d), f"{t} {token} {route} (same account)"
            if sender == me:
                legs = [self._leg(token, amount, LegKind.TRANSFER_OUT, -1), *self._fee_legs(d)]
                return LegKind.TRANSFER_OUT, legs, f"{t} {token} to {receiver}"
            if receiver == me:
                legs = [self._leg(token, amount, LegKind.TRANSFER_IN)]
                return LegKind.TRANSFER_IN, legs, f"{t} {token} from {sender}"
            return LegKind.ADJUSTMENT, [], f"{t} between other addresses"
        if t in ("vaultCreate", "vaultDeposit"):
            if str(d.get("vault", "")).lower() == me:
                legs = [self._leg(USDC, d["usdc"], LegKind.TRANSFER_IN)]
                return LegKind.TRANSFER_IN, legs, f"{t} into this vault"
            legs = [self._leg(USDC, d["usdc"], LegKind.TRANSFER_OUT, -1), *self._fee_legs(d)]
            return LegKind.TRANSFER_OUT, legs, f"{t} {d.get('vault')}"
        if t == "vaultWithdraw":
            if str(d.get("vault", "")).lower() == me:
                # What leaves the vault: the depositor's net plus the leader's
                # commission [VERIFY with a vault account].
                out = Decimal(_s(d["netWithdrawnUsd"])) + Decimal(_s(d.get("commission") or "0"))
                legs = [self._leg(USDC, str(out), LegKind.TRANSFER_OUT, -1)]
                return LegKind.TRANSFER_OUT, legs, "depositor withdrawal from this vault"
            legs = [self._leg(USDC, d["netWithdrawnUsd"], LegKind.TRANSFER_IN)]
            return LegKind.TRANSFER_IN, legs, f"vault withdrawal {d.get('vault')}"
        if t == "vaultDistribution":
            if str(d.get("vault", "")).lower() == me:
                legs = [self._leg(USDC, d["usdc"], LegKind.TRANSFER_OUT, -1)]
                return LegKind.TRANSFER_OUT, legs, "distribution paid by this vault"
            legs = [self._leg(USDC, d["usdc"], LegKind.TRANSFER_IN)]
            return LegKind.TRANSFER_IN, legs, f"vault distribution {d.get('vault')}"
        if t == "vaultLeaderCommission":
            legs = [self._leg(USDC, d["usdc"], LegKind.TRANSFER_IN)]
            return LegKind.TRANSFER_IN, legs, "vault leader commission"
        if t == "liquidation":
            positions = ", ".join(
                f"{p.get('coin')} {p.get('szi')}" for p in d.get("liquidatedPositions") or []
            )
            return (
                LegKind.LIQUIDATION,
                [],
                f"{d.get('leverageType')} liquidation of {positions}; "
                f"account value {d.get('accountValue')}",
            )
        if t in ("spotGenesis", "rewardsClaim"):
            token = _s(d.get("token") or USDC)
            legs = [self._leg(token, d["amount"], LegKind.TRANSFER_IN)]
            return LegKind.TRANSFER_IN, legs, f"{t} {token}"
        if t == "cStakingTransfer":
            # Staked HYPE leaves the spot balance (and fetch_balances) while
            # staked; rewards are not in this feed.
            token = _s(d.get("token") or "HYPE")
            if d.get("isDeposit"):
                legs = [self._leg(token, d["amount"], LegKind.TRANSFER_OUT, -1)]
                return LegKind.TRANSFER_OUT, legs, f"stake {token}"
            legs = [self._leg(token, d["amount"], LegKind.TRANSFER_IN)]
            return LegKind.TRANSFER_IN, legs, f"unstake {token}"
        if t == "borrowLend":
            # Supplied tokens leave the spot balance (observed 2026-09-26:
            # 19.986 HYPE supplied, 0.0059 left); borrowed tokens arrive.
            # Interest is folded into the flow amounts, never a return
            # [VERIFY interestAmount against a repaid loan].
            token = _s(d.get("token") or USDC)
            op = str(d.get("operation"))
            outflow = op in ("supply", "repay")
            if op not in ("supply", "repay", "withdraw", "borrow"):
                return LegKind.ADJUSTMENT, [], f"borrowLend {op} {token} (unmapped)"
            kind = LegKind.TRANSFER_OUT if outflow else LegKind.TRANSFER_IN
            legs = [self._leg(token, d["amount"], kind, -1 if outflow else 1)]
            return kind, legs, f"borrowLend {op} {token} interest {d.get('interestAmount')}"
        return LegKind.ADJUSTMENT, [], f"unmapped ledger update type {t!r}"

    # ---- balances ----------------------------------------------------------------

    def abstraction_mode(self) -> str:
        return str(self._info({"type": "userAbstraction", "user": self.address}))

    def perp_dex_names(self) -> list[str]:
        rows = self._info({"type": "perpDexs"})
        return ["" if row is None else str(row["name"]) for row in rows or [None]]

    def fetch_balances(self) -> NormalizedBalances:
        """Balance vector per base.NormalizedBalances.

        Cash per token: under unifiedAccount / portfolioMargin the spot
        clearinghouse holds every balance (perp dex states only contribute
        positions); otherwise each perp dex adds its collateral's
        accountValue minus its positions' unrealizedPnl, on top of spot.
        Positions: "<COIN>-PERP" with entry_price. equity_usd_micros is the
        USDC value the venue computes: USDC cash plus unrealized P&L of
        USDC-margined positions (= sum of accountValue + spot USDC in
        standard mode), half-even to micros. Non-USDC spot tokens are in
        `assets` only."""
        mode = self.abstraction_mode()
        unified = mode in UNIFIED_MODES
        spot = self._info({"type": "spotClearinghouseState", "user": self.address})
        cash: dict[str, Decimal] = {}
        for b in spot.get("balances") or []:
            name = str(b["coin"])
            cash[name] = cash.get(name, Decimal(0)) + Decimal(_s(b["total"]))
        positions: list[AssetAmount] = []
        usdc_upnl = Decimal(0)
        as_of_ms: int | None = None
        for dex in self.perp_dex_names():
            body: dict[str, Any] = {"type": "clearinghouseState", "user": self.address}
            if dex:
                body["dex"] = dex
            state = self._info(body)
            as_of_ms = as_of_ms or (int(state["time"]) if state.get("time") else None)
            held = [ap["position"] for ap in state.get("assetPositions") or []]
            account_value = Decimal(_s(state.get("marginSummary", {}).get("accountValue") or "0"))
            if not held and (unified or not account_value):
                continue  # nothing on this dex (skips its meta lookup)
            collateral = self.token_name(self.perp_dex(dex).collateral_token) if dex else USDC
            upnl = sum((Decimal(_s(p.get("unrealizedPnl") or "0")) for p in held), Decimal(0))
            if collateral == USDC:
                usdc_upnl += upnl
            if not unified:
                cash[collateral] = cash.get(collateral, Decimal(0)) + account_value - upnl
            for p in held:
                coin = str(p["coin"])
                qty, dp = atoms(p["szi"], self.coin_decimals(coin))
                if qty:
                    positions.append(
                        AssetAmount(
                            asset=perp_asset(coin),
                            atoms=qty,
                            decimals=dp,
                            entry_price=_s(p["entryPx"]) if p.get("entryPx") else None,
                        )
                    )
        assets: list[AssetAmount] = []
        for name, amount in sorted(cash.items()):
            if amount == 0:
                continue
            value, dp = atoms(str(amount), self.token_decimals(name))
            assets.append(AssetAmount(asset=name, atoms=value, decimals=dp))
        equity = (cash.get(USDC, Decimal(0)) + usdc_upnl) * MICROS_PER_UNIT
        return NormalizedBalances(
            as_of=_ms_to_dt(as_of_ms) if as_of_ms else datetime.now(UTC),
            assets=assets + positions,
            equity_usd_micros=int(equity.quantize(Decimal(1), rounding=ROUND_HALF_EVEN)),
        )


def fill_id(r: dict[str, Any]) -> str:
    """`tid` is Hyperliquid's unique trade id; self-trade prevention means one
    account never holds both sides of a tid."""
    return str(r["tid"])


def _merge(legs: list[NormalizedLeg]) -> list[NormalizedLeg]:
    """Sum legs with the same (kind, asset), rescaled to the finer decimals."""
    out: dict[tuple[str, str], NormalizedLeg] = {}
    for leg in legs:
        key = (leg.kind, leg.asset)
        prev = out.get(key)
        if prev is None:
            out[key] = leg
            continue
        dp = max(prev.decimals, leg.decimals)
        total = prev.delta_atoms * 10 ** (dp - prev.decimals) + leg.delta_atoms * 10 ** (
            dp - leg.decimals
        )
        out[key] = prev.model_copy(update={"delta_atoms": total, "decimals": dp})
    return list(out.values())


def perp_instrument(coin: str, sz_decimals: int, collateral: str = USDC) -> NormalizedInstrument:
    return NormalizedInstrument(
        symbol=coin,
        type=InstrumentType.PERP,
        base_asset=perp_asset(coin),
        quote_asset=collateral,
        qty_decimals=sz_decimals,
        min_notional=MIN_ORDER_NOTIONAL,
    )


def spot_instrument(pair: SpotPair) -> NormalizedInstrument:
    return NormalizedInstrument(
        symbol=pair.name,
        type=InstrumentType.SPOT,
        base_asset=pair.base.name,
        quote_asset=pair.quote.name,
        qty_decimals=pair.base.sz_decimals,
        min_notional=MIN_ORDER_NOTIONAL,
    )


# ---- public market data ------------------------------------------------------------


class HyperliquidMarketData(_Info, AssetMarketData):
    """Public reads: instruments (first perp dex + spot), daily closes from
    candleSnapshot, top of book from l2Book, perp funding history. Symbols
    are Hyperliquid coin names: "BTC" for the perp, "@107" / "PURR/USDC" for
    spot pairs, "xyz:XYZ100" for a HIP-3 perp."""

    venue = "hyperliquid"

    def __init__(self, info_url: str = INFO_URL, client: httpx.Client | None = None) -> None:
        self._init_info(info_url, client)

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        perps = self.perp_dex("")
        out = [
            perp_instrument(str(u["name"]), int(u["szDecimals"]))
            for u in perps.universe
            if not u.get("isDelisted")
        ]
        out.extend(spot_instrument(pair) for pair in self.spot_info().pairs.values())
        return out

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """UTC daily candles; the still-open candle of today is not a close
        and is dropped (its `T` close time is in the future)."""
        start_ms = _dt_to_ms(datetime(start.year, start.month, start.day, tzinfo=UTC))
        end_day = datetime(end.year, end.month, end.day, tzinfo=UTC) + timedelta(days=1)
        rows = self._info(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": symbol,
                    "interval": "1d",
                    "startTime": start_ms,
                    "endTime": _dt_to_ms(end_day) - 1,
                },
            }
        )
        now = _now_ms()
        out = []
        for c in sorted(rows or [], key=lambda c: int(c["t"])):
            day = _ms_to_dt(c["t"]).date()
            if int(c["T"]) >= now or not start <= day <= end:
                continue
            out.append(DailyClose(symbol=symbol, day=day, close=_s(c["c"])))
        return out

    def fetch_mids(self) -> dict[str, str]:
        """Every coin's mid (the last trade when a book is empty), one call."""
        return {str(k): _s(v) for k, v in (self._info({"type": "allMids"}) or {}).items()}

    def fetch_quote(self, symbol: str) -> Quote | None:
        book = self._info({"type": "l2Book", "coin": symbol})
        if not book:
            return None
        levels = book.get("levels") or [[], []]
        bids, asks = (levels + [[], []])[:2]
        bid = _s(bids[0]["px"]) if bids else None
        ask = _s(asks[0]["px"]) if asks else None
        last = None
        if bid is None and ask is None:
            last = self.fetch_mids().get(symbol)
            if last is None:
                return None
        as_of = _ms_to_dt(book["time"]) if book.get("time") else datetime.now(UTC)
        return Quote(symbol=symbol, bid=bid, ask=ask, last=last, as_of=as_of)

    def fetch_funding_history(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[FundingRate]:
        out: list[FundingRate] = []
        cursor = _dt_to_ms(start)
        end_ms = _dt_to_ms(end)
        while cursor <= end_ms:
            rows = self._info(
                {"type": "fundingHistory", "coin": symbol, "startTime": cursor, "endTime": end_ms}
            )
            rows = sorted(rows or [], key=lambda r: int(r["time"]))
            out.extend(
                FundingRate(symbol=symbol, at=_ms_to_dt(r["time"]), rate=_s(r["fundingRate"]))
                for r in rows
            )
            if len(rows) < TIME_RANGE_PAGE_LIMIT:
                break
            cursor = int(rows[-1]["time"]) + 1  # one row per coin per time
        return out
