"""Connector tests run against payloads captured from a real demo account
(tests/fixtures/), never live calls -- invariant #6."""

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from alphamarket.connectors.base import Action, Side
from alphamarket.connectors.kalshi import KalshiConnector, sign_request
from alphamarket.money import format_micros, from_micros, notional_micros

BASE = "https://demo-api.kalshi.co/trade-api/v2"
FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _connector(key=None) -> KalshiConnector:
    return KalshiConnector(base_url=BASE, key_id="test-key-id", private_key=key or _keypair())


def test_signature_verifies_and_uses_correct_message():
    key = _keypair()
    ts = 1756200000000
    sig_b64 = sign_request(key, ts, "get", "/trade-api/v2/portfolio/balance")

    key.public_key().verify(
        base64.b64decode(sig_b64),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises InvalidSignature on mismatch


@respx.mock
def test_fetch_balance_sends_auth_headers():
    route = respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_balance.json"))
    )
    _connector().fetch_balance()

    headers = route.calls.last.request.headers
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers


@respx.mock
def test_fetch_balance_prefers_dollars_over_truncated_int():
    """The int `balance` field (110189) drops 0.8 cents that balance_dollars
    ("1101.8980") keeps. Reconciliation cannot be built on the truncated one."""
    respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_balance.json"))
    )
    balance = _connector().fetch_balance()

    assert balance.balance_micros == 1_101_898_000
    assert balance.balance_micros != 110_189 * 10_000  # what the int field would give
    assert balance.as_of == datetime.fromtimestamp(1787860432, UTC)


@respx.mock
def test_fetch_fills_normalizes_real_payload():
    respx.get(f"{BASE}/portfolio/fills").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_fills.json"))
    )
    (fill,) = _connector().fetch_fills()

    assert fill.venue_fill_id == "e9efc650-5012-75a0-6383-21440fdfc3aa"
    assert fill.ticker == "KXBTCD-26MAY3004-T73299.99"
    assert fill.side is Side.YES
    assert fill.action is Action.BUY
    assert fill.count_micros == 101_890_000  # "101.89" fractional contracts
    assert fill.price_micros == 980_000  # "0.9800" -> 98 cents
    assert fill.fee_micros == 139_800  # "0.139800" -> 13.98 cents, not 14
    assert fill.filled_at == datetime(2026, 5, 30, 7, 59, 32, 308769, tzinfo=UTC)


@respx.mock
def test_fill_notional_is_exact():
    """101.89 x $0.9800 + $0.139800 fee = $99.992000, the exact account debit."""
    respx.get(f"{BASE}/portfolio/fills").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_fills.json"))
    )
    (fill,) = _connector().fetch_fills()

    debit = notional_micros(fill.price_micros, fill.count_micros) + fill.fee_micros
    assert debit == 99_992_000
    assert from_micros(debit) == Decimal("99.992")
    assert format_micros(debit, 6) == "99.992000"


@respx.mock
def test_no_side_fill_uses_no_price():
    payload = _fixture("kalshi_fills.json")
    payload["fills"][0]["side"] = "no"
    respx.get(f"{BASE}/portfolio/fills").mock(return_value=httpx.Response(200, json=payload))

    (fill,) = _connector().fetch_fills()
    assert fill.side is Side.NO
    assert fill.price_micros == 20_000  # "0.0200"


@respx.mock
def test_fetch_fills_paginates_and_sorts_oldest_first():
    older = _fixture("kalshi_fills.json")
    older["fills"][0] |= {"fill_id": "older", "created_time": "2026-05-29T10:00:00Z"}
    newer = _fixture("kalshi_fills.json")
    newer["fills"][0] |= {"fill_id": "newer"}
    newer["cursor"] = "page-2"

    respx.get(f"{BASE}/portfolio/fills").mock(
        side_effect=[httpx.Response(200, json=newer), httpx.Response(200, json=older)]
    )
    fills = _connector().fetch_fills()
    assert [f.venue_fill_id for f in fills] == ["older", "newer"]


@respx.mock
def test_paginate_stops_on_repeated_cursor():
    """A server that keeps handing back the same cursor must not loop forever."""
    looping = _fixture("kalshi_fills.json")
    looping["cursor"] = "same-cursor"
    respx.get(f"{BASE}/portfolio/fills").mock(return_value=httpx.Response(200, json=looping))
    fills = _connector().fetch_fills()
    assert len(fills) == 2  # first page, then the repeat is detected and we stop


@respx.mock
def test_missing_fee_defaults_to_zero():
    payload = _fixture("kalshi_fills.json")
    del payload["fills"][0]["fee_cost"]
    respx.get(f"{BASE}/portfolio/fills").mock(return_value=httpx.Response(200, json=payload))

    (fill,) = _connector().fetch_fills()
    assert fill.fee_micros == 0


@respx.mock
def test_fetch_settlements_normalizes_real_payload():
    """Captured live 2026-09-01: 0.84 NO contracts lost when the market
    resolved yes. fee field is `fee_cost`; `revenue` is integer cents."""
    respx.get(f"{BASE}/portfolio/settlements").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_settlements.json"))
    )
    (stl,) = _connector().fetch_settlements()

    assert stl.venue_settlement_id == "KXETH15M-26AUG312315-15:2026-09-01T03:15:11.341971Z"
    assert stl.ticker == "KXETH15M-26AUG312315-15"
    assert stl.market_result == "yes"
    assert stl.no_count_micros == 840_000  # "0.84"
    assert stl.yes_count_micros == 0
    assert stl.revenue_micros == 0  # lost: revenue 0 cents
    assert stl.fee_micros == 14_500  # "0.014500"
    assert stl.settled_at == datetime(2026, 9, 1, 3, 15, 11, 341971, tzinfo=UTC)


@respx.mock
def test_settlement_revenue_cents_converted_exactly():
    """A winning 0.84-contract NO position pays 84 integer cents."""
    payload = _fixture("kalshi_settlements.json")
    payload["settlements"][0] |= {"market_result": "no", "revenue": 84}
    respx.get(f"{BASE}/portfolio/settlements").mock(return_value=httpx.Response(200, json=payload))

    (stl,) = _connector().fetch_settlements()
    assert stl.revenue_micros == 840_000  # 84c exactly, no float anywhere


@respx.mock
def test_settlement_prefers_revenue_dollars_if_present():
    """Mirrors balance_dollars: a dollars string wins over the cents int."""
    payload = _fixture("kalshi_settlements.json")
    payload["settlements"][0] |= {"revenue": 84, "revenue_dollars": "0.8412"}
    respx.get(f"{BASE}/portfolio/settlements").mock(return_value=httpx.Response(200, json=payload))

    (stl,) = _connector().fetch_settlements()
    assert stl.revenue_micros == 841_200


@respx.mock
def test_fetch_positions_normalizes_real_payload():
    """Captured live 2026-09-01: position_fp is signed (negative = net NO)."""
    respx.get(f"{BASE}/portfolio/positions").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_positions.json"))
    )
    (position,) = _connector().fetch_positions()

    assert position.ticker == "KXETH15M-26AUG312315-15"
    assert position.position_micros == -840_000  # "-0.84" contracts, net NO
    assert position.market_exposure_micros == 478_800  # "0.478800"


@respx.mock
def test_write_scope_false_for_read_only_key():
    respx.get(f"{BASE}/api_keys").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_api_keys.json"))
    )
    assert _connector().check_write_scope() is False


@respx.mock
def test_write_scope_true_when_key_has_write():
    """Shape captured live 2026-08-31: scopes is ["read", "write"]."""
    payload = _fixture("kalshi_api_keys.json")
    payload["api_keys"][0]["scopes"] = ["read", "write"]
    respx.get(f"{BASE}/api_keys").mock(return_value=httpx.Response(200, json=payload))
    assert _connector().check_write_scope() is True


@respx.mock
def test_write_scope_undetermined_when_key_absent_or_errored():
    payload = _fixture("kalshi_api_keys.json")
    payload["api_keys"][0]["api_key_id"] = "someone-else"
    respx.get(f"{BASE}/api_keys").mock(return_value=httpx.Response(200, json=payload))
    assert _connector().check_write_scope() is None

    respx.get(f"{BASE}/api_keys").mock(return_value=httpx.Response(500, json={}))
    assert _connector().check_write_scope() is None


@respx.mock
def test_retries_on_429_then_succeeds(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr("alphamarket.connectors.kalshi.time.sleep", sleeps.append)
    respx.get(f"{BASE}/portfolio/balance").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(503),
            httpx.Response(200, json=_fixture("kalshi_balance.json")),
        ]
    )
    assert _connector().fetch_balance().balance_micros == 1_101_898_000
    assert 2.0 in sleeps  # honoured Retry-After
    assert any(s == pytest.approx(1.0) for s in sleeps)  # 0.5 * 2**1 backoff after the 503


@respx.mock
def test_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr("alphamarket.connectors.kalshi.time.sleep", lambda s: None)
    route = respx.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        _connector().fetch_balance()
    assert route.call_count == KalshiConnector.MAX_ATTEMPTS


@respx.mock
def test_http_error_propagates():
    respx.get(f"{BASE}/portfolio/balance").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        _connector().fetch_balance()
