"""Polymarket connector.

Polymarket accounts are public on-chain wallets, so there is no credential
at all: the "key id" is the proxy wallet address and every read is an
unauthenticated call to Polymarket's public APIs. Ownership is proved by a
claim code in the trader's profile bio (see venues.py), not by a key.

Account reads use the Data API **v2** (`{data_url}/v2/...`; v1 is retired on
2026-10-24). v2 changes the shape of the data, not its meaning:

  envelope    every payload is {"data": [...], "pagination": {limit, offset,
              has_more, next_cursor}} (v1 returned a bare array). Paging is
              an opaque keyset cursor: send cursor=<next_cursor> until it is
              null (v1: limit/offset, capped). Pages hold up to 1000 rows.
              /v2/activity orders by (block_timestamp, sequence_id) and
              sort_by may only be TIMESTAMP; a cursor binds the sort direction.
  field names snake_case: conditionId -> condition_id, outcomeIndex ->
              outcome_index, usdcSize -> usdc_size, transactionHash ->
              transaction_hash, and `asset` -> `token_id` (same value, the CLOB
              token id). Query params are snake_case too (sort_by,
              sort_direction); `since` still maps to the inclusive `start`.
  history     an omitted `start` is floored to three years back, so a full
              sync sends start=1.
  positions   `status` defaults to OPEN, a superset that includes settled but
              unredeemed winners. v1 hid rows under 1 share (default size
              threshold); v2's floor is 0.1. `size` -> `current_size`,
              `initialValue` -> `entry_cost_usdc` (still the fee-EXCLUSIVE
              basis; `total_cost_usdc` = entry_cost_usdc + entry_fees_usdc).

Sources: https://docs.polymarket.com/migrate/data-api-v1-to-v2.md and
https://data-api.polymarket.com/v2/openapi.json. The mappings are VERIFIED
against real v2 payloads recorded 2026-10-09 (tests/fixtures/polymarket/),
and against the live v1 feed: for a wallet with 113 trades and 73 redeems,
v1 and v2 rows gave identical fill/settlement ids, cash, timestamps and
outcome indexes. What matters for the ledger:

  TRADE    size (shares, float, 6 dp)   price (float, up to 10 dp: volume-
           weighted across the maker fills of one taker order)   usdc_size
           (float, 6 dp) = the NET cash that moved, fee included. So fee =
           |size x price - usdc_size| and the ledger's cash flow reproduces
           usdc_size exactly. Cross-checked: 21.294467 x 0.4696055083 =
           9.999999 gross (a $10 order), 10.265189 net -> fee 0.265190, which
           is 5% x size x p x (1-p) to within the price rounding: Polymarket's
           taker formula at a 5% rate.
  REDEEM   the settlement event: size = tokens redeemed, usdc_size = payout
           ($1 per winning token, 0 for the losing side, redeemed in the same
           transaction).
  position entry_cost_usdc = cost basis net of fees, current_price,
           outcome_index (0 = first outcome = "yes" here).
  balance  NOT observable. Proxy wallets of active traders show 0 USDC.e and
           0 native USDC on-chain (checked against four wallets on
           2026-09-06), so cash lives somewhere the public chain read does not
           see. fetch_balance raises BalanceUnavailable and the account lands
           in the fills-only verification tier.

Ticker = condition_id (one binary market); side = outcome_index 0 -> yes,
1 -> no. Prices are token prices for the side traded, like Kalshi's
no_price for NO fills. Fill identity: transaction_hash:token_id:side:price:
size plus an ordinal for identical rows in the same transaction (there is no
per-fill id; several maker fills share one hash). This is the v1 id with the
v1 names (transactionHash, asset) swapped for their v2 spellings and the same
values, so history ingested from v1 is NOT re-ingested; keep it that way
(fills are append-only and unique on this id).
"""

import time
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import httpx

from alphamarket.connectors.base import (
    Action,
    BalanceUnavailable,
    LeaderboardEntry,
    LeaderboardPeriod,
    MarketDataSource,
    NormalizedBalance,
    NormalizedFill,
    NormalizedMarket,
    NormalizedPosition,
    NormalizedSettlement,
    RateLimited,
    Side,
    VenueConnector,
    leaderboard_figure,
)
from alphamarket.money import MICROS_PER_UNIT

_MICRO = Decimal(1) / MICROS_PER_UNIT
PAGE_SIZE = 1000  # v2 maximum for /v2/activity and /v2/positions
DATA_URL = "https://data-api.polymarket.com"
# Step 7: /v2/leaderboard `time_period` values (OpenAPI, 2026-10-14). A
# cursor pins its board, so later pages send nothing else.
LEADERBOARD_WINDOWS = {
    LeaderboardPeriod.DAY: "day",
    LeaderboardPeriod.WEEK: "week",
    LeaderboardPeriod.MONTH: "month",
    LeaderboardPeriod.ALL: "all",
}
LEADERBOARD_PAGE_MAX = 1000  # `limit` maximum on /v2/leaderboard


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

    def _paged(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        """Every row of a v2 `{data, pagination}` endpoint, following the
        keyset cursor until `next_cursor` is null. A cursor that repeats would
        loop forever, so it is an error."""
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page_params = dict(params)
            if cursor is not None:
                page_params["cursor"] = cursor
            body = self.http.get_json(f"{self.data_url}/v2/{path}", page_params)
            if not isinstance(body, dict):
                raise ValueError(f"unexpected {path} payload")
            data = body.get("data")
            if data is not None and not isinstance(data, list):
                raise ValueError(f"unexpected {path} payload")
            rows.extend(data or [])
            cursor = (body.get("pagination") or {}).get("next_cursor")
            if not cursor:
                return rows
            if cursor in seen:
                raise ValueError(f"{path} cursor did not advance")
            seen.add(cursor)

    def _activity(self, kind: str, since: datetime | None) -> list[dict[str, Any]]:
        """All activity rows of one type, oldest first, via cursor paging."""
        return self._paged(
            "activity",
            {
                "user": self.address,
                "type": kind,
                "limit": PAGE_SIZE,
                "sort_by": "TIMESTAMP",
                "sort_direction": "ASC",
                # An omitted start is floored to three years back; 1 is full history.
                "start": int(since.timestamp()) if since is not None else 1,
            },
        )

    def _profile(self) -> dict[str, Any]:
        return self.http.get_json(f"{self.gamma_url}/public-profile", {"address": self.address})

    def _side_of(self, row: dict[str, Any]) -> Side:
        side = _side_from_index(row.get("outcome_index"))
        if side is not None:
            return side
        # An outcome that cannot be labeled is reported as outcome_index 999;
        # resolve via the market's token list and remember it.
        asset = str(row["token_id"])
        if asset not in self._token_sides:
            market = self.http.get_json(f"{self.clob_url}/markets/{row['condition_id']}")
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
        # status=OPEN (the default, stated for clarity) is the superset: it
        # includes settled-but-unredeemed winners, which still hold tokens.
        rows = self._paged(
            "positions", {"user": self.address, "status": "OPEN", "limit": PAGE_SIZE}
        )
        for p in rows:
            size = micros(p.get("current_size") or 0)
            if size == 0:
                continue
            side = self._side_of(p)
            out.append(
                NormalizedPosition(
                    ticker=str(p["condition_id"]),
                    position_micros=size if side is Side.YES else -size,
                    # entry_cost_usdc (v1 initialValue) is the cost basis net
                    # of entry fees, which is what the ledger carries open
                    # positions at.
                    market_exposure_micros=micros(p.get("entry_cost_usdc") or 0),
                )
            )
        return out

    # ---- mapping ---------------------------------------------------------

    def _to_fill(self, r: dict[str, Any]) -> NormalizedFill:
        side = self._side_of(r)
        action = Action(str(r["side"]).lower())  # "BUY" | "SELL"
        count = micros(r["size"])
        price = micros(r["price"])
        gross = micros(_dec(r["size"]) * _dec(r["price"]))
        net = micros(r["usdc_size"])
        # The fee is whatever separates gross from the cash that moved. A
        # zero-fee market can round gross one micro past net; clamp so the
        # fee is never negative and the cash flow is still exactly `net`.
        fee = net - gross if action is Action.BUY else gross - net
        if fee < 0:
            gross, fee = net, 0
        return NormalizedFill(
            venue_fill_id=(
                f"{r['transaction_hash']}:{r['token_id']}:{action.value}:{price}:{count}"
            ),
            ticker=str(r["condition_id"]),
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
        payout = micros(r.get("usdc_size") or 0)
        # A payout means this token won; a zero payout means it was the
        # losing side being burned, so the other outcome won.
        other = Side.NO if side is Side.YES else Side.YES
        result = side.value if payout > 0 else other.value
        return NormalizedSettlement(
            venue_settlement_id=f"{r['transaction_hash']}:{r['token_id']}",
            ticker=str(r["condition_id"]),
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

    def __init__(
        self,
        clob_url: str,
        client: httpx.Client | None = None,
        data_url: str = DATA_URL,
        leaderboard_keep: int = LEADERBOARD_PAGE_MAX,
    ) -> None:
        self.clob_url = clob_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self.leaderboard_keep = leaderboard_keep
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

    # ---- copy strategies step 7: the trader leaderboard (discover.py) ----

    def fetch_leaderboards(
        self, periods: tuple[LeaderboardPeriod, ...]
    ) -> dict[LeaderboardPeriod, list[LeaderboardEntry]]:
        """The top `leaderboard_keep` rows of the PNL board, overall category,
        per period, in the venue's rank order. One request per period while
        `leaderboard_keep` <= 1000 (the page maximum); deeper boards follow
        `next_cursor`.

        `GET {data_url}/v2/leaderboard?time_period=&limit=` (VERIFIED live and
        against /v2/openapi.json, 2026-10-14). Rows: rank (the venue's
        competition rank: ties share one and the next skips), user_id (the
        wallet, lowercase), pnl (float, USDC; day/week/month are the MARKED
        equity change net of flows, `all` is realized only), volume (float,
        in SHARES, not USD), user_name, profile_image, x_username, verified.
        Kept: user_id, rank, pnl (rounded to 6 dp, leaderboard_figure).
        Dropped: every identity field (user_name, profile_image, x_username,
        verified) and volume, because LeaderboardEntry.volume is in the quote
        asset and shares are not dollars. There is no roi and no account
        value on this board."""
        out: dict[LeaderboardPeriod, list[LeaderboardEntry]] = {}
        for period in periods:
            window = LEADERBOARD_WINDOWS.get(period)
            if window is not None:
                out[period] = self._leaderboard(window)
        return out

    def _leaderboard(self, window: str) -> list[LeaderboardEntry]:
        rows: list[LeaderboardEntry] = []
        params: dict[str, Any] = {
            "time_period": window,
            "limit": min(self.leaderboard_keep, LEADERBOARD_PAGE_MAX),
        }
        seen: set[str] = set()
        while len(rows) < self.leaderboard_keep:
            self.throttle("leaderboard")
            resp = self.http.get(f"{self.data_url}/v2/leaderboard", params)
            if resp.status_code == 429:  # still busy after _PublicHttp's own retries
                retry = resp.headers.get("Retry-After") or ""
                wait = float(retry) if retry.replace(".", "", 1).isdigit() else 10.0
                raise RateLimited(wait, "leaderboard")
            resp.raise_for_status()
            payload = resp.json()
            for r in payload.get("data") or []:
                address = str(r.get("user_id") or "").lower()
                if not is_address(address):
                    continue
                rows.append(
                    LeaderboardEntry(
                        address=address,
                        rank=int(r["rank"]),
                        pnl=leaderboard_figure(_dec(r["pnl"])),
                    )
                )
            cursor = (payload.get("pagination") or {}).get("next_cursor")
            if not cursor or cursor in seen:
                break
            seen.add(cursor)
            params = {"cursor": cursor}  # the cursor pins the board and window
        return rows[: self.leaderboard_keep]
