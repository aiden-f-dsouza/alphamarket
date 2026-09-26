"""Coinbase connector tests, respx only -- invariant #6.

Private-endpoint fixtures are built from the CDP docs' own example payloads
(no Coinbase account exists yet); see tests/fixtures/coinbase/README.md for
exact provenance and what still needs re-verifying against a real account.
Public market-data fixtures are real, unauthenticated captures.
"""

import base64
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt as pyjwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alphamarket.connectors.base import Action, LegKind, RateLimited
from alphamarket.connectors.coinbase import (
    BASE_URL,
    CoinbaseConnector,
    CoinbaseMarketData,
    build_jwt,
    load_private_key,
)
from alphamarket.connectors.registry import build_connector, build_market_data, get_profile

FIXTURES = Path(__file__).parent / "fixtures" / "coinbase"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _ed25519_secret() -> tuple[str, Ed25519PrivateKey]:
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(seed + pub).decode(), key


def _connector() -> CoinbaseConnector:
    secret, _key = _ed25519_secret()
    alg, private_key = load_private_key(secret)
    return CoinbaseConnector(
        key_name="organizations/org/apiKeys/test-key", private_key=private_key, alg=alg
    )


# ---- key loading + JWT --------------------------------------------------------


def test_load_private_key_ed25519_base64_and_jwt_claims():
    secret, key = _ed25519_secret()
    alg, private_key = load_private_key(secret)
    assert alg == "EdDSA"

    token = build_jwt(
        "organizations/org/apiKeys/test-key", private_key, alg, "GET", "/api/v3/brokerage/accounts"
    )
    decoded = pyjwt.decode(
        token, key.public_key(), algorithms=["EdDSA"], options={"verify_aud": False}
    )
    assert decoded["sub"] == "organizations/org/apiKeys/test-key"
    assert decoded["iss"] == "cdp"
    assert decoded["uri"] == "GET api.coinbase.com/api/v3/brokerage/accounts"
    assert decoded["exp"] - decoded["nbf"] == 120  # 2-minute expiry, per task spec

    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == "organizations/org/apiKeys/test-key"
    assert header["alg"] == "EdDSA"
    assert "nonce" in header


def test_load_private_key_32_byte_seed_also_accepted():
    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    alg, private_key = load_private_key(base64.b64encode(seed).decode())
    assert alg == "EdDSA"
    token = build_jwt("k", private_key, alg, "GET", "/x")
    pyjwt.decode(token, key.public_key(), algorithms=["EdDSA"], options={"verify_aud": False})


def test_load_private_key_legacy_pem_is_es256():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    alg, private_key = load_private_key(pem)
    assert alg == "ES256"
    token = build_jwt("k", private_key, alg, "POST", "/api/v3/brokerage/orders/historical/fills")
    decoded = pyjwt.decode(
        token, key.public_key(), algorithms=["ES256"], options={"verify_aud": False}
    )
    assert decoded["uri"] == "POST api.coinbase.com/api/v3/brokerage/orders/historical/fills"


def test_load_private_key_rejects_garbage():
    with pytest.raises(ValueError):
        load_private_key("not base64 and not a PEM !!!")


# ---- read-only proof -----------------------------------------------------------


@respx.mock
def test_prove_read_only_true_and_sets_account_ref():
    respx.get(f"{BASE_URL}/api/v3/brokerage/key_permissions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_key_permissions_readonly.json"))
    )
    conn = _connector()
    evidence = conn.prove_read_only()
    assert evidence.read_only is True
    assert evidence.details["can_trade"] is False
    assert evidence.details["can_transfer"] is False
    assert conn.account_ref == "11111111-1111-1111-1111-111111111111"
    assert conn.check_write_scope() is False


@respx.mock
def test_prove_read_only_false_for_a_writable_key():
    respx.get(f"{BASE_URL}/api/v3/brokerage/key_permissions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_key_permissions_writable.json"))
    )
    conn = _connector()
    evidence = conn.prove_read_only()
    assert evidence.read_only is False
    assert conn.check_write_scope() is True


@respx.mock
def test_prove_read_only_undetermined_on_http_error():
    respx.get(f"{BASE_URL}/api/v3/brokerage/key_permissions").mock(return_value=httpx.Response(500))
    conn = _connector()
    evidence = conn.prove_read_only()
    assert evidence.read_only is None
    assert conn.check_write_scope() is None


@respx.mock
def test_key_permissions_sends_bearer_jwt():
    route = respx.get(f"{BASE_URL}/api/v3/brokerage/key_permissions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_key_permissions_readonly.json"))
    )
    _connector().prove_read_only()
    auth = route.calls.last.request.headers["Authorization"]
    assert auth.startswith("Bearer ")
    header = pyjwt.get_unverified_header(auth.removeprefix("Bearer "))
    assert header["kid"] == "organizations/org/apiKeys/test-key"


# ---- fills: mapping, atoms, pagination, cursors --------------------------------


@respx.mock
def test_fetch_asset_fills_paginates_and_computes_legs_exactly():
    route = respx.get(f"{BASE_URL}/api/v3/brokerage/orders/historical/fills").mock(
        side_effect=[
            httpx.Response(200, json=_fixture("coinbase_fills_page1.json")),
            httpx.Response(200, json=_fixture("coinbase_fills_page2.json")),
        ]
    )

    fills = _connector().fetch_asset_fills()
    assert route.call_count == 2
    assert route.calls[1].request.url.params["cursor"] == "abc123"
    assert [f.venue_fill_id for f in fills] == [
        "22222-2222222-22222222",
        "22222-2222222-22222223",
        "22222-2222222-22222224",
    ]
    # entry_id is the id used, never trade_id (docs: trade_id is not unique).
    assert all(f.order_id and f.venue_fill_id != f.order_id for f in fills)

    btc_buy = fills[0]
    # 0.01 BTC @ $50000, fee $0.30: base +1,000,000 atoms (8dp), quote
    # -500,000,000 atoms (6dp) = -$500.00, fee -300,000 atoms = -$0.30.
    assert btc_buy.action is Action.BUY
    assert btc_buy.qty_atoms == 1_000_000
    legs = {(leg.kind, leg.asset): leg.delta_atoms for leg in btc_buy.legs}
    assert legs[(LegKind.FILL_BASE, "BTC")] == 1_000_000
    assert legs[(LegKind.FILL_QUOTE, "USD")] == -500_000_000
    assert legs[(LegKind.FEE, "USD")] == -300_000

    eth_sell = fills[1]
    # 2.5 ETH @ $3000.555 = $7501.3875 exactly, fee $1.50.
    assert eth_sell.action is Action.SELL
    legs = {(leg.kind, leg.asset): leg.delta_atoms for leg in eth_sell.legs}
    assert legs[(LegKind.FILL_BASE, "ETH")] == -250_000_000
    assert legs[(LegKind.FILL_QUOTE, "USD")] == 7_501_387_500
    assert legs[(LegKind.FEE, "USD")] == -1_500_000

    quote_fill = fills[2]
    # size_in_quote=True: size "100.00" IS the quote amount spent; base
    # estimated as 100/50000 = 0.002 BTC exactly (no rounding needed).
    assert quote_fill.qty_atoms == 200_000
    legs = {(leg.kind, leg.asset): leg.delta_atoms for leg in quote_fill.legs}
    assert legs[(LegKind.FILL_BASE, "BTC")] == 200_000
    assert legs[(LegKind.FILL_QUOTE, "USD")] == -100_000_000
    assert legs[(LegKind.FEE, "USD")] == -500_000


@respx.mock
def test_fills_page_is_sorted_oldest_first_even_if_venue_returns_newest_first():
    reversed_page = {
        "fills": list(reversed(_fixture("coinbase_fills_page1.json")["fills"])),
        "cursor": "",
    }
    respx.get(f"{BASE_URL}/api/v3/brokerage/orders/historical/fills").mock(
        return_value=httpx.Response(200, json=reversed_page)
    )
    page = _connector().fetch_asset_fills_page()
    assert [f.filled_at for f in page.items] == sorted(f.filled_at for f in page.items)


@respx.mock
def test_final_page_sets_resume_cursor_from_last_fill():
    respx.get(f"{BASE_URL}/api/v3/brokerage/orders/historical/fills").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_fills_page2.json"))
    )
    page = _connector().fetch_asset_fills_page()
    assert page.next_cursor is None
    assert page.resume_cursor is not None
    state = json.loads(page.resume_cursor)
    assert state["since"] == page.items[0].filled_at.isoformat()


@respx.mock
def test_resume_cursor_is_honoured_as_since_on_the_next_incremental_sync():
    route = respx.get(f"{BASE_URL}/api/v3/brokerage/orders/historical/fills").mock(
        return_value=httpx.Response(200, json={"fills": [], "cursor": ""})
    )
    resume = json.dumps({"since": "2026-08-01T12:00:00+00:00"})
    _connector().fetch_asset_fills_page(cursor=resume)
    assert (
        route.calls.last.request.url.params["start_sequence_timestamp"]
        == "2026-08-01T12:00:00+00:00"
    )


# ---- balances --------------------------------------------------------------


@respx.mock
def test_fetch_balances_sums_available_and_hold_per_asset():
    respx.get(f"{BASE_URL}/api/v3/brokerage/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_accounts.json"))
    )
    balances = _connector().fetch_balances()
    by_asset = {a.asset: a for a in balances.assets}
    assert by_asset["BTC"].atoms == 50_000_000  # 0.5 BTC, 8dp, no hold
    assert by_asset["BTC"].decimals == 8
    # 1234.56 + 10.00 = 1244.56 USD at 6dp.
    assert by_asset["USD"].atoms == 1_244_560_000
    assert by_asset["USD"].decimals == 6
    assert balances.equity_usd_micros is None


@respx.mock
def test_m7_fetch_balance_falls_back_to_usd_cash():
    respx.get(f"{BASE_URL}/api/v3/brokerage/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_accounts.json"))
    )
    balance = _connector().fetch_balance()
    assert balance.balance_micros == 1_244_560_000


# ---- events / transfers ------------------------------------------------------


@respx.mock
def test_fetch_events_walks_every_account_and_skips_advanced_trade_fill():
    respx.get(f"{BASE_URL}/api/v3/brokerage/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_accounts.json"))
    )
    respx.get(f"{BASE_URL}/v2/accounts/8bfc20d7-f7c6-4422-bf07-8243ca4169fe/transactions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_transactions_btc.json"))
    )
    respx.get(f"{BASE_URL}/v2/accounts/8bfc20d7-f7c6-4422-bf07-8243ca4169ff/transactions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_transactions_usd.json"))
    )
    events = _connector().fetch_events()
    refs = [e.venue_ref for e in events]
    # advanced_trade_fill (tx-btc id ...66d) is skipped: it duplicates a row
    # fetch_asset_fills_page already returns.
    assert "57ffb4ae-0c59-5430-bcd3-3f98f797a66d" not in refs
    assert len(events) == 3

    receive = next(e for e in events if e.venue_ref == "57ffb4ae-0c59-5430-bcd3-3f98f797a66c")
    assert receive.kind is LegKind.DEPOSIT
    # 0.01 BTC received, 8dp -> 1,000,000 atoms.
    assert receive.legs[0].delta_atoms == 1_000_000
    assert receive.legs[0].asset == "BTC"

    deposit = next(e for e in events if e.venue_ref == "9d1e6c1a-2222-4444-8888-000000000001")
    assert deposit.kind is LegKind.DEPOSIT
    assert deposit.legs[0].delta_atoms == 1_000_000_000  # $1000.00 at 6dp

    staking = next(e for e in events if e.venue_ref == "9d1e6c1a-2222-4444-8888-000000000002")
    assert staking.kind is LegKind.STAKING
    assert staking.legs[0].delta_atoms == 750_000  # $0.75 at 6dp


@respx.mock
def test_fetch_events_since_filters_client_side():
    respx.get(f"{BASE_URL}/api/v3/brokerage/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_accounts.json"))
    )
    respx.get(f"{BASE_URL}/v2/accounts/8bfc20d7-f7c6-4422-bf07-8243ca4169fe/transactions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_transactions_btc.json"))
    )
    respx.get(f"{BASE_URL}/v2/accounts/8bfc20d7-f7c6-4422-bf07-8243ca4169ff/transactions").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_transactions_usd.json"))
    )
    since = datetime(2026, 6, 16, tzinfo=UTC)  # excludes the 2026-06-15 fiat_deposit only
    events = _connector().fetch_events(since=since)
    assert sorted(e.venue_ref for e in events) == sorted(
        ["57ffb4ae-0c59-5430-bcd3-3f98f797a66c", "9d1e6c1a-2222-4444-8888-000000000002"]
    )


# ---- rate limiting -----------------------------------------------------------


@respx.mock
def test_429_raises_rate_limited_with_retry_after():
    respx.get(f"{BASE_URL}/api/v3/brokerage/accounts").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"})
    )
    with pytest.raises(RateLimited) as exc_info:
        _connector().fetch_balances()
    assert exc_info.value.retry_after_seconds == 12.0
    assert exc_info.value.endpoint == "Accounts"


@respx.mock
def test_market_data_429_raises_rate_limited():
    respx.get(f"{BASE_URL}/api/v3/brokerage/market/products/BTC-USD").mock(
        return_value=httpx.Response(418, headers={"Retry-After": "3"})
    )
    with pytest.raises(RateLimited):
        CoinbaseMarketData().fetch_quote("BTC-USD")


# ---- public market data -------------------------------------------------------


@respx.mock
def test_fetch_instruments_derives_decimals_from_base_increment():
    # The real fixture's `pagination.has_next` is True (it was captured with
    # `limit=3` against ~700 real products); a second, final page ends the
    # walk so the test does not loop forever replaying the same fixture.
    final_page = {"products": [], "num_products": 0, "pagination": {"has_next": False}}
    respx.get(f"{BASE_URL}/api/v3/brokerage/market/products").mock(
        side_effect=[
            httpx.Response(200, json=_fixture("coinbase_products.json")),
            httpx.Response(200, json=final_page),
        ]
    )
    instruments = CoinbaseMarketData().fetch_instruments()
    btc = next(i for i in instruments if i.symbol == "BTC-USD")
    assert btc.base_asset == "BTC"
    assert btc.quote_asset == "USD"
    assert btc.qty_decimals == 8  # base_increment "0.00000001"
    assert btc.qty_step_atoms == 1


@respx.mock
def test_fetch_daily_closes_sorts_oldest_first():
    respx.get(f"{BASE_URL}/api/v3/brokerage/market/products/BTC-USD/candles").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_candles_btc_usd.json"))
    )
    closes = CoinbaseMarketData().fetch_daily_closes(
        "BTC-USD", datetime(2026, 9, 1, tzinfo=UTC).date(), datetime(2026, 9, 26, tzinfo=UTC).date()
    )
    assert closes == sorted(closes, key=lambda c: c.day)
    assert len(closes) == 5


@respx.mock
def test_fetch_quote_returns_last_price_only_no_bid_ask():
    respx.get(f"{BASE_URL}/api/v3/brokerage/market/products/BTC-USD").mock(
        return_value=httpx.Response(200, json=_fixture("coinbase_product_btc_usd.json"))
    )
    quote = CoinbaseMarketData().fetch_quote("BTC-USD")
    assert quote is not None
    assert quote.bid is None
    assert quote.ask is None
    assert quote.last == _fixture("coinbase_product_btc_usd.json")["price"]


@respx.mock
def test_fetch_quote_returns_none_on_404():
    respx.get(f"{BASE_URL}/api/v3/brokerage/market/products/DOESNOTEXIST-USD").mock(
        return_value=httpx.Response(404, json=_fixture("coinbase_product_404.json"))
    )
    assert CoinbaseMarketData().fetch_quote("DOESNOTEXIST-USD") is None


# ---- registry --------------------------------------------------------------


def test_registry_profile_is_gated_behind_partner_approval():
    profile = get_profile("coinbase")
    assert profile.requires_partner_approval is True
    assert profile.instrument.value == "asset"
    assert profile.credential.value == "key_secret"
    assert profile.tos_doc == "docs/venues/coinbase.md"


def test_build_connector_wires_a_working_key():
    secret, _key = _ed25519_secret()
    conn = build_connector("coinbase", "prod", "organizations/org/apiKeys/test-key", secret)
    assert isinstance(conn, CoinbaseConnector)
    assert conn.key_name == "organizations/org/apiKeys/test-key"


def test_build_connector_rejects_missing_secret():
    with pytest.raises(ValueError):
        build_connector("coinbase", "prod", "organizations/org/apiKeys/test-key", None)


def test_build_market_data_returns_coinbase_market_data():
    assert isinstance(build_market_data("coinbase", "prod"), CoinbaseMarketData)
