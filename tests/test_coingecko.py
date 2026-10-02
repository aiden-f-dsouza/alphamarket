"""CoinGecko fallback price source (wave2 D13): the 00:00 UTC close rule, exact
decimals, the curated no-search id map, throttling, error mapping, and that it
is a data source only (never a connectable venue). Fixtures are hand-built from
the documented response shape (tests/fixtures/coingecko/README.md)."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.config import get_settings
from alphamarket.connectors.base import RateLimited
from alphamarket.connectors.coingecko import (
    DEMO_KEY_HEADER,
    ENDPOINT_RANGE,
    CoinGeckoError,
    CoinGeckoMarketData,
)
from alphamarket.connectors.registry import (
    COINGECKO_COIN_IDS,
    DATA_SOURCE_BUDGETS,
    PROFILES,
    build_connector,
    build_market_data,
)

FIXTURES = Path(__file__).parent / "fixtures" / "coingecko"
BASE = "https://api.coingecko.com/api/v3"
NOW = datetime(2026, 9, 24, 4, 15, tzinfo=UTC)
IDS = {"BTC": "bitcoin", "SHIB": "shiba-inu"}


def _body(name: str) -> str:
    return (FIXTURES / name).read_text()


def _source(key: str = "demo-key-123", clock_now: datetime = NOW) -> CoinGeckoMarketData:
    return CoinGeckoMarketData(IDS, api_key=key, base_url=BASE, clock=lambda: clock_now)


def _ts(text: str) -> int:
    return int(datetime.fromisoformat(text).timestamp())


class _Throttle:
    def __init__(self) -> None:
        self.endpoints: list[str] = []

    def before(self, endpoint: str) -> None:
        self.endpoints.append(endpoint)

    def after(self, endpoint: str, extra_cost: int = 0, used: int | None = None) -> None:
        pass


# ---- the close rule ---------------------------------------------------------------------------


@respx.mock
def test_close_of_day_d_is_the_price_at_midnight_of_the_next_day():
    route = respx.get(f"{BASE}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, text=_body("market_chart_range_bitcoin_hourly.json"))
    )
    closes = _source().fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 22))
    # Day 20 closes at 09-21T00:00:42Z (42 s from midnight), day 21 at 09-22T00:00:05Z,
    # day 22 at 09-23T00:01:00Z. Exact digits survive (more than a float keeps).
    assert [(c.day.isoformat(), c.close) for c in closes] == [
        ("2026-09-20", "61234.56789123"),
        ("2026-09-21", "62111.25"),
        ("2026-09-22", "62345.5"),
    ]
    assert all(c.symbol == "bitcoin" for c in closes)
    # One request; the window spans the three midnights with two hours of padding.
    assert route.call_count == 1
    params = route.calls[0].request.url.params
    assert params["vs_currency"] == "usd"
    assert int(params["from"]) == _ts("2026-09-21T00:00:00+00:00") - 2 * 3600
    assert int(params["to"]) == _ts("2026-09-23T00:00:00+00:00") + 2 * 3600


@respx.mock
def test_sub_cent_price_keeps_every_digit_and_daily_samples_work():
    respx.get(f"{BASE}/coins/shiba-inu/market_chart/range").mock(
        return_value=httpx.Response(200, text=_body("market_chart_range_shiba_daily.json"))
    )
    closes = _source().fetch_daily_closes("shiba-inu", date(2026, 9, 20), date(2026, 9, 21))
    assert [c.close for c in closes] == ["0.000012345678", "0.000012"]


@respx.mock
def test_midnight_without_a_nearby_sample_has_no_close():
    respx.get(f"{BASE}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, text=_body("market_chart_range_sparse.json"))
    )
    closes = _source().fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 22))
    # Day 21's midnight (09-22T00:00Z) has samples 1 h before and 24 h after: outside 30 min.
    assert [(c.day.isoformat(), c.close) for c in closes] == [
        ("2026-09-20", "100.5"),
        ("2026-09-22", "102.25"),
    ]


@respx.mock
def test_a_close_whose_midnight_has_not_happened_is_dropped():
    respx.get(f"{BASE}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, text=_body("market_chart_range_bitcoin_hourly.json"))
    )
    noon = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
    closes = _source(clock_now=noon).fetch_daily_closes(
        "bitcoin", date(2026, 9, 20), date(2026, 9, 22)
    )
    # Day 22 would close at 09-23T00:00Z, still in the future at noon on the 22nd.
    assert [c.day.isoformat() for c in closes] == ["2026-09-20", "2026-09-21"]


def test_empty_range_makes_no_request():
    with respx.mock:
        assert _source().fetch_daily_closes("bitcoin", date(2026, 9, 22), date(2026, 9, 20)) == []


# ---- identity: curated map, never search --------------------------------------------------------


def test_curated_map_only_and_synthetic_instruments_need_no_request():
    with respx.mock:  # any HTTP call would fail the test: no route is registered
        src = _source()
        assert src.pricing_symbol("BTC") == "bitcoin"
        assert src.pricing_symbol("NOPECOIN") is None  # not searched, not guessed
        assert src.pricing_symbol("USDC") is None  # par, never priced
        instruments = {i.base_asset: i for i in src.fetch_instruments()}
    btc = instruments["BTC"]
    assert (btc.symbol, btc.quote_asset, btc.type.value) == ("bitcoin", "USD", "spot")


# ---- auth, throttle, errors ---------------------------------------------------------------------


@respx.mock
def test_demo_key_header_sent_only_when_configured_and_never_in_repr():
    route = respx.get(f"{BASE}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, text=_body("market_chart_range_bitcoin_hourly.json"))
    )
    keyed = _source("demo-key-123")
    keyed.fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 20))
    assert route.calls[0].request.headers[DEMO_KEY_HEADER] == "demo-key-123"
    assert "demo-key-123" not in repr(keyed)
    _source("").fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 20))
    assert DEMO_KEY_HEADER not in route.calls[1].request.headers


@respx.mock
def test_every_request_is_throttled_first():
    respx.get(f"{BASE}/coins/bitcoin/market_chart/range").mock(
        return_value=httpx.Response(200, text=_body("market_chart_range_bitcoin_hourly.json"))
    )
    src = _source()
    throttle = _Throttle()
    src.set_throttle(throttle)
    src.fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 20))
    assert throttle.endpoints == [ENDPOINT_RANGE]


@respx.mock
def test_error_mapping():
    route = respx.get(f"{BASE}/coins/bitcoin/market_chart/range")
    route.mock(return_value=httpx.Response(404, text=_body("error_coin_not_found.json")))
    assert _source().fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 20)) == []

    route.mock(return_value=httpx.Response(429, headers={"Retry-After": "42"}, text="{}"))
    with pytest.raises(RateLimited) as limited:
        _source().fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 20))
    assert limited.value.retry_after_seconds == 42
    assert limited.value.endpoint == ENDPOINT_RANGE

    route.mock(return_value=httpx.Response(401, text=_body("error_invalid_key.json")))
    with pytest.raises(CoinGeckoError) as failed:
        _source().fetch_daily_closes("bitcoin", date(2026, 9, 20), date(2026, 9, 20))
    assert failed.value.status == 401
    assert "invalid API key" in str(failed.value)


# ---- a data source, never a venue ---------------------------------------------------------------


def test_registered_as_market_data_only():
    assert "coingecko" not in PROFILES  # no profile: it cannot be connected
    with pytest.raises(ValueError):
        build_connector("coingecko", "prod", "key", None)
    assert "coingecko" in DATA_SOURCE_BUDGETS  # throttled like a venue
    settings = get_settings()
    original = settings.coingecko_api_key
    settings.coingecko_api_key = "from-settings"
    try:
        built = build_market_data("coingecko", "prod")
    finally:
        settings.coingecko_api_key = original
    assert isinstance(built, CoinGeckoMarketData)
    assert built.coin_ids == COINGECKO_COIN_IDS
    assert built._api_key == "from-settings"


def test_curated_ids_are_unique_and_stablecoin_free():
    assert len(set(COINGECKO_COIN_IDS.values())) == len(COINGECKO_COIN_IDS)
    assert not {"USD", "USDC", "USDT"} & set(COINGECKO_COIN_IDS)
    assert json.dumps(sorted(COINGECKO_COIN_IDS))  # codes are plain strings
