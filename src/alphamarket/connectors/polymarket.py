"""Polymarket connector.

Polymarket accounts are public on-chain wallets, so there is no credential
at all: the "key id" is the proxy wallet address and every read is an
unauthenticated call to Polymarket's public APIs. Ownership is proved by a
claim code in the trader's profile bio (see venues.py), not by a key.

All mappings are VERIFIED against public payloads captured 2026-09-06
(tests/fixtures/polymarket_*.json). What matters for the ledger:

  /activity?type=TRADE  size (tokens, float, 6 dp)   price (float, up to 10 dp:
                        volume-weighted across the maker fills of one taker
                        order)   usdcSize (float, 6 dp) = the NET cash that
                        moved, fee included. So fee = |size x price - usdcSize|
                        and the ledger's cash flow reproduces usdcSize exactly.
                        Cross-checked: 21.294467 x 0.4696055083 = 9.999999
                        gross (a $10 order), 10.265189 net -> fee 0.265190,
                        which is 5% x size x p x (1-p) to within the price
                        rounding: Polymarket's taker formula at a 5% rate.
  /activity?type=REDEEM the settlement event: size = tokens redeemed,
                        usdcSize = payout ($1 per winning token, 0 for the
                        losing side, redeemed in the same transaction).
  /positions            initialValue = cost basis net of fees, curPrice,
                        outcomeIndex (0 = first outcome = "yes" here).
  balance               NOT observable. Proxy wallets of active traders show
                        0 USDC.e and 0 native USDC on-chain (checked against
                        four wallets on 2026-09-06), so cash lives somewhere
                        the public chain read does not see. fetch_balance
                        raises BalanceUnavailable and the account lands in
                        the fills-only verification tier.

Ticker = conditionId (one binary market); side = outcomeIndex 0 -> yes,
1 -> no. Prices are token prices for the side traded, like Kalshi's
no_price for NO fills. Fill identity: transactionHash:asset:side:price:size
plus an ordinal for identical rows in the same transaction (there is no
per-fill id; several maker fills share one hash).
"""

import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import httpx

from alphamarket.connectors.base import (
    Action,
    BalanceUnavailable,
    MarketDataSource,
    NormalizedBalance,
    NormalizedFill,
    NormalizedMarket,
    NormalizedPosition,
    NormalizedSettlement,
    Side,
    VenueConnector,
)
from alphamarket.money import MICROS_PER_UNIT

_MICRO = Decimal(1) / MICROS_PER_UNIT
PAGE_SIZE = 500


def is_address(value: str) -> bool:
    """A 20-byte hex address with the 0x prefix. Checksum is not required
    because the data-api is case-insensitive and lowercases everything."""
    if len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        int(value[2:], 16)
    except ValueError:
        return False
    return True


def _dec(value: Any) -> Decimal:
    """JSON numbers arrive as floats; `str()` gives the shortest round-trip
    text, which is the digits the API printed. Never arithmetic on the float."""
    if isinstance(value, bool):
        raise TypeError("bool is not a quantity")
    if isinstance(value, float | int):
        return Decimal(str(value))
    return Decimal(value)


def micros(value: Any) -> int:
    """Round a decimal quantity to micros, half-even. Polymarket prices carry
    up to 10 dp (a volume-weighted average), finer than the fixed-point
    scale, so unlike money.to_micros this rounds rather than raising. Cash
    (usdcSize) has 6 dp and is never rounded."""
    return int((_dec(value) * MICROS_PER_UNIT).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def _ts(value: Any) -> datetime:
    return datetime.fromtimestamp(int(value), UTC)


def _side_from_index(index: Any) -> Side | None:
    if index == 0:
        return Side.YES
    if index == 1:
        return Side.NO
    return None


class _PublicHttp:
    """Retrying GET with a polite floor between calls, shared by the account
    connector and the market-data source. No auth headers ever."""

    MIN_INTERVAL = 0.11
    RETRY_STATUSES = (429, 500, 502, 503, 504)
    MAX_ATTEMPTS = 4
    BACKOFF_BASE = 0.5

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(timeout=15.0)
        self._last_request_at = 0.0

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        for attempt in range(self.MAX_ATTEMPTS):
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError:
                if attempt == self.MAX_ATTEMPTS - 1:
                    raise
                time.sleep(self.BACKOFF_BASE * 2**attempt)
                continue
            if resp.status_code in self.RETRY_STATUSES and attempt < self.MAX_ATTEMPTS - 1:
                retry_after = resp.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else self.BACKOFF_BASE * 2**attempt
                time.sleep(delay)
                continue
            return resp
        raise RuntimeError("unreachable")  # pragma: no cover

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        resp = self.get(url, params)
        resp.raise_for_status()
        return resp.json()


class PolymarketConnector(VenueConnector):
    venue = "polymarket"

    def __init__(
        self,
        address: str,
        data_url: str,
        clob_url: str,
        gamma_url: str,
        client: httpx.Client | None = None,
    ) -> None:
        if not is_address(address):
            raise ValueError("Polymarket accounts are identified by a 0x wallet address")
        self.address = address.lower()
        self.data_url = data_url.rstrip("/")
        self.clob_url = clob_url.rstrip("/")
        self.gamma_url = gamma_url.rstrip("/")
        self.http = _PublicHttp(client)
        self._token_sides: dict[str, Side] = {}

    # ---- transport -------------------------------------------------------

    def _activity(self, kind: str, since: datetime | None) -> list[dict[str, Any]]:
        """All activity rows of one type, oldest first, via offset paging."""
        params: dict[str, Any] = {
            "user": self.address,
            "type": kind,
            "limit": PAGE_SIZE,
            "sortBy": "TIMESTAMP",
            "sortDirection": "ASC",
        }
        if since is not None:
            params["start"] = int(since.timestamp())
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.http.get_json(f"{self.data_url}/activity", {**params, "offset": offset})
            if not isinstance(page, list):
                raise ValueError("unexpected activity payload")
            rows.extend(page)
            if len(page) < PAGE_SIZE:
                return rows
            offset += PAGE_SIZE

    def _profile(self) -> dict[str, Any]:
        return self.http.get_json(f"{self.gamma_url}/public-profile", {"address": self.address})

    def _side_of(self, row: dict[str, Any]) -> Side:
        side = _side_from_index(row.get("outcomeIndex"))
        if side is not None:
            return side
        # The unfiltered feed reports outcomeIndex 999; resolve via the
        # market's token list and remember it.
        asset = str(row["asset"])
        if asset not in self._token_sides:
            market = self.http.get_json(f"{self.clob_url}/markets/{row['conditionId']}")
            for index, token in enumerate(market.get("tokens", [])):
                found = _side_from_index(index)
                if found is not None:
                    self._token_sides[str(token["token_id"])] = found
        try:
            return self._token_sides[asset]
        except KeyError:
            raise ValueError(f"cannot determine outcome side for asset {asset}") from None

    # ---- VenueConnector --------------------------------------------------

    def check_connection(self) -> NormalizedBalance:
        """The address must have a Polymarket profile (404 otherwise). There
        is no cash balance to return; zero is a placeholder the connect flow
        never stores as an anchor (see BalanceUnavailable)."""
        self._profile()
        return NormalizedBalance(balance_micros=0, as_of=datetime.now(UTC))

    def check_write_scope(self) -> bool | None:
        return False  # no credential exists, so nothing can write

    def verify_claim(self, code: str) -> bool | None:
        try:
            profile = self._profile()
        except httpx.HTTPError:
            return None
        bio = str(profile.get("bio") or "")
        return code in bio

    def fetch_balance(self) -> NormalizedBalance:
        raise BalanceUnavailable("Polymarket exposes no cash balance for a wallet")

    def fetch_fills(self, since: datetime | None = None) -> list[NormalizedFill]:
        rows = self._activity("TRADE", since)
        fills = [self._to_fill(r) for r in rows]
        # Ordinal suffix for identical rows in one transaction so both survive
        # the unique venue-id constraint.
        seen: dict[str, int] = {}
        out: list[NormalizedFill] = []
        for f in fills:
            n = seen.get(f.venue_fill_id, 0)
            seen[f.venue_fill_id] = n + 1
            out.append(f.model_copy(update={"venue_fill_id": f"{f.venue_fill_id}#{n}"}))
        out.sort(key=lambda f: f.filled_at)
        return out

    def fetch_settlements(self, since: datetime | None = None) -> list[NormalizedSettlement]:
        rows = self._activity("REDEEM", since)
        out = [self._to_settlement(r) for r in rows]
        if since is not None:
            out = [s for s in out if s.settled_at > since]
        out.sort(key=lambda s: s.settled_at)
        return out

    def fetch_positions(self) -> list[NormalizedPosition]:
        out: list[NormalizedPosition] = []
        offset = 0
        while True:
            page = self.http.get_json(
                f"{self.data_url}/positions",
                {"user": self.address, "limit": PAGE_SIZE, "offset": offset},
            )
            for p in page:
                size = micros(p.get("size") or 0)
                if size == 0:
                    continue
                side = self._side_of(p)
                out.append(
                    NormalizedPosition(
                        ticker=str(p["conditionId"]),
                        position_micros=size if side is Side.YES else -size,
                        # initialValue is the cost basis net of entry fees,
                        # which is what the ledger carries open positions at.
                        market_exposure_micros=micros(p.get("initialValue") or 0),
                    )
                )
            if len(page) < PAGE_SIZE:
                return out
            offset += PAGE_SIZE

    # ---- mapping ---------------------------------------------------------

    def _to_fill(self, r: dict[str, Any]) -> NormalizedFill:
        side = self._side_of(r)
        action = Action(str(r["side"]).lower())  # "BUY" | "SELL"
        count = micros(r["size"])
        price = micros(r["price"])
        gross = micros(_dec(r["size"]) * _dec(r["price"]))
        net = micros(r["usdcSize"])
        # The fee is whatever separates gross from the cash that moved. A
        # zero-fee market can round gross one micro past net; clamp so the
        # fee is never negative and the cash flow is still exactly `net`.
        fee = net - gross if action is Action.BUY else gross - net
        if fee < 0:
            gross, fee = net, 0
        return NormalizedFill(
            venue_fill_id=f"{r['transactionHash']}:{r['asset']}:{action.value}:{price}:{count}",
            ticker=str(r["conditionId"]),
            side=side,
            action=action,
            count_micros=count,
            price_micros=price,
            fee_micros=fee,
            filled_at=_ts(r["timestamp"]),
            notional_micros=gross,
        )

    def _to_settlement(self, r: dict[str, Any]) -> NormalizedSettlement:
        side = self._side_of(r)
        count = micros(r["size"])
        payout = micros(r.get("usdcSize") or 0)
        # A payout means this token won; a zero payout means it was the
        # losing side being burned, so the other outcome won.
        other = Side.NO if side is Side.YES else Side.YES
        result = side.value if payout > 0 else other.value
        return NormalizedSettlement(
            venue_settlement_id=f"{r['transactionHash']}:{r['asset']}",
            ticker=str(r["conditionId"]),
            market_result=result,
            yes_count_micros=count if side is Side.YES else 0,
            no_count_micros=count if side is Side.NO else 0,
            revenue_micros=payout,
            settled_at=_ts(r["timestamp"]),
        )


# ---- public market data --------------------------------------------------------


class PolymarketMarketData(MarketDataSource):
    """CLOB reads, keyed by conditionId. VERIFIED 2026-09-06
    (tests/fixtures/polymarket_market*.json): `tokens[0]` is the first
    outcome (our "yes"), `closed` flips at resolution and the winning token
    carries `winner: true`. `/price?side=buy` is the best bid and
    `side=sell` the best ask (buy 0.77 < sell 0.83 on the captured market);
    `/midpoint` is their midpoint. Unknown ids return 404."""

    venue = "polymarket"

    def __init__(self, clob_url: str, client: httpx.Client | None = None) -> None:
        self.clob_url = clob_url.rstrip("/")
        self.http = _PublicHttp(client)

    def _quote(self, token_id: str, side: str) -> int:
        resp = self.http.get(f"{self.clob_url}/price", {"token_id": token_id, "side": side})
        if resp.status_code != 200:
            return 0
        return micros(resp.json().get("price") or 0)

    def _mid(self, token_id: str) -> int:
        resp = self.http.get(f"{self.clob_url}/midpoint", {"token_id": token_id})
        if resp.status_code != 200:
            return 0
        return micros(resp.json().get("mid") or 0)

    def fetch_market(self, ticker: str) -> NormalizedMarket | None:
        resp = self.http.get(f"{self.clob_url}/markets/{ticker}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        m = resp.json()
        tokens = m.get("tokens") or []
        if not tokens:
            return None
        yes_token = str(tokens[0]["token_id"])
        closed = bool(m.get("closed"))
        result = ""
        if closed:
            winners = [i for i, t in enumerate(tokens) if t.get("winner")]
            if winners == [0]:
                result = "yes"
            elif winners == [1]:
                result = "no"
        bid = ask = last = 0
        if not result:
            bid = self._quote(yes_token, "buy")
            ask = self._quote(yes_token, "sell")
            last = self._mid(yes_token)
        end = m.get("end_date_iso")
        return NormalizedMarket(
            ticker=str(m.get("condition_id") or ticker),
            status="closed" if closed else "active",
            yes_bid_micros=bid,
            yes_ask_micros=ask,
            last_price_micros=last,
            result=result,
            close_time=datetime.fromisoformat(end.replace("Z", "+00:00")) if end else None,
            as_of=datetime.now(UTC),
        )
