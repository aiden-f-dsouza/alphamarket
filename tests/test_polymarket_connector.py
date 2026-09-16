"""Polymarket connector against public payloads captured 2026-09-06
(tests/fixtures/polymarket_*.json), never live calls -- invariant #6."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.connectors.base import Action, BalanceUnavailable, Side
from alphamarket.connectors.polymarket import (
    PolymarketConnector,
    PolymarketMarketData,
    is_address,
    micros,
)
from alphamarket.connectors.registry import build_connector, build_market_data
from alphamarket.ledger import fill_cash_flow

DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
ADDRESS = "0x124628e686db22da39b6ee43ca786c8a074bd6f2"
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _connector() -> PolymarketConnector:
    return PolymarketConnector(ADDRESS, DATA, CLOB, GAMMA)


def _mock_activity(trades=None, redeems=None):
    respx.get(f"{DATA}/activity", params__contains={"type": "TRADE"}).mock(
        return_value=httpx.Response(200, json=_fixture("polymarket_activity_trades.json"))
        if trades is None
        else httpx.Response(200, json=trades)
    )
    respx.get(f"{DATA}/activity", params__contains={"type": "REDEEM"}).mock(
        return_value=httpx.Response(200, json=_fixture("polymarket_activity_redeems.json"))
        if redeems is None
        else httpx.Response(200, json=redeems)
    )


def test_address_validation_and_micros_rounding():
    assert is_address(ADDRESS)
    assert is_address(ADDRESS.upper().replace("0X", "0x"))
    assert not is_address("0x1234")
    assert not is_address("0xzz4628e686db22da39b6ee43ca786c8a074bd6f2")
    with pytest.raises(ValueError):
        PolymarketConnector("not-an-address", DATA, CLOB, GAMMA)
    # 10-dp average prices round half-even to the micro; 6-dp cash is exact.
    assert micros(0.4696055083) == 469_606
    assert micros(0.219999985) == 220_000
    assert micros(10.265189) == 10_265_189
    assert micros(19.6) == 19_600_000


@respx.mock
def test_fetch_fills_derives_fee_from_net_cash():
    _mock_activity()
    fills = _connector().fetch_fills()
    assert len(fills) == 4
    f = fills[0]
    assert f.ticker == "0x1030ca305c4ac8e0d91ddbc21da7681cd28851260ed25ae69294c7907e64ad64"
    assert f.side is Side.YES and f.action is Action.BUY
    assert f.count_micros == 21_294_467
    assert f.price_micros == 469_606
    # size x price rounds to 9.999999 (a $10 order); the venue says 10.265189
    # left the wallet, so the fee is the difference and cash reproduces the
    # venue exactly. 5% x 21.294467 x 0.4696 x 0.5304 = 0.2652: the published
    # formula, to within the price rounding.
    assert f.notional_micros == 9_999_999
    assert f.fee_micros == 265_190
    assert fill_cash_flow(f) == -10_265_189
    assert f.filled_at == datetime(2026, 9, 5, 15, 15, 45, tzinfo=UTC)
    assert f.venue_fill_id.endswith("#0")
    # Oldest first, and every id unique.
    assert [x.filled_at for x in fills] == sorted(x.filled_at for x in fills)
    assert len({x.venue_fill_id for x in fills}) == 4


@respx.mock
def test_identical_rows_in_one_transaction_get_ordinals():
    row = _fixture("polymarket_activity_trades.json")[0]
    _mock_activity(trades=[row, dict(row)])
    fills = _connector().fetch_fills()
    ids = sorted(f.venue_fill_id for f in fills)
    assert ids[0].endswith("#0") and ids[1].endswith("#1")
    assert ids[0][:-2] == ids[1][:-2]


@respx.mock
def test_zero_fee_rounding_never_produces_a_negative_fee():
    row = dict(_fixture("polymarket_activity_trades.json")[0])
    row["usdcSize"] = 9.999998  # net one micro below the rounded gross
    _mock_activity(trades=[row])
    (f,) = _connector().fetch_fills()
    assert f.fee_micros == 0
    assert f.notional_micros == 9_999_998
    assert fill_cash_flow(f) == -9_999_998


@respx.mock
def test_sell_fee_is_gross_minus_net():
    row = dict(_fixture("polymarket_activity_trades.json")[2])
    row.update(side="SELL", size=19.6, price=0.81, usdcSize=15.72518)
    _mock_activity(trades=[row])
    (f,) = _connector().fetch_fills()
    assert f.action is Action.SELL
    assert f.notional_micros == 15_876_000  # 19.6 x 0.81
    assert f.fee_micros == 150_820
    assert fill_cash_flow(f) == 15_725_180


@respx.mock
def test_redeems_become_settlements():
    _mock_activity()
    settlements = _connector().fetch_settlements()
    assert len(settlements) == 3
    losing = settlements[0]
    # 19.607842 YES tokens redeemed for nothing: the market resolved NO.
    assert losing.market_result == "no"
    assert losing.yes_count_micros == 19_607_842 and losing.no_count_micros == 0
    assert losing.revenue_micros == 0
    assert losing.settled_at == datetime(2026, 9, 6, 16, 27, 34, tzinfo=UTC)
    winning = settlements[2]
    assert winning.market_result == "yes"
    assert winning.yes_count_micros == 170_000 and winning.revenue_micros == 170_000
    assert winning.venue_settlement_id.startswith("0x0749368b")
    # Same transaction, different asset: distinct ids.
    assert settlements[1].venue_settlement_id != winning.venue_settlement_id


@respx.mock
def test_positions_carry_cost_basis_and_sign():
    respx.get(f"{DATA}/positions").mock(
        return_value=httpx.Response(200, json=_fixture("polymarket_positions.json"))
    )
    positions = _connector().fetch_positions()
    assert len(positions) == 2
    assert positions[0].ticker.startswith("0x941b674f")
    assert positions[0].position_micros == 30_317_200  # YES, so positive
    assert positions[0].market_exposure_micros == 9_999_900  # initialValue


@respx.mock
def test_outcome_index_missing_falls_back_to_market_tokens():
    row = dict(_fixture("polymarket_activity_trades.json")[0])
    row["outcomeIndex"] = 999
    row["conditionId"] = "0xe8082eb2691a5d9288440f624241c0dcb9d53730d6b944413207797052efd808"
    market = _fixture("polymarket_market.json")
    row["asset"] = market["tokens"][1]["token_id"]  # the NO token
    _mock_activity(trades=[row])
    route = respx.get(f"{CLOB}/markets/{row['conditionId']}").mock(
        return_value=httpx.Response(200, json=market)
    )
    (f,) = _connector().fetch_fills()
    assert f.side is Side.NO
    assert route.called


@respx.mock
def test_no_balance_no_write_scope_and_profile_claim():
    c = _connector()
    with pytest.raises(BalanceUnavailable):
        c.fetch_balance()
    assert c.check_write_scope() is False
    respx.get(f"{GAMMA}/public-profile").mock(
        return_value=httpx.Response(200, json=_fixture("polymarket_profile.json"))
    )
    assert c.check_connection().balance_micros == 0
    assert c.verify_claim("koine-deadbeef") is False
    assert c.verify_claim("adversely selected") is True
    respx.get(f"{GAMMA}/public-profile").mock(return_value=httpx.Response(500, json={}))
    assert c.verify_claim("koine-deadbeef") is None
    respx.get(f"{GAMMA}/public-profile").mock(
        return_value=httpx.Response(404, json={"error": "profile not found"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        c.check_connection()


@respx.mock
def test_market_data_active_and_closed():
    m = _fixture("polymarket_market.json")
    cid = m["condition_id"]
    yes_token = m["tokens"][0]["token_id"]
    respx.get(f"{CLOB}/markets/{cid}").mock(return_value=httpx.Response(200, json=m))
    respx.get(f"{CLOB}/price", params__contains={"side": "buy"}).mock(
        return_value=httpx.Response(200, json={"price": "0.77"})
    )
    respx.get(f"{CLOB}/price", params__contains={"side": "sell"}).mock(
        return_value=httpx.Response(200, json={"price": "0.83"})
    )
    mid = respx.get(f"{CLOB}/midpoint").mock(return_value=httpx.Response(200, json={"mid": "0.8"}))
    market = PolymarketMarketData(CLOB).fetch_market(cid)
    assert market.ticker == cid and market.status == "active" and market.result == ""
    assert (market.yes_bid_micros, market.yes_ask_micros) == (770_000, 830_000)
    assert market.last_price_micros == 800_000
    assert market.yes_mark_micros == 800_000  # midpoint of bid/ask
    assert market.close_time == datetime(2026, 9, 7, tzinfo=UTC)
    assert mid.calls[0].request.url.params["token_id"] == yes_token

    closed = _fixture("polymarket_market_closed.json")
    respx.get(f"{CLOB}/markets/{closed['condition_id']}").mock(
        return_value=httpx.Response(200, json=closed)
    )
    resolved = PolymarketMarketData(CLOB).fetch_market(closed["condition_id"])
    assert resolved.status == "closed" and resolved.result == "yes"
    assert resolved.settled and resolved.yes_mark_micros == 1_000_000

    respx.get(f"{CLOB}/markets/0xnope").mock(
        return_value=httpx.Response(404, json={"error": "market not found"})
    )
    assert PolymarketMarketData(CLOB).fetch_market("0xnope") is None


def test_registry_builds_polymarket_without_a_secret():
    c = build_connector("polymarket", "prod", ADDRESS)
    assert isinstance(c, PolymarketConnector) and c.address == ADDRESS
    assert isinstance(build_market_data("polymarket", "prod"), PolymarketMarketData)
    with pytest.raises(ValueError):
        build_connector("polymarket", "demo", ADDRESS)  # no demo environment
    with pytest.raises(ValueError):
        build_connector("kalshi", "demo", "key-id", None)  # Kalshi needs the PEM
