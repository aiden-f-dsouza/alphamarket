"""Binance.US connector tests. respx only (invariant #6: no live API calls).

Fixture provenance: tests/fixtures/binanceus/README.md.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.connectors import binanceus as m
from alphamarket.connectors.base import Action, LegKind, RateLimited

FIXTURES = Path(__file__).parent / "fixtures" / "binanceus"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _connector(client: httpx.Client | None = None) -> m.BinanceUSConnector:
    return m.BinanceUSConnector(
        base_url=m.BASE_URL, api_key="test-key", api_secret="test-secret", client=client
    )


# ---- signing ---------------------------------------------------------------


def test_sign_matches_published_binance_test_vector():
    """The published HMAC-SHA256 signing example from Binance's own API docs
    (https://developers.binance.com/docs/binance-spot-api-docs/rest-api/request-security,
    checked 2026-09-26): the same algorithm docs.binance.us documents for
    Binance.US (X-MBX-APIKEY + HMAC-SHA256 over the query string)."""
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    connector = m.BinanceUSConnector(base_url=m.BASE_URL, api_key="unused", api_secret=secret)
    params = {
        "symbol": "LTCBTC",
        "side": "BUY",
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": 1,
        "price": 0.1,
        "recvWindow": 5000,
        "timestamp": 1499827319559,
    }
    signed = connector._sign(params)
    assert signed["signature"] == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


# ---- read-only proof ---------------------------------------------------


@respx.mock
def test_prove_read_only_true_when_only_reading_enabled():
    respx.get(f"{m.BASE_URL}{m.EP_API_RESTRICTIONS}").mock(
        return_value=httpx.Response(200, json=_load("apirestrictions_readonly.json"))
    )
    connector = _connector()
    assert connector.check_write_scope() is False
    evidence = connector.prove_read_only()
    assert evidence.read_only is True
    assert evidence.method.value == "scope_endpoint"
    assert "secret" not in json.dumps(evidence.details)  # never secrets


@respx.mock
def test_prove_read_only_false_when_trading_enabled():
    respx.get(f"{m.BASE_URL}{m.EP_API_RESTRICTIONS}").mock(
        return_value=httpx.Response(200, json=_load("apirestrictions_writable.json"))
    )
    connector = _connector()
    assert connector.check_write_scope() is True
    evidence = connector.prove_read_only()
    assert evidence.read_only is False


@respx.mock
def test_prove_read_only_undetermined_on_http_error():
    respx.get(f"{m.BASE_URL}{m.EP_API_RESTRICTIONS}").mock(return_value=httpx.Response(500))
    connector = _connector()
    assert connector.check_write_scope() is None
    assert connector.prove_read_only().read_only is None


# ---- rate limiting -----------------------------------------------------


@respx.mock
def test_429_raises_rate_limited_with_retry_after():
    """RateLimited is not an httpx.HTTPError, so it propagates straight
    through check_write_scope()'s `except httpx.HTTPError` instead of being
    swallowed into a `None` (undetermined) result."""
    respx.get(f"{m.BASE_URL}{m.EP_API_RESTRICTIONS}").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"})
    )
    connector = _connector()
    with pytest.raises(RateLimited) as exc_info:
        connector.check_write_scope()
    assert exc_info.value.retry_after_seconds == 12.0


@respx.mock
def test_418_also_raises_rate_limited():
    respx.get(f"{m.BASE_URL}{m.EP_ACCOUNT}").mock(return_value=httpx.Response(418))
    connector = _connector()
    with pytest.raises(RateLimited) as exc_info:
        connector.fetch_balances()
    assert exc_info.value.retry_after_seconds == 60.0  # no Retry-After header -> default


# ---- balances ------------------------------------------------------------


@respx.mock
def test_fetch_balances_hand_computed_atoms():
    respx.get(f"{m.BASE_URL}{m.EP_ACCOUNT}").mock(
        return_value=httpx.Response(200, json=_load("account.json"))
    )
    respx.get(f"{m.BASE_URL}{m.EP_EXCHANGE_INFO}").mock(
        return_value=httpx.Response(200, json=_load("exchangeinfo.json"))
    )
    connector = _connector()
    balances = connector.fetch_balances()
    by_asset = {a.asset: a for a in balances.assets}
    # ETH is all-zero (free 0 + locked 0) and must be dropped entirely.
    assert "ETH" not in by_asset
    # BTC: free "0.50000000" (8dp, from exchangeInfo baseAssetPrecision) ->
    # 0.5 * 1e8 = 50_000_000 atoms.
    assert by_asset["BTC"].atoms == 50_000_000
    assert by_asset["BTC"].decimals == 8
    # USD: free 1234.56 + locked 10.00 = 1244.56, at 8dp (quoteAssetPrecision)
    # -> 1244.56 * 1e8 = 124_456_000_000 atoms.
    assert by_asset["USD"].atoms == 124_456_000_000
    assert by_asset["USD"].decimals == 8


@respx.mock
def test_fetch_balance_converts_finer_than_6dp_exactly():
    """AssetVenueConnector's default fetch_balance() only accepts <=6dp USD;
    Binance.US reports USD at 8dp, so the override must still produce an
    exact micros figure: 1244.56000000 USD -> to_micros == 1_244_560_000."""
    respx.get(f"{m.BASE_URL}{m.EP_ACCOUNT}").mock(
        return_value=httpx.Response(200, json=_load("account.json"))
    )
    respx.get(f"{m.BASE_URL}{m.EP_EXCHANGE_INFO}").mock(
        return_value=httpx.Response(200, json=_load("exchangeinfo.json"))
    )
    connector = _connector()
    balance = connector.fetch_balance()
    assert balance.balance_micros == 1_244_560_000


# ---- fill mapping (pure, hand-computed) ------------------------------------


def test_to_asset_fill_buy_with_fee_in_base_asset():
    inst = m._Instrument(
        symbol="BTCUSD",
        base_asset="BTC",
        quote_asset="USD",
        base_decimals=8,
        quote_decimals=8,
        qty_step_atoms=1000,
        min_qty_atoms=1000,
        min_notional="1.00000000",
    )
    trade = _load("mytrades_btcusd.json")[0]  # isBuyer, commissionAsset BTC
    fill = m._to_asset_fill("BTCUSD", inst, trade)

    assert fill.venue_fill_id == "BTCUSD:28457"
    assert fill.action == Action.BUY
    # qty "0.01000000" BTC @ 8dp -> 0.01 * 1e8 = 1_000_000 atoms.
    assert fill.qty_atoms == 1_000_000
    assert fill.price == "84300.00000000"

    legs = {(leg.kind, leg.asset): leg.delta_atoms for leg in fill.legs}
    # FILL_BASE: +0.01 BTC = +1_000_000 atoms.
    assert legs[(LegKind.FILL_BASE, "BTC")] == 1_000_000
    # FILL_QUOTE: quoteQty "843.00000000" USD @ 8dp = 84_300_000_000 atoms,
    # negative because USD was spent on a buy.
    assert legs[(LegKind.FILL_QUOTE, "USD")] == -84_300_000_000
    # FEE: commission "0.00001000" BTC @ 8dp = 1_000 atoms, always negative.
    assert legs[(LegKind.FEE, "BTC")] == -1_000
    assert fill.liquidity == "taker"  # isMaker: false


def test_to_asset_fill_sell_with_fee_in_quote_asset():
    inst = m._Instrument(
        symbol="BTCUSD", base_asset="BTC", quote_asset="USD", base_decimals=8, quote_decimals=8
    )
    trade = _load("mytrades_btcusd.json")[1]  # not isBuyer, commissionAsset USD
    fill = m._to_asset_fill("BTCUSD", inst, trade)

    assert fill.action == Action.SELL
    legs = {(leg.kind, leg.asset): leg.delta_atoms for leg in fill.legs}
    # qty "0.00500000" BTC -> -500_000 atoms (sold).
    assert legs[(LegKind.FILL_BASE, "BTC")] == -500_000
    # quoteQty "422.50000000" USD -> +42_250_000_000 atoms (received).
    assert legs[(LegKind.FILL_QUOTE, "USD")] == 42_250_000_000
    # commission "0.42250000" USD @ 8dp -> -42_250_000 atoms.
    assert legs[(LegKind.FEE, "USD")] == -42_250_000
    assert fill.liquidity == "maker"


def test_no_duplicate_legs_validator_still_passes():
    """Guards against a future edit reintroducing a (kind, asset) collision:
    the model validator would raise if it did."""
    inst = m._Instrument(
        symbol="BTCUSD", base_asset="BTC", quote_asset="USD", base_decimals=8, quote_decimals=8
    )
    fill = m._to_asset_fill("BTCUSD", inst, _load("mytrades_btcusd.json")[0])
    keys = [(leg.kind, leg.asset) for leg in fill.legs]
    assert len(keys) == len(set(keys))


# ---- merge_legs --------------------------------------------------------


def test_merge_legs_sums_same_asset_and_kind():
    rows = [
        ("TRX", LegKind.CONVERT, -1_000_000_000, 8),
        ("TRX", LegKind.CONVERT, -500_000_000, 8),
        ("BNB", LegKind.CONVERT, 300_000, 8),
    ]
    legs = m._merge_legs(rows)
    by_key = {(leg.asset, leg.kind): leg.delta_atoms for leg in legs}
    assert by_key[("TRX", LegKind.CONVERT)] == -1_500_000_000
    assert by_key[("BNB", LegKind.CONVERT)] == 300_000


# ---- events (pure mapping) ----------------------------------------------


def _decimals_for(asset: str) -> int:
    return 8


def test_deposit_event_hand_computed():
    event = m._deposit_event(_load("deposit_history.json")[0], _decimals_for)
    assert event.venue_ref == "deposit:d_id_123"
    assert event.kind == LegKind.DEPOSIT
    leg = event.legs[0]
    # "0.50000000" BTC @ 8dp -> 50_000_000 atoms, positive (money arriving).
    assert leg.asset == "BTC"
    assert leg.delta_atoms == 50_000_000
    assert leg.kind == LegKind.DEPOSIT
    assert event.occurred_at == datetime.fromtimestamp(1790035200, UTC)


def test_withdraw_event_hand_computed_with_fee_and_string_date():
    event = m._withdraw_event(_load("withdraw_history.json")[0], _decimals_for)
    assert event.venue_ref == "withdraw:w_id_456"
    assert event.kind == LegKind.WITHDRAWAL
    legs = {leg.kind: leg.delta_atoms for leg in event.legs}
    # "0.10000000" BTC @ 8dp -> -10_000_000 atoms (money leaving).
    assert legs[LegKind.WITHDRAWAL] == -10_000_000
    # transactionFee "0.00050000" BTC @ 8dp -> -50_000 atoms.
    assert legs[LegKind.FEE] == -50_000
    # applyTime "2026-08-01 10:00:00" is a naive UTC string, not epoch ms.
    assert event.occurred_at == datetime(2026, 8, 1, 10, 0, 0, tzinfo=UTC)


def test_convert_event_hand_computed():
    event = m._convert_event(_load("convert_tradeflow.json")["list"][0], _decimals_for)
    assert event.venue_ref == "convert:940708407462087195"
    legs = {leg.asset: leg.delta_atoms for leg in event.legs}
    # fromAmount "100.00000000" USD @ 8dp -> -10_000_000_000 atoms.
    assert legs["USD"] == -10_000_000_000
    # toAmount "0.00118500" BTC @ 8dp -> +118_500 atoms.
    assert legs["BTC"] == 118_500


def test_dust_event_merges_same_source_asset():
    batch = _load("dustlog.json")["userAssetDribbletDetails"][0]
    event = m._dust_event(batch, _decimals_for)
    assert event.venue_ref == "dust:45178372831"
    legs = {(leg.asset, leg.kind): leg.delta_atoms for leg in event.legs}
    # Two TRX detail rows, amounts "10.00000000" and "5.00000000" @ 8dp,
    # both debited (negative): -1_000_000_000 + -500_000_000 = -1_500_000_000.
    # _merge_legs must combine them into ONE leg (same asset+kind) or the
    # NormalizedEvent model validator would raise on construction.
    assert legs[("TRX", LegKind.CONVERT)] == -1_500_000_000
    # totalTransferedAmount "0.00500000" BNB @ 8dp -> +500_000 atoms.
    assert legs[("BNB", LegKind.CONVERT)] == 500_000
    assert len(event.legs) == 2  # not 3: the two TRX rows merged into one


# ---- pagination / cursors -----------------------------------------------


@respx.mock
def test_fetch_asset_fills_page_first_pass_window_mode_then_terminal(monkeypatch):
    """`since` is set one hour before a frozen "now" so the very first
    window (since .. since+24h) already covers "now": one myTrades call
    finishes the (only) symbol and the page is terminal (next_cursor None,
    resume_cursor set with the discovered from_id for later incremental
    syncs). `_now_ms` is frozen so the window-vs-now comparison in
    `fetch_asset_fills_page` cannot race against wall-clock time."""
    frozen_now_ms = m.VENUE_EPOCH_MS + 10 * m.DAY_MS
    monkeypatch.setattr(m, "_now_ms", lambda: frozen_now_ms)
    since = m._ms_to_dt(frozen_now_ms - 3_600_000)  # one hour before frozen "now"

    respx.get(f"{m.BASE_URL}{m.EP_ACCOUNT}").mock(
        return_value=httpx.Response(200, json=_load("account.json"))
    )
    respx.get(f"{m.BASE_URL}{m.EP_DEPOSIT_HISTORY}").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{m.BASE_URL}{m.EP_EXCHANGE_INFO}").mock(
        return_value=httpx.Response(200, json=_load("exchangeinfo.json"))
    )
    trades_route = respx.get(f"{m.BASE_URL}{m.EP_MY_TRADES}").mock(
        return_value=httpx.Response(200, json=_load("mytrades_btcusd.json"))
    )

    connector = _connector()
    page = connector.fetch_asset_fills_page(cursor=None, since=since)

    assert trades_route.call_count == 1
    request = trades_route.calls[0].request
    query = dict(httpx.QueryParams(request.url.query))
    assert query["symbol"] == "BTCUSD"
    assert "startTime" in query and "endTime" in query
    assert "fromId" not in query  # first-ever sync for this symbol: window mode

    assert len(page.items) == 2
    assert page.items[0].venue_fill_id == "BTCUSD:28457"
    assert page.next_cursor is None
    assert page.resume_cursor is not None
    state = json.loads(page.resume_cursor)
    assert state["from_ids"]["BTCUSD"] == 28458  # last trade id seen
    assert state["queue"] == []  # pass complete


@respx.mock
def test_fetch_asset_fills_page_resumes_with_from_id():
    """A stored resume_cursor from a previous pass (from_ids known, queue
    empty) re-discovers symbols and, for a symbol with a known from_id,
    pages by fromId (not by window) -- cursor beats since."""
    respx.get(f"{m.BASE_URL}{m.EP_ACCOUNT}").mock(
        return_value=httpx.Response(200, json=_load("account.json"))
    )
    respx.get(f"{m.BASE_URL}{m.EP_DEPOSIT_HISTORY}").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"{m.BASE_URL}{m.EP_EXCHANGE_INFO}").mock(
        return_value=httpx.Response(200, json=_load("exchangeinfo.json"))
    )
    trades_route = respx.get(f"{m.BASE_URL}{m.EP_MY_TRADES}").mock(
        return_value=httpx.Response(200, json=[_load("mytrades_btcusd.json")[1]])
    )

    connector = _connector()
    prior_state = {
        "symbols": ["BTCUSD"],
        "from_ids": {"BTCUSD": 28457},
        "queue": [],
        "window_start_ms": None,
    }
    # `since` is a much older timestamp than the known from_id's implied
    # progress; because a from_id is already known for BTCUSD, it must be
    # ignored ("cursor beats since").
    page = connector.fetch_asset_fills_page(
        cursor=json.dumps(prior_state), since=datetime(2019, 1, 1, tzinfo=UTC)
    )

    assert trades_route.call_count == 1
    query = dict(httpx.QueryParams(trades_route.calls[0].request.url.query))
    assert query["fromId"] == "28458"  # last known id + 1
    assert "startTime" not in query

    assert len(page.items) == 1
    assert page.next_cursor is None
    state = json.loads(page.resume_cursor)
    assert state["from_ids"]["BTCUSD"] == 28458


@respx.mock
def test_fetch_asset_fills_page_from_id_full_page_keeps_symbol_queued():
    """A full-size page (== MY_TRADES_PAGE_LIMIT) means there may be more:
    the symbol must stay at the front of the queue with next_cursor set,
    not be popped."""
    respx.get(f"{m.BASE_URL}{m.EP_EXCHANGE_INFO}").mock(
        return_value=httpx.Response(200, json=_load("exchangeinfo.json"))
    )
    full_page = [
        dict(_load("mytrades_btcusd.json")[0], id=i) for i in range(m.MY_TRADES_PAGE_LIMIT)
    ]
    respx.get(f"{m.BASE_URL}{m.EP_MY_TRADES}").mock(
        return_value=httpx.Response(200, json=full_page)
    )

    connector = _connector()
    prior_state = {
        "symbols": ["BTCUSD"],
        "from_ids": {"BTCUSD": 0},
        "queue": ["BTCUSD"],
        "window_start_ms": None,
    }
    page = connector.fetch_asset_fills_page(cursor=json.dumps(prior_state))

    assert len(page.items) == m.MY_TRADES_PAGE_LIMIT
    assert page.resume_cursor is None
    assert page.next_cursor is not None
    state = json.loads(page.next_cursor)
    assert state["queue"] == ["BTCUSD"]  # still queued: page was full


# ---- market data ---------------------------------------------------------


@respx.mock
def test_market_data_fetch_instruments_from_exchange_info():
    respx.get(f"{m.BASE_URL}{m.EP_EXCHANGE_INFO}").mock(
        return_value=httpx.Response(200, json=_load("exchangeinfo.json"))
    )
    md = m.BinanceUSMarketData(base_url=m.BASE_URL)
    instruments = {i.symbol: i for i in md.fetch_instruments()}
    btc = instruments["BTCUSD"]
    assert btc.base_asset == "BTC"
    assert btc.quote_asset == "USD"
    assert btc.qty_decimals == 8
    # stepSize "0.00001000" @ 8dp -> 1000 atoms.
    assert btc.qty_step_atoms == 1000
    assert btc.min_qty_atoms == 1000
    assert btc.min_notional == "1.00000000"


@respx.mock
def test_market_data_fetch_daily_closes_from_klines():
    respx.get(f"{m.BASE_URL}{m.EP_KLINES}").mock(
        return_value=httpx.Response(200, json=_load("klines_btcusd_1d.json"))
    )
    md = m.BinanceUSMarketData(base_url=m.BASE_URL)
    closes = md.fetch_daily_closes(
        "BTCUSD", start=datetime(2026, 9, 21).date(), end=datetime(2026, 9, 23).date()
    )
    assert len(closes) == 2
    assert closes[0].symbol == "BTCUSD"
    assert closes[0].close == "86179.95000000"  # close field (index 4) of bar 0


@respx.mock
def test_market_data_fetch_quote():
    respx.get(f"{m.BASE_URL}{m.EP_BOOK_TICKER}").mock(
        return_value=httpx.Response(200, json=_load("bookticker_btcusd.json"))
    )
    md = m.BinanceUSMarketData(base_url=m.BASE_URL)
    quote = md.fetch_quote("BTCUSD")
    assert quote.bid == "84313.24000000"
    assert quote.ask == "84313.83000000"


@respx.mock
def test_market_data_fetch_quote_returns_none_on_400():
    respx.get(f"{m.BASE_URL}{m.EP_BOOK_TICKER}").mock(return_value=httpx.Response(400))
    md = m.BinanceUSMarketData(base_url=m.BASE_URL)
    assert md.fetch_quote("NOSUCHSYMBOL") is None
