"""CoinGecko fallback prices (wave2, plan D13).

NOT a venue: there is no VenueProfile, no credential and no account, so it
lives only in registry._MARKET_DATA_BUILDERS and can never be connected. It
prices crypto assets that no connected venue lists (a coin held on Coinbase
but not on Kraken, say) so a holdings value always has a USD price, at the
cost of a different source's quote. Every row it produces is labelled
`source="coingecko"` and the public methodology says so.

Identity. CoinGecko names coins by id ("bitcoin"), not by ticker, and
tickers collide. Koine therefore keeps a CURATED asset -> coin id map
(registry.COINGECKO_COIN_IDS, passed in as `coin_ids`) and never searches: an
asset that is not in the map stays unpriced (and is shown as such) rather
than being matched to whatever coin happens to share its ticker. The
synthetic instrument a price attaches to is venue "coingecko", symbol = the
coin id, base_asset = the asset code, quote_asset = USD.

Closes. Day d's close is the price at 00:00 UTC of d+1, so a close is "the
last price of that UTC day" exactly like an exchange's daily candle.
Endpoint [VERIFY against the account's plan]:
    GET {base}/coins/{id}/market_chart/range?vs_currency=usd&from=<unix>&to=<unix>
    -> {"prices": [[unix_ms, price], ...], ...}
CoinGecko's automatic granularity is hourly for ranges of 2-90 days and
daily (00:00 UTC) beyond, so each midnight is read as the sample nearest to
it within MIDNIGHT_TOLERANCE (ties go to the earlier sample); a midnight with
no sample that close has no close. Numbers are parsed as Decimal straight
from the JSON text, so a sub-cent price keeps every digit CoinGecko sent.

Auth. The demo plan sends `x-cg-demo-api-key`; with no key the same endpoint
answers under a much smaller anonymous limit. The budget lives in
registry.DATA_SOURCE_BUDGETS and is charged through throttle() before every
request; a 429 raises RateLimited.
"""

import json
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from alphamarket.connectors.base import (
    AssetMarketData,
    DailyClose,
    InstrumentType,
    NormalizedInstrument,
    RateLimited,
)

COINGECKO_API_URL = "https://api.coingecko.com/api/v3"
DEMO_KEY_HEADER = "x-cg-demo-api-key"
SOURCE = "coingecko"
QUOTE = "USD"
QTY_DECIMALS = 8  # synthetic instruments are never traded; a valid placeholder

ENDPOINT_RANGE = "MarketChartRange"
MIDNIGHT_TOLERANCE = timedelta(minutes=30)
DEFAULT_RETRY_AFTER = 60.0
# The demo plan serves about the past 365 days of history and answers an
# older range with an error [VERIFY the exact limit and error]. Closes are
# requested only inside it, so an old anchor costs its oldest days (they stay
# unpriced) rather than failing the whole request every night.
HISTORY_DAYS = 365
# Query this far either side of the wanted midnights so the nearest sample
# is inside the response whichever granularity CoinGecko picks.
_PAD = timedelta(hours=2)


class CoinGeckoError(Exception):
    def __init__(self, status: int, message: str, endpoint: str = "") -> None:
        super().__init__(f"CoinGecko {endpoint or 'request'} failed ({status}): {message}")
        self.status = status
        self.endpoint = endpoint


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


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


class CoinGeckoMarketData(AssetMarketData):
    """Daily USD closes for the curated coin list. Quotes are not offered
    (CoinGecko is a daily-price fallback, never a fill price)."""

    venue = SOURCE

    def __init__(
        self,
        coin_ids: Mapping[str, str],
        api_key: str = "",
        base_url: str = COINGECKO_API_URL,
        client: httpx.Client | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.clock = clock
        self.coin_ids = dict(coin_ids)
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=20.0)

    def __repr__(self) -> str:  # never the key
        return f"CoinGeckoMarketData(base_url={self.base_url!r}, coins={len(self.coin_ids)})"

    # ---- identity (no HTTP) ----

    def fetch_instruments(self) -> list[NormalizedInstrument]:
        """The curated coins as synthetic instruments (no request)."""
        return [self.instrument(asset) for asset in sorted(self.coin_ids)]

    def instrument(self, asset: str) -> NormalizedInstrument:
        return NormalizedInstrument(
            symbol=self.coin_ids[asset],
            type=InstrumentType.SPOT,
            base_asset=asset,
            quote_asset=QUOTE,
            qty_decimals=QTY_DECIMALS,
        )

    def pricing_symbol(
        self, asset: str, instruments: list[NormalizedInstrument] | None = None
    ) -> str | None:
        """The coin id of a curated asset; None for anything else (stablecoins
        at par included, they never need a price). Never searches."""
        if asset in self.STABLECOINS_AT_PAR:
            return None
        return self.coin_ids.get(asset)

    # ---- prices ----

    def _get(self, endpoint: str, path: str, params: dict[str, Any]) -> Any:
        self.throttle(endpoint)
        headers = {DEMO_KEY_HEADER: self._api_key} if self._api_key else {}
        resp = self.client.get(f"{self.base_url}{path}", params=params, headers=headers)
        if resp.status_code in (429, 418):
            raise RateLimited(_retry_after(resp), endpoint)
        if resp.status_code == 404:
            return None  # unknown coin id: nothing to price
        if resp.status_code >= 400:
            raise CoinGeckoError(resp.status_code, _error_text(resp), endpoint)
        # parse_float=Decimal keeps every digit of a sub-cent price.
        return json.loads(resp.text, parse_float=Decimal)

    def fetch_daily_closes(self, symbol: str, start: date, end: date) -> list[DailyClose]:
        """UTC daily closes for [start, end] of coin id `symbol`, oldest first.
        The close of day d is the price at 00:00 UTC of d+1; a close whose
        midnight is still in the future is not a close and is dropped. Days
        older than HISTORY_DAYS are not requested (no closes for them)."""
        now = self.clock()
        # One day of margin inside the limit: the request starts 2 h before
        # the first wanted midnight.
        start = max(start, now.astimezone(UTC).date() - timedelta(days=HISTORY_DAYS - 2))
        if end < start:
            return []
        first = _midnight(start + timedelta(days=1))
        last = _midnight(end + timedelta(days=1))
        body = self._get(
            ENDPOINT_RANGE,
            f"/coins/{symbol}/market_chart/range",
            {
                "vs_currency": "usd",
                "from": int((first - _PAD).timestamp()),
                "to": int((last + _PAD).timestamp()),
            },
        )
        samples = _samples(body)
        out: list[DailyClose] = []
        day = start
        while day <= end:
            target = _midnight(day + timedelta(days=1))
            if target <= now:
                price = _nearest(samples, target)
                if price is not None:
                    out.append(DailyClose(symbol=symbol, day=day, close=format(price, "f")))
            day += timedelta(days=1)
        return out


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        status = body.get("status")
        if isinstance(status, dict) and status.get("error_message"):
            return str(status["error_message"])[:200]
        if body.get("error"):
            return str(body["error"])[:200]
    return resp.text[:200]


def _samples(body: Any) -> list[tuple[datetime, Decimal]]:
    """(time, price) pairs from a market_chart response, oldest first; rows
    that are not positive prices are ignored."""
    out: list[tuple[datetime, Decimal]] = []
    for row in (body or {}).get("prices") or []:
        try:
            ms, price = row[0], Decimal(str(row[1]))
            when = datetime.fromtimestamp(int(ms) / 1000, UTC)
        except (TypeError, ValueError, IndexError, InvalidOperation, OverflowError):
            continue
        if price.is_finite() and price > 0:
            out.append((when, price))
    out.sort(key=lambda s: s[0])
    return out


def _nearest(samples: list[tuple[datetime, Decimal]], target: datetime) -> Decimal | None:
    """The sample closest to `target` within MIDNIGHT_TOLERANCE (earlier wins a tie)."""
    best: tuple[timedelta, datetime, Decimal] | None = None
    for when, price in samples:
        gap = abs(when - target)
        if gap > MIDNIGHT_TOLERANCE:
            continue
        if best is None or (gap, when) < (best[0], best[1]):
            best = (gap, when, price)
    return best[2] if best else None
