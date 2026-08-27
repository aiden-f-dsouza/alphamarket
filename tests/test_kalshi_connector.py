from datetime import UTC, datetime

import httpx
import respx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from alphamarket.connectors.base import Action, Side
from alphamarket.connectors.kalshi import KalshiConnector, sign_request

BASE = "https://demo-api.kalshi.co/trade-api/v2"


def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _connector(key=None) -> KalshiConnector:
    return KalshiConnector(base_url=BASE, key_id="test-key-id", private_key=key or _keypair())


def test_signature_verifies_and_uses_correct_message():
    key = _keypair()
    ts = 1756200000000
    sig_b64 = sign_request(key, ts, "get", "/trade-api/v2/portfolio/balance")

    import base64

    key.public_key().verify(
        base64.b64decode(sig_b64),
        f"{ts}GET/trade-api/v2/portfolio/balance".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )  # raises InvalidSignature on mismatch


@respx.mock
def test_fetch_balance_sends_auth_headers():
    route = respx.get(f"{BASE}/portfolio/balance").mock(
        return_value=httpx.Response(200, json={"balance": 123456})
    )
    balance = _connector().fetch_balance()
    assert balance.balance_cents == 123456

    headers = route.calls.last.request.headers
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers


@respx.mock
def test_fetch_fills_paginates_and_normalizes():
    page1 = {
        "fills": [
            {
                "trade_id": "t2",
                "ticker": "KXBTC-26AUG",
                "side": "yes",
                "action": "buy",
                "count": 10,
                "yes_price": 62,
                "no_price": 38,
                "created_time": "2026-08-20T15:00:00Z",
            }
        ],
        "cursor": "next-page",
    }
    page2 = {
        "fills": [
            {
                "trade_id": "t1",
                "ticker": "KXBTC-26AUG",
                "side": "no",
                "action": "sell",
                "count": 5,
                "yes_price": 60,
                "no_price": 40,
                "fee": 3,
                "created_time": "2026-08-19T10:00:00Z",
            }
        ],
        "cursor": None,
    }
    respx.get(f"{BASE}/portfolio/fills").mock(
        side_effect=[httpx.Response(200, json=page1), httpx.Response(200, json=page2)]
    )

    fills = _connector().fetch_fills()
    assert len(fills) == 2
    # sorted oldest first regardless of API order
    assert [f.venue_fill_id for f in fills] == ["t1", "t2"]

    f1, f2 = fills
    assert f1.side is Side.NO and f1.price_cents == 40 and f1.fee_cents == 3
    assert f2.side is Side.YES and f2.action is Action.BUY and f2.price_cents == 62
    assert f1.filled_at == datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
