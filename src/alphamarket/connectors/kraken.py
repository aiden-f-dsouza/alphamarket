"""Kraken spot connector (contract v2, platform_plan P4 item 1).

Auth (docs.kraken.com/api/docs/guides/spot-rest-auth): every private call is
a form-encoded POST to /0/private/<Method> carrying a `nonce`, with headers
    API-Key   the public key string
    API-Sign  base64(HMAC-SHA512(base64decode(secret), path + SHA256(nonce + postdata)))
The nonce must strictly increase per key, so one process-wide counter hands
them out (microseconds, bumped by one on collision).

Payload shapes come from the official docs' example responses (private
endpoints; no account exists yet) and from real public payloads recorded
2026-09-26 (tests/fixtures/kraken/README.md). What matters for the ledger:

  Ledgers        one row per balance change: refid, time, type, subtype,
                 asset, amount, fee, balance. Kraken defines
                 balance = old_balance + amount - fee (support article
                 "How to interpret Ledger history fields"), so a row is a leg
                 of `amount` plus a FEE leg of -fee, and `balance` is the
                 venue's running balance after both. Newest first, 50 a page.
  TradesHistory  trade txid -> pair, time, type, price, cost, fee, vol,
                 ordertxid, maker, ledgers (ids, with ledgers=true). Newest
                 first. `cost` has the pair's cost_decimals (5 for USD pairs)
                 while ZUSD ledger rows have 4, so trade fields alone cannot
                 reproduce the running balance: fill legs use the trade's
                 ledger rows and the trade fields are only cross-checked.
  Balance        asset -> decimal string (strings; BalanceEx's docs example
                 prints numbers, so it is not used).

Asset codes: Kraken's legacy X/Z codes map to the canonical code through the
altname (XXBT -> XBT -> BTC, XETH -> ETH, ZUSD -> USD, XXDG -> XDG -> DOGE).
Suffixed balances (ETH2.S staked, XBT.M opt-in rewards, DOT28.S bonded,
USD.HOLD, *.F auto-earn, *.B yield-bearing, *.P parachain) are SEPARATE
balances with their own running ledger balance, so they stay separate assets:
root mapped, suffix kept ("XBT.M" -> "BTC.M", "ETH2.S" -> "ETH2.S"). Moving
coins between a base balance and a variant is a CONVERT; valuation maps a
variant to its underlying (underlying_asset, KrakenMarketData.pricing_symbol).

Pagination (base.Page rules): Ledgers are walked OLDEST page first inside a
window pinned at pass start: (start, end] with `end` fixed, a probe request
learns `count`, then ofs = count - emitted - 50 walks upward. Fills need two
requests per 50 ledger rows (Ledgers type=trade, then TradesHistory for that
time window), so each is its own page: the first page returns no items and
carries the ledger rows in its cursor to the second. Re-emitting a row is
harmless (ids are stable), so every boundary errs towards overlap.
"""

import base64
import hashlib
import hmac
import json
import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from alphamarket.connectors.base import (
    Action,
    AssetAmount,
    AssetMarketData,
    AssetVenueConnector,
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

log = logging.getLogger(__name__)

KRAKEN_API_URL = "https://api.kraken.com"
LEDGER_PAGE = 50  # Ledgers returns a fixed 50 rows per request
PERMISSION_DENIED = "EGeneral:Permission denied"
# Retry hint for EAPI:Rate limit exceeded, which carries no Retry-After:
# a history call costs up to 4 counter points and Starter decays 0.33/s.
RATE_LIMIT_RETRY_SECONDS = 12.0
MAX_WINDOW_RESTARTS = 3

# ---- asset codes -------------------------------------------------------------

# Kraken code -> altname, for every code whose altname differs (checked
# against the full public Assets list on 2026-09-26; test pins the fixture).
_LEGACY_CODES = {
    "KFEE": "KFEE",  # fee credits; altname "FEE" is too generic to adopt
    "XETC": "ETC",
    "XETH": "ETH",
    "XLTC": "LTC",
    "XMLN": "MLN",
    "XREP": "REP",
    "XXBT": "XBT",
    "XXDG": "XDG",
    "XXLM": "XLM",
    "XXMR": "XMR",
    "XXRP": "XRP",
    "XZEC": "ZEC",
    "ZARS": "ARS",
    "ZAUD": "AUD",
    "ZCAD": "CAD",
    "ZCLP": "CLP",
    "ZCOP": "COP",
    "ZDKK": "DKK",
    "ZEUR": "EUR",
    "ZGBP": "GBP",
    "ZGEL": "GEL",
    "ZGHS": "GHS",
    "ZJPY": "JPY",
    "ZLKR": "LKR",
    "ZMXN": "MXN",
    "ZPLN": "PLN",
    "ZSEK": "SEK",
    "ZUGX": "UGX",
    "ZUSD": "USD",
    "ZVND": "VND",
    "ZXOF": "XOF",
}
# Kraken altnames that differ from the code the rest of the market uses.
_RENAMES = {"XBT": "BTC", "XDG": "DOGE"}
# Roots of suffixed variants that name a different asset than the root text.
_VARIANT_ROOTS = {"ETH2": "ETH"}


def canonical_asset(code: str) -> str:
    """Kraken asset code -> canonical ledger code (base.py "Asset codes").
    Variants keep their suffix: "XBT.M" -> "BTC.M", "ETH2.S" -> "ETH2.S"."""
    root, dot, suffix = code.partition(".")
    root = _LEGACY_CODES.get(root, root)
    root = _RENAMES.get(root, root)
    return f"{root}{dot}{suffix}"


def underlying_asset(code: str) -> str:
    """The asset a (canonical) variant is worth one unit of: "ETH2.S" -> "ETH",
    "DOT28.S" -> "DOT" (28-day bonded), "SOL03.S" -> "SOL", "BTC.M" -> "BTC",
    "USD.HOLD" -> "USD", and the legacy unstaked "ETH2" -> "ETH". Other plain
    codes are returned unchanged."""
    root, dot, _ = code.partition(".")
    if root in _VARIANT_ROOTS:
        return _VARIANT_ROOTS[root]
    if not dot:
        return code
    # Bonding-period variants append two digits (DOT28, SOL03, KSM07, FLOW14).
    if len(root) > 2 and root[-2:].isdigit() and not root[-3].isdigit():
        return root[:-2]
    return root


# ---- signing and nonces ------------------------------------------------------


def sign(url_path: str, postdata: str, nonce: int | str, secret_b64: str | bytes) -> str:
    """API-Sign for one private request (Kraken's published algorithm)."""
    secret = base64.b64decode(secret_b64) if isinstance(secret_b64, str) else secret_b64
    digest = hashlib.sha256(f"{nonce}{postdata}".encode()).digest()
    mac = hmac.new(secret, url_path.encode() + digest, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class NonceCounter:
    """Strictly increasing nonces per API key, shared by every connector in
    the process: max(clock in microseconds, last + 1)."""

    def __init__(self, clock=time.time_ns) -> None:
        self._clock = clock
        self._last: dict[str, int] = {}
        self._lock = threading.Lock()

    def next(self, key: str) -> int:
        with self._lock:
            nonce = max(self._clock() // 1000, self._last.get(key, 0) + 1)
            self._last[key] = nonce
            return nonce


NONCES = NonceCounter()


# ---- errors and parsing ------------------------------------------------------


class KrakenError(Exception):
    """Kraken answered with a non-empty `error` list (or an unusable body)."""

    def __init__(self, endpoint: str, errors: list[str]) -> None:
        super().__init__(f"kraken {endpoint}: {', '.join(errors)}")
        self.endpoint = endpoint
        self.errors = errors


class KrakenAuthError(KrakenError):
    """The key or signature was rejected (EAPI:Invalid key / signature / nonce)."""


def _load(resp: httpx.Response) -> dict[str, Any]:
    """JSON with every non-integer number as Decimal: floats never appear."""
    return json.loads(resp.content, parse_float=Decimal)


def _retry_after(resp: httpx.Response) -> float:
    value = resp.headers.get("Retry-After")
    try:
        return max(float(value), 0.0) if value is not None else RATE_LIMIT_RETRY_SECONDS
    except ValueError:
        return RATE_LIMIT_RETRY_SECONDS


def _raise_for(resp: httpx.Response, endpoint: str) -> dict[str, Any]:
    """HTTP 429/418 and Kraken's in-body rate-limit errors -> RateLimited;
    other errors -> KrakenError. Returns the parsed body."""
    if resp.status_code in (429, 418):
        raise RateLimited(_retry_after(resp), endpoint)
    resp.raise_for_status()
    body = _load(resp)
    errors = [str(e) for e in body.get("error") or []]
    for err in errors:
        if err.startswith(("EAPI:Rate limit exceeded", "EGeneral:Too many requests")):
            raise RateLimited(RATE_LIMIT_RETRY_SECONDS, endpoint)
        if err.startswith("EService:Throttled"):
            # "EService:Throttled: <unix ts>" = retry after that time.
            _, _, when = err.rpartition(":")
            try:
                wait = max(float(when.strip()) - time.time(), 1.0)
            except ValueError:
                wait = RATE_LIMIT_RETRY_SECONDS
            raise RateLimited(wait, endpoint)
    return body


def _check(body: dict[str, Any], endpoint: str) -> Any:
    errors = [str(e) for e in body.get("error") or []]
    if errors:
        if any(e.startswith("EAPI:Invalid") for e in errors):
            raise KrakenAuthError(endpoint, errors)
        raise KrakenError(endpoint, errors)
    return body.get("result")


def _ts(value: Any) -> datetime:
    """Unix seconds with up to 4 decimals (Decimal or int) -> aware UTC."""
    d = Decimal(str(value))
    secs = int(d)
    micros = int(((d - secs) * 1_000_000).to_integral_value(ROUND_HALF_EVEN))
    return datetime.fromtimestamp(secs, UTC) + timedelta(microseconds=micros)


def _places(value: str) -> int:
    """Decimal places as printed: "0.0200000000" -> 10. Kraken prints every
    amount of an asset at that asset's precision."""
    exp = Decimal(value).as_tuple().exponent
    return max(-exp, 0) if isinstance(exp, int) else 0


# ---- ledger rows -------------------------------------------------------------


@dataclass(frozen=True)
class LedgerRow:
    id: str
    refid: str
    time: str  # decimal string, unix seconds
    type: str
    subtype: str
    asset: str  # Kraken code as reported
    amount: str
    fee: str
    balance: str

    @classmethod
    def parse(cls, ledger_id: str, raw: dict[str, Any]) -> "LedgerRow":
        return cls(
            id=ledger_id,
            refid=str(raw.get("refid", "")),
            time=str(raw["time"]),
            type=str(raw.get("type", "")),
            subtype=str(raw.get("subtype", "")),
            asset=str(raw["asset"]),
            amount=str(raw["amount"]),
            fee=str(raw.get("fee", "0")),
            balance=str(raw["balance"]),
        )

    @property
    def code(self) -> str:
        return canonical_asset(self.asset)

    @property
    def decimals(self) -> int:
        return max(_places(self.amount), _places(self.fee), _places(self.balance))

    def atoms(self, value: str) -> int:
        return to_atoms(value, self.decimals)


def _merge(
    legs: list[NormalizedLeg], final_balances: dict[str, tuple[int, int]]
) -> list[NormalizedLeg]:
    """Sum legs sharing (kind, asset) (base.py forbids duplicates), then put
    the venue's running balance on the LAST leg of each asset only: it is the
    balance after every leg of that asset in this fill/event."""
    merged: dict[tuple[LegKind, str], NormalizedLeg] = {}
    for leg in legs:
        key = (leg.kind, leg.asset)
        prev = merged.get(key)
        if prev is None:
            merged[key] = leg
            continue
        d = max(prev.decimals, leg.decimals)
        delta = prev.delta_atoms * 10 ** (d - prev.decimals) + leg.delta_atoms * 10 ** (
            d - leg.decimals
        )
        merged[key] = prev.model_copy(update={"delta_atoms": delta, "decimals": d})
    out = list(merged.values())
    last_index = {leg.asset: i for i, leg in enumerate(out)}
    result = []
    for i, leg in enumerate(out):
        bal = None
        if last_index[leg.asset] == i and leg.asset in final_balances:
            atoms, decimals = final_balances[leg.asset]
            if decimals <= leg.decimals:
                bal = atoms * 10 ** (leg.decimals - decimals)
            else:  # rescale the leg up so the balance stays exact
                leg = leg.model_copy(
                    update={
                        "delta_atoms": leg.delta_atoms * 10 ** (decimals - leg.decimals),
                        "decimals": decimals,
                    }
                )
                bal = atoms
        result.append(leg.model_copy(update={"balance_after_atoms": bal}))
    return result


def _final_balances(rows: list[LedgerRow]) -> dict[str, tuple[int, int]]:
    """(atoms, decimals) of each asset's balance after the last row (rows
    oldest first)."""
    out: dict[str, tuple[int, int]] = {}
    for row in rows:
        out[row.code] = (row.atoms(row.balance), row.decimals)
    return out


_STAKING_WRAPPERS = {"spottostaking", "stakingfromspot", "stakingtospot", "spotfromstaking"}
_EARN_REWARDS = {"reward"}


def event_kind(row: LedgerRow) -> LegKind:
    """Primary leg kind of a non-trade ledger row (methodology "Kraken")."""
    t, sub = row.type, row.subtype
    amount = Decimal(row.amount)
    if t == "deposit":
        return LegKind.DEPOSIT
    if t == "withdrawal":
        return LegKind.WITHDRAWAL
    if t == "staking":
        return LegKind.STAKING
    if t == "dividend":
        return LegKind.DIVIDEND
    if t == "rollover":
        return LegKind.INTEREST
    if t in ("margin", "margin trade", "settled"):
        return LegKind.REALIZED_PNL
    if t in ("spend", "receive", "conversion"):
        return LegKind.CONVERT
    if t == "earn":
        return LegKind.STAKING if sub in _EARN_REWARDS else LegKind.CONVERT
    if t == "transfer":
        if sub in _STAKING_WRAPPERS:
            return LegKind.CONVERT
        if sub == "spotfromfutures":
            return LegKind.TRANSFER_IN
        if sub == "spottofutures":
            return LegKind.TRANSFER_OUT
        # Airdrops, forks and unlabelled transfers: an external flow, so the
        # flow-adjusted return neither gains nor loses by it.
        return LegKind.TRANSFER_IN if amount >= 0 else LegKind.TRANSFER_OUT
    # adjustment, credit, sale, nft_rebate, invite bonus and anything new.
    return LegKind.ADJUSTMENT


def ledger_event(row: LedgerRow) -> NormalizedEvent:
    """One non-trade ledger row -> one event keyed by its ledger id (unique
    per account). Legs: amount under the primary kind, -fee as FEE, except
    rollover where amount - fee is the (negative) margin interest."""
    kind = event_kind(row)
    code, d = row.code, row.decimals
    amount, fee = row.atoms(row.amount), row.atoms(row.fee)
    if kind is LegKind.INTEREST:
        legs = [NormalizedLeg(asset=code, delta_atoms=amount - fee, decimals=d, kind=kind)]
    else:
        legs = [NormalizedLeg(asset=code, delta_atoms=amount, decimals=d, kind=kind)]
        if fee:
            legs.append(NormalizedLeg(asset=code, delta_atoms=-fee, decimals=d, kind=LegKind.FEE))
    description = f"{row.type}/{row.subtype}" if row.subtype else row.type
    return NormalizedEvent(
        venue_ref=row.id,
        kind=kind,
        legs=_merge(legs, _final_balances([row])),
        occurred_at=_ts(row.time),
        description=f"{description} refid={row.refid}",
    )


def trade_crosscheck(
    trade: dict[str, Any], rows: list[LedgerRow], base: str, quote: str
) -> list[str]:
    """Differences between a trade's own fields and its ledger rows beyond
    one atom of rounding. Empty when they agree (ledger amounts win)."""
    problems: list[str] = []
    base_rows = [r for r in rows if r.code == base and Decimal(r.amount) != 0]
    quote_rows = [r for r in rows if r.code == quote and Decimal(r.amount) != 0]
    vol = Decimal(str(trade["vol"]))
    if sum(abs(Decimal(r.amount)) for r in base_rows) != vol:
        problems.append(f"base amount != vol {vol}")
    if quote_rows:
        atom = Decimal(1).scaleb(-quote_rows[0].decimals)
        cost = Decimal(str(trade["cost"]))
        if abs(sum(abs(Decimal(r.amount)) for r in quote_rows) - cost) > atom:
            problems.append(f"quote amount != cost {cost}")
        quote_fee = sum(Decimal(r.fee) for r in rows if r.code == quote)
        fee = Decimal(str(trade.get("fee", "0")))
        charged_elsewhere = any(Decimal(r.fee) != 0 for r in rows if r.code != quote)
        if not charged_elsewhere and abs(quote_fee - fee) > atom:
            problems.append(f"quote fee {quote_fee} != fee {fee}")
    ids = set(trade.get("ledgers") or [])
    if ids and ids != {r.id for r in rows}:
        problems.append("ledger ids differ from the trade's ledgers field")
    return problems


def trade_fill(txid: str, trade: dict[str, Any], rows: list[LedgerRow]) -> NormalizedAssetFill:
    """A trade plus its ledger rows (oldest first) -> one fill. Legs come from
    the ledger rows: the asset that moves WITH the trade direction is the
    base (+ on a buy, - on a sell), the other nonzero row is the quote, and
    every row's fee becomes a FEE leg in that row's asset (KFEE included)."""
    action = Action(str(trade["type"]))
    sign_base = 1 if action is Action.BUY else -1
    moving = [r for r in rows if Decimal(r.amount) != 0]
    base_rows = [r for r in moving if Decimal(r.amount) * sign_base > 0]
    quote_rows = [r for r in moving if Decimal(r.amount) * sign_base < 0]
    base_codes = {r.code for r in base_rows}
    quote_codes = {r.code for r in quote_rows}
    if len(base_codes) != 1 or len(quote_codes) != 1:
        raise KrakenError("TradesHistory", [f"trade {txid}: cannot tell base from quote rows"])
    base, quote = base_codes.pop(), quote_codes.pop()
    pair = str(trade["pair"])
    legs: list[NormalizedLeg] = []
    for row in rows:
        amount = row.atoms(row.amount)
        if amount:
            kind = LegKind.FILL_BASE if row.code == base else LegKind.FILL_QUOTE
            legs.append(
                NormalizedLeg(
                    asset=row.code,
                    delta_atoms=amount,
                    decimals=row.decimals,
                    kind=kind,
                    instrument_symbol=pair,
                )
            )
        fee = row.atoms(row.fee)
        if fee:
            legs.append(
                NormalizedLeg(
                    asset=row.code,
                    delta_atoms=-fee,
                    decimals=row.decimals,
                    kind=LegKind.FEE,
                    instrument_symbol=pair,
                )
            )
    # Order legs base, quote, fee so each asset's running balance lands on
    # its final leg (the FEE leg when the fee is in that asset).
    order = {LegKind.FILL_BASE: 0, LegKind.FILL_QUOTE: 1, LegKind.FEE: 2}
    legs.sort(key=lambda leg: order[leg.kind])
    problems = trade_crosscheck(trade, rows, base, quote)
    if problems:
        log.warning("kraken trade %s disagrees with its ledger rows: %s", txid, "; ".join(problems))
    vol = str(trade["vol"])
    qty_decimals = _places(vol)
    maker = trade.get("maker")
    return NormalizedAssetFill(
        venue_fill_id=txid,
        order_id=str(trade["ordertxid"]) if trade.get("ordertxid") else None,
        instrument=NormalizedInstrument(
            symbol=pair,
            type=InstrumentType.SPOT,
            base_asset=base,
            quote_asset=quote,
            qty_decimals=qty_decimals,
        ),
        action=action,
        qty_atoms=to_atoms(vol, qty_decimals),
        price=str(trade["price"]),
        legs=_merge(legs, _final_balances(rows)),
        filled_at=_ts(trade["time"]),
        liquidity=None if maker is None else ("maker" if maker else "taker"),
    )


# ---- cursors -----------------------------------------------------------------


@dataclass
class _Walk:
    """Oldest-first walk over one Ledgers window (s, e]. `n` rows in the
    window (None until probed), `k` of them (the oldest) already emitted,
    `t` the newest emitted row time this pass, `r` window restarts."""

    s: int | None
    e: int
    n: int | None = None
    k: int = 0
    t: str | None = None
    r: int = 0

    def resume(self) -> str:
        """Next pass starts one second before the newest row seen, so rows
        sharing its second are re-read (ids dedupe them)."""
        start = math.floor(Decimal(self.t)) - 1 if self.t is not None else self.s
        return json.dumps({"v": 1, "s": start})


def _start_from(since: datetime | None) -> int | None:
    return math.floor(since.timestamp()) - 1 if since is not None else None


def _decode(cursor: str) -> dict[str, Any]:
    state = json.loads(cursor)
    if not isinstance(state, dict) or state.get("v") != 1:
        raise ValueError("not a Kraken cursor")
    return state


# ---- connector ---------------------------------------------------------------


class KrakenConnector(AssetVenueConnector):
    venue = "kraken"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        base_url: str = KRAKEN_API_URL,
        client: httpx.Client | None = None,
        nonces: NonceCounter | None = None,
        clock=time.time,
    ) -> None:
        # Pasted credentials often carry a trailing newline or wrapped lines;
        # neither the key nor base64 ever contains whitespace.
        api_key = (api_key or "").strip()
        api_secret = "".join((api_secret or "").split())
        if not api_key:
            raise ValueError("Kraken needs an API key")
        try:
            self._secret = base64.b64decode(api_secret, validate=True)
        except (ValueError, TypeError):
            raise ValueError("Kraken private key is not valid base64") from None
        if not self._secret:
            raise ValueError("Kraken needs a private key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=20.0)
        self._nonces = nonces or NONCES
        self._clock = clock

    def __repr__(self) -> str:  # never show the secret
        return f"KrakenConnector(base_url={self.base_url!r})"

    # -- transport --

    def _post(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """One signed POST; returns the parsed body (errors not yet checked)."""
        self.throttle(method)
        path = f"/0/private/{method}"
        nonce = self._nonces.next(self.api_key)
        postdata = urlencode({"nonce": nonce, **(params or {})})
        headers = {
            "API-Key": self.api_key,
            "API-Sign": sign(path, postdata, nonce, self._secret),
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        resp = self.client.post(self.base_url + path, content=postdata, headers=headers)
        return _raise_for(resp, method)

    def _private(self, method: str, params: dict[str, Any] | None = None) -> Any:
        return _check(self._post(method, params), method)

    # -- balances --

    def fetch_balances(self) -> NormalizedBalances:
        result = self._private("Balance") or {}
        amounts: dict[str, AssetAmount] = {}
        for code, value in result.items():
            value = str(value)
            d = _places(value)
            asset = canonical_asset(code)
            atoms = to_atoms(value, d)
            prev = amounts.get(asset)
            if prev is not None:  # two Kraken codes for one asset: sum exactly
                top = max(prev.decimals, d)
                atoms = prev.atoms * 10 ** (top - prev.decimals) + atoms * 10 ** (top - d)
                d = top
            amounts[asset] = AssetAmount(asset=asset, atoms=atoms, decimals=d)
        return NormalizedBalances(
            as_of=datetime.fromtimestamp(self._clock(), UTC), assets=list(amounts.values())
        )

    def check_connection(self) -> NormalizedBalance:
        """One authenticated Balance call. Kraken omits assets the account has
        never held (a new or EUR-only account has no ZUSD), so a missing USD
        entry is a USD balance of exactly 0, not a bad key: the base
        fetch_balance would raise BalanceUnavailable and the connect screen
        would report "could not authenticate" for a valid key."""
        balances = self.fetch_balances()
        for amount in balances.assets:
            if amount.asset == "USD":
                micros = to_micros(from_atoms(amount.atoms, amount.decimals))
                return NormalizedBalance(balance_micros=micros, as_of=balances.as_of)
        return NormalizedBalance(balance_micros=0, as_of=balances.as_of)

    # -- read-only proof --

    def _probe(self, method: str, params: dict[str, Any] | None = None) -> str:
        """ "ok" or the first venue error string. Rate limits still raise."""
        body = self._post(method, params)
        errors = [str(e) for e in body.get("error") or []]
        if errors and any(e.startswith("EAPI:Invalid") for e in errors):
            raise KrakenAuthError(method, errors)
        return errors[0] if errors else "ok"

    # AddOrder probe: validate=true means Kraken checks the order and never
    # sends it to the matching engine. A limit buy of 0.0001 BTC at $1 is
    # also far below any market, as a second guard.
    ORDER_PROBE = {
        "pair": "XBTUSD",
        "type": "buy",
        "ordertype": "limit",
        "price": "1.0",
        "volume": "0.0001",
        "validate": "true",
    }

    def prove_read_only(self) -> ReadOnlyEvidence:
        """ORDER_PROBE: Balance, TradesHistory and Ledgers must succeed;
        AddOrder(validate=true) and WithdrawMethods must both be refused with
        EGeneral:Permission denied. read_only is False if either is not
        refused (the key can trade or withdraw), None if a read fails."""
        details: dict[str, str | bool | int | None] = {
            "balance": self._probe("Balance"),
            "trades_history": self._probe("TradesHistory", {"without_count": "true"}),
            "ledgers": self._probe("Ledgers", {"without_count": "true"}),
            "add_order_validate": self._probe("AddOrder", self.ORDER_PROBE),
            "withdraw_methods": self._probe("WithdrawMethods"),
        }
        can_trade = details["add_order_validate"] != PERMISSION_DENIED
        can_withdraw = details["withdraw_methods"] != PERMISSION_DENIED
        reads_ok = all(details[k] == "ok" for k in ("balance", "trades_history", "ledgers"))
        details["can_trade"] = can_trade
        details["can_withdraw"] = can_withdraw
        details["reads_ok"] = reads_ok
        if can_trade or can_withdraw:
            read_only: bool | None = False
        elif reads_ok:
            read_only = True
        else:
            read_only = None  # safe, but cannot read what Koine needs
        return ReadOnlyEvidence(
            method=ReadOnlyProof.ORDER_PROBE,
            read_only=read_only,
            checked_at=datetime.fromtimestamp(self._clock(), UTC),
            details=details,
        )

    def check_write_scope(self) -> bool | None:
        read_only = self.prove_read_only().read_only
        return None if read_only is None else not read_only

    # -- ledger walk --

    def _new_walk(self, state: dict[str, Any] | None, since: datetime | None) -> _Walk:
        start = state.get("s") if state is not None else _start_from(since)
        return _Walk(s=start, e=math.floor(self._clock()))

    def _ledger_page(self, walk: _Walk, ledger_type: str) -> tuple[list[LedgerRow], bool, _Walk]:
        """One Ledgers request. Returns the not-yet-emitted rows it covers,
        oldest first; whether they reach the top of the window; and the walk
        with `n` updated (the caller advances `k` by what it emits)."""
        ofs = 0 if walk.n is None else max(walk.n - walk.k - LEDGER_PAGE, 0)
        params: dict[str, Any] = {"type": ledger_type, "end": walk.e, "ofs": ofs}
        if walk.s is not None:
            params["start"] = walk.s
        result = self._private("Ledgers", params) or {}
        count = int(result.get("count", 0))
        rows = [LedgerRow.parse(i, raw) for i, raw in (result.get("ledger") or {}).items()]
        rows.sort(key=lambda r: Decimal(r.time), reverse=True)  # newest first, stable
        if walk.n is not None and count != walk.n:
            # History inside the pinned window changed; re-walk it (row ids
            # make the re-emitted rows harmless).
            if walk.r >= MAX_WINDOW_RESTARTS:
                raise KrakenError("Ledgers", [f"window count keeps changing ({walk.n}->{count})"])
            return [], False, replace(walk, n=None, k=0, r=walk.r + 1)
        remaining = count - walk.k
        if walk.n is None and remaining > LEDGER_PAGE:
            return [], False, replace(walk, n=count)  # probe only: oldest rows come next
        taken = rows[: max(remaining - ofs, 0)]
        return list(reversed(taken)), ofs == 0, replace(walk, n=count)

    # -- events --

    def fetch_events_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedEvent]:
        state = _decode(cursor) if cursor is not None else None
        walk = _Walk(**state["w"]) if state and "w" in state else self._new_walk(state, since)
        rows, final, walk = self._ledger_page(walk, "all")
        if rows:
            walk = replace(walk, k=walk.k + len(rows), t=rows[-1].time)
        events = [ledger_event(r) for r in rows if r.type != "trade"]
        if final:
            return Page(items=events, resume_cursor=walk.resume())
        return Page(items=events, next_cursor=json.dumps({"v": 1, "w": asdict(walk)}))

    # -- fills --

    def fetch_asset_fills_page(
        self, cursor: str | None = None, since: datetime | None = None
    ) -> Page[NormalizedAssetFill]:
        state = _decode(cursor) if cursor is not None else None
        if state is not None and state.get("ph") == "T":
            return self._trades_page(state)
        walk = _Walk(**state["w"]) if state and "w" in state else self._new_walk(state, since)
        rows, final, walk = self._ledger_page(walk, "trade")
        if rows and not final:
            rows = rows[: _complete_prefix(rows)]
        if rows:
            walk = replace(walk, k=walk.k + len(rows), t=rows[-1].time)
        if not rows:
            if final:
                return Page(resume_cursor=walk.resume())
            return Page(next_cursor=json.dumps({"v": 1, "w": asdict(walk)}))
        next_state = {
            "v": 1,
            "ph": "T",
            "w": asdict(walk),
            "final": final,
            "rows": [asdict(r) for r in rows],
            "tofs": 0,
            "found": {},
        }
        return Page(next_cursor=json.dumps(next_state))

    def _trades_page(self, state: dict[str, Any]) -> Page[NormalizedAssetFill]:
        """Second half of a fills page: the TradesHistory entries for the
        ledger rows carried in the cursor, over the same time window."""
        rows = [LedgerRow(**r) for r in state["rows"]]
        walk = _Walk(**state["w"])
        needed = list(dict.fromkeys(r.refid for r in rows))
        found: dict[str, Any] = state["found"]
        times = [Decimal(r.time) for r in rows]
        params = {
            "start": math.floor(min(times)) - 2,  # exclusive
            "end": math.ceil(max(times)) + 1,  # inclusive
            "ofs": state["tofs"],
            "ledgers": "true",
            "consolidate_taker": "false",  # one trade per ledger refid
        }
        result = self._private("TradesHistory", params) or {}
        trades: dict[str, Any] = result.get("trades") or {}
        for txid, trade in trades.items():
            if txid in needed:
                found[txid] = {k: _jsonable(v) for k, v in trade.items()}
        missing = [t for t in needed if t not in found]
        seen = state["tofs"] + len(trades)
        if missing and trades and seen < int(result.get("count", 0)):
            return Page(next_cursor=json.dumps({**state, "tofs": seen, "found": found}))
        if missing:
            raise KrakenError(
                "TradesHistory", [f"no trade for ledger refids {', '.join(missing[:5])}"]
            )
        fills = [
            trade_fill(txid, found[txid], [r for r in rows if r.refid == txid]) for txid in needed
        ]
        fills.sort(key=lambda f: f.filled_at)  # stable: ledger order breaks ties
        if state["final"]:
            return Page(items=fills, resume_cursor=walk.resume())
        return Page(items=fills, next_cursor=json.dumps({"v": 1, "w": asdict(walk)}))


def _complete_prefix(rows: list[LedgerRow]) -> int:
    """Rows (oldest first) to emit from a page that is not the top of the
    window: drop the newest trade's rows, which may continue on the next
    page, plus any earlier row that shares a refid with the dropped ones, so
    a trade's rows are always emitted together. Keeps everything if that
    would leave nothing (a single trade larger than a page)."""
    cut = next(i for i, r in enumerate(rows) if r.refid == rows[-1].refid)
    while True:
        tail = {r.refid for r in rows[cut:]}
        first = next(i for i, r in enumerate(rows) if r.refid in tail)
        if first == cut:
            break
        cut = first
    return cut or len(rows)


def _jsonable(value: Any) -> Any:
    return str(value) if isinstance(value, Decimal) else value


# ---- market data -------------------------------------------------------------


class KrakenMarketData(AssetMarketData):
    """Public, unauthenticated Kraken data. Kraken's API terms require prior
    permission for non-personal commercial use of public market data
    (docs/venues/kraken.md), so production valuation uses this only once
    that permission is recorded."""

    venue = "kraken"
    # Kraken quotes these at par against USD only by market; the variants of
    # a stablecoin are the same coin in another balance bucket.
    STABLECOINS_AT_PAR = frozenset(
        {"USD", "USDC", "USDT", "USD.M", "USD.HOLD", "USDC.M", "USDT.M", "USDC.F", "USDT.F"}
    )

    def __init__(self, base_url: str = KRAKEN_API_URL, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=20.0)

    def _public(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        self.throttle(endpoint)
        resp = self.client.get(f"{self.base_url}/0/public/{endpoint}", params=params)
        return _check(_raise_for(resp, endpoint), endpoint)

    def fetch_assets(self) -> dict[str, int]:
        """Canonical asset code -> decimals Kraken stores it at."""
        result = self._public("Assets") or {}
        return {canonical_asset(code): int(info["decimals"]) for code, info in result.items()}

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        result = self._public("AssetPairs") or {}
        out: list[NormalizedInstrument] = []
        for symbol, info in result.items():
            if symbol.endswith(".d"):  # retired dark-pool books
                continue
            lot = int(info["lot_decimals"])
            ordermin = info.get("ordermin")
            out.append(
                NormalizedInstrument(
                    symbol=symbol,
                    type=InstrumentType.SPOT,
                    base_asset=canonical_asset(str(info["base"])),
                    quote_asset=canonical_asset(str(info["quote"])),
                    qty_decimals=lot,
                    min_qty_atoms=to_atoms(str(ordermin), lot) if ordermin is not None else 0,
                    min_notional=str(info["costmin"]) if info.get("costmin") is not None else None,
                )
            )
        return out

    def pricing_symbol(
        self, asset: str, instruments: list[NormalizedInstrument] | None = None
    ) -> str | None:
        """Variants are priced as their underlying (ETH2.S by ETH/USD)."""
        underlying = underlying_asset(asset)
        if asset in self.STABLECOINS_AT_PAR or underlying in self.STABLECOINS_AT_PAR:
            return None
        return super().pricing_symbol(underlying, instruments)

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """UTC daily closes from OHLC interval=1440. A bar's time is the UTC
        midnight it opens; only committed bars (time <= `last`) count, so the
        running day never appears. Kraken serves the last 720 bars only."""
        since = int(datetime(start.year, start.month, start.day, tzinfo=UTC).timestamp()) - 1
        try:
            result = self._public("OHLC", {"pair": symbol, "interval": 1440, "since": since})
        except KrakenError as exc:
            if any("Unknown asset pair" in e for e in exc.errors):
                return []
            raise
        last = int(result.get("last", 0))
        bars = next((v for k, v in result.items() if k != "last"), [])
        out = []
        for bar in bars:
            opened = int(bar[0])
            day = datetime.fromtimestamp(opened, UTC).date()
            if opened <= last and start <= day <= end:
                out.append(DailyClose(symbol=symbol, day=day, close=str(bar[4])))
        return out

    def fetch_quote(self, symbol: str) -> Quote | None:
        try:
            result = self._public("Ticker", {"pair": symbol})
        except KrakenError as exc:
            if any("Unknown asset pair" in e for e in exc.errors):
                return None
            raise
        if not result:
            return None
        info = next(iter(result.values()))
        return Quote(
            symbol=symbol,
            bid=str(info["b"][0]),
            ask=str(info["a"][0]),
            last=str(info["c"][0]),
            as_of=datetime.now(UTC),
        )
