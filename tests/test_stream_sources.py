"""P7 stream sources: frame parsing per venue from fixture frames
(tests/fixtures/streams/, provenance in its README), the subscribe frames,
the Kraken websocket token, and the registry wiring. No sockets, no live
calls (invariant 6)."""

import json
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alphamarket.connectors import hyperliquid as hl
from alphamarket.connectors.base import StreamMode, StreamProtocolError, StreamUnavailable
from alphamarket.connectors.coinbase import CoinbaseConnector, CoinbaseUserStream
from alphamarket.connectors.kraken import KrakenConnector, KrakenExecutionStream
from alphamarket.connectors.registry import build_connector, get_profile, streaming_venues

STREAMS = Path(__file__).parent / "fixtures" / "streams"
HL_FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"
ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"
KRAKEN_KEY = "test-api-key"
KRAKEN_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
)
WS_TOKEN = "WW91ciBhdXRoZW50aWNhdGlvbiB0b2tlbiBnb2VzIGhlcmUu"  # Kraken docs example token


def frames(name: str) -> dict:
    return json.loads((STREAMS / name).read_text())


def frame(name: str, key: str) -> str:
    return json.dumps(frames(name)[key])


def hl_fx(name: str):
    return json.loads((HL_FIXTURES / f"{name}_2026-09-26.json").read_text())


def hl_client() -> httpx.Client:
    """The info endpoint, answering metadata and fills from the captures."""
    answers = {
        "meta": hl_fx("meta"),
        "meta:xyz": hl_fx("meta_xyz"),
        "spotMeta": hl_fx("spotMeta"),
        "userFillsByTime": hl_fx("userFillsByTime"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = body["type"] + (f":{body['dex']}" if body.get("dex") else "")
        return httpx.Response(200, json=answers[key])

    return httpx.Client(transport=httpx.MockTransport(handler))


def hl_connector() -> hl.HyperliquidConnector:
    return hl.HyperliquidConnector(ADDRESS, client=hl_client())


# ---- registry -----------------------------------------------------------------------


def test_streaming_venues_and_modes_come_from_the_profiles():
    venues = streaming_venues()
    assert set(venues) == {"kraken", "hyperliquid", "coinbase"}
    assert get_profile("hyperliquid").stream_mode is StreamMode.FILLS
    assert get_profile("kraken").stream_mode is StreamMode.NUDGE
    assert get_profile("coinbase").stream_mode is StreamMode.NUDGE
    # Docs "Rate limits and user limits": 10 unique users per IP on the websocket.
    assert get_profile("hyperliquid").stream_users_per_ip == 10
    assert get_profile("kraken").stream_users_per_ip is None
    for venue in ("kalshi", "polymarket", "alpaca", "binanceus", "ibkr", "snaptrade"):
        assert get_profile(venue).stream_mode is None


def test_alpaca_is_a_documented_stub_and_binary_venues_have_no_stream():
    alpaca = build_connector("alpaca", "paper", "acct-1", json.dumps({"access_token": "t"}))
    assert alpaca.stream_source() is None
    poly = build_connector("polymarket", "prod", "0x" + "ab" * 20)
    assert poly.stream_source() is None


# ---- Hyperliquid (FILLS) -------------------------------------------------------------


def test_hyperliquid_ws_url_follows_the_info_endpoint():
    assert hl.ws_url_for(hl.INFO_URLS["prod"]) == "wss://api.hyperliquid.xyz/ws"
    assert hl.ws_url_for(hl.INFO_URLS["testnet"]) == "wss://api.hyperliquid-testnet.xyz/ws"
    assert hl.ws_url_for("https://node.example/info") == "wss://node.example/ws"
    source = hl_connector().stream_source()
    assert source.url() == "wss://api.hyperliquid.xyz/ws"
    assert json.loads(source.subscribe_frames()[0]) == {
        "method": "subscribe",
        "subscription": {"type": "userFills", "user": ADDRESS},
    }
    assert json.loads(source.ping_frame()) == {"method": "ping"}


def test_hyperliquid_stream_fills_equal_the_pollers_fills():
    """The point of FILLS mode: a streamed fill is the poller's fill (same
    id, instrument, legs), so ingest stores it once."""
    connector = hl_connector()
    source = connector.stream_source()
    (event,) = source.parse(frame("hyperliquid_ws.json", "snapshot"))
    assert not event.nudge
    # The snapshot lists LDO (row 1) before XPL (row 0); fills come oldest first.
    assert [f.venue_fill_id for f in event.fills] == ["229662172402780", "33406773875521"]
    polled = {f.venue_fill_id: f for f in hl_connector().fetch_asset_fills_page().items}
    for fill in event.fills:
        assert fill.model_dump() == polled[fill.venue_fill_id].model_dump()
    # XPL close long, hand-checked in test_hyperliquid_connector: sz 73874 at 0 dp,
    # closedPnl -286.852742 and fee 1.220361 USDC at 8 dp.
    xpl = event.fills[0]
    legs = {(leg.kind.value, leg.asset): leg.delta_atoms for leg in xpl.legs}
    assert legs == {
        ("fill_base", "XPL-PERP"): -73_874,
        ("realized_pnl", "USDC"): -28_685_274_200,
        ("fee", "USDC"): -122_036_100,
    }
    assert event.ref == "33406773875521"


def test_hyperliquid_update_acks_pongs_and_other_users():
    source = hl_connector().stream_source()
    (event,) = source.parse(frame("hyperliquid_ws.json", "update"))
    assert [f.venue_fill_id for f in event.fills] == ["537099385287280"]
    assert source.parse(frame("hyperliquid_ws.json", "subscription_response")) == []
    assert source.parse(frame("hyperliquid_ws.json", "pong")) == []
    assert source.parse(frame("hyperliquid_ws.json", "other_user")) == []
    assert source.parse(json.dumps({"channel": "userFills", "data": {"fills": []}})) == []


def test_hyperliquid_error_frames():
    source = hl_connector().stream_source()
    with pytest.raises(StreamProtocolError):
        source.parse(frame("hyperliquid_ws.json", "error"))
    with pytest.raises(StreamUnavailable):  # the per-IP user cap: back off for long
        source.parse(frame("hyperliquid_ws.json", "user_cap_error"))
    with pytest.raises(StreamProtocolError):
        source.parse("not json")


# ---- Kraken (NUDGE) -----------------------------------------------------------------


def kraken_client(answer: dict, calls: list | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request.url.path)
        return httpx.Response(200, json=answer)

    return httpx.Client(transport=httpx.MockTransport(handler))


def kraken(answer: dict, calls: list | None = None) -> KrakenConnector:
    return KrakenConnector(KRAKEN_KEY, KRAKEN_SECRET, client=kraken_client(answer, calls))


def test_kraken_token_is_fetched_and_only_lands_in_the_subscribe_frame():
    calls: list[str] = []
    conn = kraken({"error": [], "result": {"token": WS_TOKEN, "expires": 900}}, calls)
    source = conn.stream_source()
    assert isinstance(source, KrakenExecutionStream)
    with pytest.raises(StreamProtocolError):
        source.subscribe_frames()  # no token before prepare()
    source.prepare()
    assert calls == ["/0/private/GetWebSocketsToken"]
    (sub,) = source.subscribe_frames()
    assert json.loads(sub) == {
        "method": "subscribe",
        "params": {
            "channel": "executions",
            "token": WS_TOKEN,
            "snap_orders": False,
            "snap_trades": False,
        },
        "req_id": 1,
    }
    assert source.url() == "wss://ws-auth.kraken.com/v2"
    assert WS_TOKEN not in repr(source)


def test_kraken_key_without_websocket_permission_is_unavailable_not_an_error():
    conn = kraken({"error": ["EGeneral:Permission denied"]})
    with pytest.raises(StreamUnavailable):
        conn.stream_source().prepare()


def test_kraken_trade_executions_nudge_and_everything_else_is_quiet():
    source = KrakenExecutionStream(kraken({"error": []}))
    (event,) = source.parse(frame("kraken_ws_v2.json", "trade"))
    assert event.nudge and not event.fills
    assert event.ref == "TJE5WD-R2SAT-6X3ACL"
    for quiet in ("subscribe_ack", "status", "heartbeat", "pong", "pending_new"):
        assert source.parse(frame("kraken_ws_v2.json", quiet)) == [], quiet


def test_kraken_subscribe_refusals():
    source = KrakenExecutionStream(kraken({"error": []}))
    with pytest.raises(StreamUnavailable):
        source.parse(frame("kraken_ws_v2.json", "subscribe_denied"))
    with pytest.raises(StreamProtocolError):  # reconnect: prepare() fetches a new token
        source.parse(frame("kraken_ws_v2.json", "subscribe_bad_token"))


# ---- Coinbase (NUDGE) ---------------------------------------------------------------


def coinbase() -> CoinbaseConnector:
    return CoinbaseConnector(
        key_name="organizations/o/apiKeys/k", private_key=Ed25519PrivateKey.generate(), alg="EdDSA"
    )


def test_coinbase_subscribe_frames_carry_a_ws_jwt_without_uri():
    source = coinbase().stream_source()
    assert isinstance(source, CoinbaseUserStream)
    subs = [json.loads(f) for f in source.subscribe_frames()]
    assert [s["channel"] for s in subs] == ["heartbeats", "user"]
    for sub in subs:
        claims = pyjwt.decode(sub["jwt"], options={"verify_signature": False})
        assert claims["sub"] == "organizations/o/apiKeys/k"
        assert claims["iss"] == "cdp"
        assert claims["exp"] - claims["nbf"] == 120
        assert "uri" not in claims  # websocket JWTs are not bound to a REST path
    assert "private" not in repr(source).lower()


def test_coinbase_nudges_only_when_an_order_fills_more():
    source = coinbase().stream_source()
    assert source.parse(frame("coinbase_ws_user.json", "subscriptions")) == []
    assert source.parse(frame("coinbase_ws_user.json", "heartbeat")) == []
    assert source.parse(frame("coinbase_ws_user.json", "snapshot")) == []  # baseline only
    (event,) = source.parse(frame("coinbase_ws_user.json", "partial_fill"))
    assert event.nudge and event.ref == "XXX"
    assert source.parse(frame("coinbase_ws_user.json", "status_only")) == []  # no new quantity
    (event,) = source.parse(frame("coinbase_ws_user.json", "filled"))
    assert event.nudge
    assert source._filled == {}  # a finished order is forgotten


def test_coinbase_auth_error_is_unavailable():
    with pytest.raises(StreamUnavailable):
        coinbase().stream_source().parse(frame("coinbase_ws_user.json", "error_auth"))
