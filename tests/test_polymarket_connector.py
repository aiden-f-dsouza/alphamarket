"""Polymarket connector (Data API v2) against public payloads recorded
2026-10-09 (tests/fixtures/polymarket/*.json; market/profile reads still use
the 2026-09-06 polymarket_*.json), never live calls -- invariant #6."""

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
TRADES = "polymarket/activity_trades_20261009.json"
REDEEMS = "polymarket/activity_redeems_20261009.json"
POSITIONS = "polymarket/positions_20261009.json"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _connector() -> PolymarketConnector:
    return PolymarketConnector(ADDRESS, DATA, CLOB, GAMMA)


def _rows(name: str) -> list[dict]:
    return _fixture(name)["data"]


def _envelope(rows, next_cursor=None) -> dict:
    """The v2 `{data, pagination}` wrapper around `rows`."""
    return {
        "data": rows,
        "pagination": {
            "limit": 1000,
            "offset": 0,
            "has_more": next_cursor is not None,
            "next_cursor": next_cursor,
        },
    }


def _mock_activity(trades=None, redeems=None):
    """`trades` / `redeems` are row lists; None replays the recorded v2 page."""
    trades_body = _fixture(TRADES) if trades is None else _envelope(trades)
    redeems_body = _fixture(REDEEMS) if redeems is None else _envelope(redeems)
    return (
        respx.get(f"{DATA}/v2/activity", params__contains={"type": "TRADE"}).mock(
            return_value=httpx.Response(200, json=trades_body)
        ),
        respx.get(f"{DATA}/v2/activity", params__contains={"type": "REDEEM"}).mock(
            return_value=httpx.Response(200, json=redeems_body)
        ),
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
    row = _rows(TRADES)[0]
    _mock_activity(trades=[row, dict(row)])
    fills = _connector().fetch_fills()
    ids = sorted(f.venue_fill_id for f in fills)
    assert ids[0].endswith("#0") and ids[1].endswith("#1")
    assert ids[0][:-2] == ids[1][:-2]


@respx.mock
def test_zero_fee_rounding_never_produces_a_negative_fee():
    row = dict(_rows(TRADES)[0])
    row["usdc_size"] = 9.999998  # net one micro below the rounded gross
    _mock_activity(trades=[row])
    (f,) = _connector().fetch_fills()
    assert f.fee_micros == 0
    assert f.notional_micros == 9_999_998
    assert fill_cash_flow(f) == -9_999_998


@respx.mock
def test_sell_fee_is_gross_minus_net():
    row = dict(_rows(TRADES)[2])
    row.update(side="SELL", size=19.6, price=0.81, usdc_size=15.72518)
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
    route = respx.get(f"{DATA}/v2/positions").mock(
        return_value=httpx.Response(200, json=_fixture(POSITIONS))
    )
    positions = _connector().fetch_positions()
    assert len(positions) == 2
    # Both outcomes of one market: the first row holds outcome_index 1 (NO).
    assert all(p.ticker.startswith("0xad08268a") for p in positions)
    assert positions[0].position_micros == -21_199_700  # NO, so negative
    assert positions[0].market_exposure_micros == 12_599_900  # entry_cost_usdc
    assert positions[1].position_micros == 23_814_000  # YES, so positive
    assert positions[1].market_exposure_micros == 8_599_900
    params = route.calls[0].request.url.params
    assert params["user"] == ADDRESS and params["status"] == "OPEN"


@respx.mock
def test_v2_request_shape_and_since_window():
    trades_route, _ = _mock_activity()
    _connector().fetch_fills()
    params = trades_route.calls[0].request.url.params
    assert params["user"] == ADDRESS and params["type"] == "TRADE"
    assert params["sort_by"] == "TIMESTAMP" and params["sort_direction"] == "ASC"
    assert params["limit"] == "1000"
    assert params["start"] == "1"  # an omitted start is floored to 3 years on v2
    assert "offset" not in params and "cursor" not in params
    since = datetime(2026, 9, 5, 15, 15, 45, tzinfo=UTC)
    _connector().fetch_fills(since)
    assert trades_route.calls[1].request.url.params["start"] == str(int(since.timestamp()))


@respx.mock
def test_v1_fill_and_settlement_ids_are_preserved():
    """Fills are append-only and unique on venue_fill_id, so the v2 mapping
    must reproduce the ids history ingested from v1 already carries
    (transactionHash:asset:side:price:size#n, the same values under the v2
    names). Hand-spelled from the recorded rows, not computed by the code."""
    _mock_activity()
    c = _connector()
    fills = c.fetch_fills()
    assert fills[0].venue_fill_id == (
        "0xcf6d1b3123e77c1f83fc8ac07884a02ac1771a767d82dd41c0d7488ec7814707:"
        "93415506741081748396200975939257521410698376620584355147171598503949152127353:"
        "buy:469606:21294467#0"
    )
    settlements = c.fetch_settlements()
    assert settlements[0].venue_settlement_id.endswith(
        ":74938643521125205475990578848537837441151693889010555507991371847249758717036"
    )
    assert settlements[0].venue_settlement_id.startswith(
        "0xd68a6cd76337595ba7205e3043b7ae410d2e9a931b6e46a3e23c3b9e0e65e1dc:"
    )


@respx.mock
def test_activity_follows_the_cursor_until_it_is_null():
    rows = _rows(TRADES)
    route = respx.get(f"{DATA}/v2/activity", params__contains={"type": "TRADE"})
    route.side_effect = [
        httpx.Response(200, json=_envelope(rows[:2], next_cursor="c1")),
        httpx.Response(200, json=_envelope(rows[2:3], next_cursor="c2")),
        httpx.Response(200, json=_envelope(rows[3:])),
    ]
    fills = _connector().fetch_fills()
    assert len(fills) == 4 and len({f.venue_fill_id for f in fills}) == 4
    assert [c.request.url.params.get("cursor") for c in route.calls] == [None, "c1", "c2"]
    # Every page keeps the same anchor and direction (a cursor binds the sort).
    assert all(c.request.url.params["sort_direction"] == "ASC" for c in route.calls)


@respx.mock
def test_positions_follow_the_cursor_and_keep_the_user_anchor():
    rows = _rows(POSITIONS)
    route = respx.get(f"{DATA}/v2/positions")
    route.side_effect = [
        httpx.Response(200, json=_envelope(rows[:1], next_cursor="p1")),
        httpx.Response(200, json=_envelope(rows[1:])),
    ]
    assert len(_connector().fetch_positions()) == 2
    second = route.calls[1].request.url.params
    assert second["cursor"] == "p1" and second["user"] == ADDRESS


@respx.mock
def test_empty_or_null_data_is_no_rows_and_a_stuck_cursor_is_an_error():
    respx.get(f"{DATA}/v2/activity").mock(
        return_value=httpx.Response(
            200, json={"data": None, "pagination": {"has_more": False, "next_cursor": None}}
        )
    )
    assert _connector().fetch_fills() == []
    respx.get(f"{DATA}/v2/activity").mock(
        return_value=httpx.Response(200, json=_envelope(_rows(TRADES)[:1], next_cursor="same"))
    )
    with pytest.raises(ValueError, match="cursor did not advance"):
        _connector().fetch_fills()
    respx.get(f"{DATA}/v2/activity").mock(return_value=httpx.Response(200, json=[]))
    with pytest.raises(ValueError, match="unexpected activity payload"):
        _connector().fetch_fills()


@respx.mock
def test_outcome_index_missing_falls_back_to_market_tokens():
    row = dict(_rows(TRADES)[0])
    row["outcome_index"] = 999
    row["condition_id"] = "0xe8082eb2691a5d9288440f624241c0dcb9d53730d6b944413207797052efd808"
    market = _fixture("polymarket_market.json")
    row["token_id"] = market["tokens"][1]["token_id"]  # the NO token
    _mock_activity(trades=[row])
    route = respx.get(f"{CLOB}/markets/{row['condition_id']}").mock(
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
