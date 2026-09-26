"""SnapTrade connector: signing, mapping to legs, pagination, rate limits and
read-only proof, all against respx mocks of the docs-example payloads in
tests/fixtures/snaptrade/ (see that directory's README for provenance) --
invariant #6, no live API calls in tests."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.connectors.base import Action, LegKind, RateLimited
from alphamarket.connectors.registry import build_connector, build_market_data, get_profile
from alphamarket.connectors.snaptrade import (
    BASE_URL,
    SnapTradeConnector,
    SnapTradeMarketData,
    SnapTradeUserExists,
    _date_only,
    content_hash,
    delete_user,
    generate_connection_portal_url,
    register_user,
    sign,
)

FIXTURES = Path(__file__).parent / "fixtures" / "snaptrade"
CLIENT_ID = "KOINE-TEST"
CONSUMER_KEY = "consumer-secret-key"
USER_ID = "trader-42"
USER_SECRET = "user-secret-abc"
ACCOUNT_REF = "917c8734-8470-4a3e-a18f-57c3f2ee6631"
AUTH_ID = "87b24961-b51e-4db8-9226-f198f6518a89"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def _connector(account_ref: str | None = ACCOUNT_REF) -> SnapTradeConnector:
    c = SnapTradeConnector(CLIENT_ID, CONSUMER_KEY, USER_ID, USER_SECRET, base_url=BASE_URL)
    c.account_ref = account_ref
    return c


def _mock_activities(payload) -> respx.Route:
    return respx.get(f"{BASE_URL}/accounts/{ACCOUNT_REF}/activities").mock(
        return_value=httpx.Response(200, json=payload)
    )


# ---- signing ----------------------------------------------------------------


def test_sign_matches_documented_algorithm():
    """docs example https://docs.snaptrade.com/docs/request-signatures ,
    checked 2026-09-26: the mockSignature request shape is
    content={"userId": "api@passiv.com", "userSecret": "CHRIS.P.BACON"},
    path="/api/v1/snapTrade/mockSignature",
    query="clientId=PASSIVTEST&timestamp=1635790389". No numeric output is
    published (the docs use placeholder keys), so this pins THIS module's
    reproduction of the documented algorithm against a concrete key -- a
    regression test, not a third-party vector (see fixtures/snaptrade/README.md).
    Hand-computed: canonical JSON is
    {"content":{"userId":"api@passiv.com","userSecret":"CHRIS.P.BACON"},
    "path":"/api/v1/snapTrade/mockSignature",
    "query":"clientId=PASSIVTEST&timestamp=1635790389"} (keys sorted, no
    whitespace), then base64(HMAC-SHA256("test-consumer-key", <that>))."""
    sig = sign(
        "test-consumer-key",
        "/api/v1/snapTrade/mockSignature",
        "clientId=PASSIVTEST&timestamp=1635790389",
        {"userId": "api@passiv.com", "userSecret": "CHRIS.P.BACON"},
    )
    assert sig == "eXlc+rG2r3kIEUtDy6JkSYXVwpa5wruoH36jXOb9Tz0="


def test_sign_empty_body_signs_null_content():
    # A GET has no body: {"content": null, ...}, never {"content": {}, ...}.
    with_none = sign(CONSUMER_KEY, "/api/v1/accounts", "clientId=x&timestamp=1", None)
    with_empty = sign(CONSUMER_KEY, "/api/v1/accounts", "clientId=x&timestamp=1", {})
    assert with_none == with_empty


@respx.mock
def test_request_signs_exact_query_bytes_and_includes_user_credentials():
    route = respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_accounts.json"))
    )
    _connector().list_accounts()
    sent = route.calls[0].request
    raw_query = str(sent.url.query, "ascii")
    params = dict(pair.split("=") for pair in raw_query.split("&"))
    assert params["clientId"] == CLIENT_ID
    assert params["userId"] == USER_ID
    assert params["userSecret"] == USER_SECRET
    assert "timestamp" in params
    # The signature must cover these exact bytes.
    expected = sign(CONSUMER_KEY, "/api/v1/accounts", raw_query, None)
    assert sent.headers["Signature"] == expected


# ---- rate limiting -----------------------------------------------------------


@respx.mock
def test_429_raises_rate_limited_parsing_documented_body():
    # docs example https://docs.snaptrade.com/docs/ratelimiting , checked
    # 2026-09-26: no Retry-After header, so retry_after comes from the
    # "Expected available in N seconds" body text.
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(
            429,
            json={
                "detail": "Request was throttled. Expected available in 7 seconds.",
                "status_code": 429,
                "code": "0000",
            },
        )
    )
    with pytest.raises(RateLimited) as exc:
        _connector().list_accounts()
    assert exc.value.retry_after_seconds == 7.0
    assert exc.value.endpoint == "ListAccounts"


@respx.mock
def test_429_prefers_retry_after_header_when_present():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"}, json={"detail": "x"})
    )
    with pytest.raises(RateLimited) as exc:
        _connector().list_accounts()
    assert exc.value.retry_after_seconds == 3.0


@respx.mock
def test_throttle_is_called_before_every_request():
    class _Recorder:
        def __init__(self):
            self.seen: list[str] = []

        def before(self, endpoint):
            self.seen.append(endpoint)

        def after(self, endpoint, extra_cost=0, used=None):
            pass

    respx.get(f"{BASE_URL}/accounts/{ACCOUNT_REF}/balances").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_balances.json"))
    )
    rec = _Recorder()
    c = _connector()
    c.set_throttle(rec)
    c.fetch_balances()
    assert rec.seen == ["Balances"]


# ---- accounts / balances ------------------------------------------------------


@respx.mock
def test_list_accounts_maps_the_documented_shape():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_accounts.json"))
    )
    accounts = _connector().list_accounts()
    assert len(accounts) == 1
    a = accounts[0]
    assert a["account_ref"] == ACCOUNT_REF
    assert a["institution_name"] == "Robinhood"
    assert a["brokerage_authorization"] == AUTH_ID
    assert a["is_paper"] is False


@respx.mock
def test_fetch_balances_and_m7_balance_wrapper():
    respx.get(f"{BASE_URL}/accounts/{ACCOUNT_REF}/balances").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_balances.json"))
    )
    balances = _connector().fetch_balances()
    # 300.71 USD x 1e6 = 300_710_000 atoms, hand-computed from the docs example.
    assert len(balances.assets) == 1
    asset = balances.assets[0]
    assert asset.asset == "USD" and asset.atoms == 300_710_000 and asset.decimals == 6
    # AssetVenueConnector.fetch_balance() rescales the single USD asset to
    # micros 1:1 since decimals == 6 already.
    assert _connector().check_connection().balance_micros == 300_710_000


def test_no_account_ref_raises_before_any_request():
    with pytest.raises(ValueError, match="account_ref"):
        _connector(account_ref=None).fetch_balances()


# ---- read-only proof (D4: aggregator_flag) -------------------------------------


@respx.mock
def test_prove_read_only_true_for_a_read_connection():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_accounts.json"))
    )
    respx.get(f"{BASE_URL}/authorizations/{AUTH_ID}").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_authorization_read.json"))
    )
    evidence = _connector().prove_read_only()
    assert evidence.method.value == "aggregator_flag"
    assert evidence.read_only is True
    assert evidence.details["connection_type"] == "read"


@respx.mock
def test_prove_read_only_false_for_a_trade_connection():
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_accounts.json"))
    )
    respx.get(f"{BASE_URL}/authorizations/{AUTH_ID}").mock(
        return_value=httpx.Response(200, json=_fixture("snaptrade_authorization_trade.json"))
    )
    evidence = _connector().prove_read_only()
    assert evidence.read_only is False


def test_check_write_scope_is_undeterminable_directly():
    # SnapTrade has no scope-probe endpoint; only prove_read_only() (via the
    # authorization's connectionType) answers the question.
    assert _connector().check_write_scope() is None


# ---- asset fills (BUY / SELL) --------------------------------------------------


@respx.mock
def test_buy_fill_legs_hand_computed():
    _mock_activities(_fixture("snaptrade_activities_fills.json"))
    page = _connector().fetch_asset_fills_page()
    buy, sell = page.items
    assert buy.instrument.symbol == "AAPL" and buy.action is Action.BUY
    assert buy.qty_atoms == 10_000_000  # 10 shares x 1e6
    base, quote, fee = buy.legs
    assert base.kind is LegKind.FILL_BASE and base.asset == "AAPL"
    assert base.delta_atoms == 10_000_000
    # 10 x $150.25 = $1502.50 gross (FILL_QUOTE) and -$1.00 fee (FEE), two
    # separate legs per base.py's NormalizedAssetFill docstring; together
    # they net to the $1503.50 actually paid out.
    assert quote.kind is LegKind.FILL_QUOTE and quote.asset == "USD"
    assert quote.delta_atoms == -1_502_500_000
    assert fee.kind is LegKind.FEE and fee.delta_atoms == -1_000_000
    assert quote.delta_atoms + fee.delta_atoms == -1_503_500_000
    assert page.next_cursor is None and page.resume_cursor is None

    assert sell.instrument.symbol == "MSFT" and sell.action is Action.SELL
    assert sell.qty_atoms == 4_500_000  # 4.5 shares
    base, quote, fee = sell.legs
    assert base.delta_atoms == -4_500_000  # selling reduces the holding
    # 4.5 x $300.10 = $1350.45 gross (FILL_QUOTE) and -$0.50 fee (FEE);
    # together they net to the $1349.95 actually received.
    assert quote.delta_atoms == 1_350_450_000
    assert fee.delta_atoms == -500_000
    assert quote.delta_atoms + fee.delta_atoms == 1_349_950_000


@respx.mock
def test_fill_id_is_a_content_hash_stable_across_reprocessing():
    rows = _fixture("snaptrade_activities_fills.json")
    reprocessed = copy.deepcopy(rows)
    reprocessed["data"][0]["id"] = "a-totally-different-id-after-reprocessing"
    changed = copy.deepcopy(rows)
    changed["data"][0]["units"] = 11  # a genuinely different fill

    route = respx.get(f"{BASE_URL}/accounts/{ACCOUNT_REF}/activities")
    route.mock(
        side_effect=[
            httpx.Response(200, json=rows),
            httpx.Response(200, json=reprocessed),
            httpx.Response(200, json=changed),
        ]
    )
    first = _connector().fetch_asset_fills_page().items[0].venue_fill_id
    second = _connector().fetch_asset_fills_page().items[0].venue_fill_id
    third = _connector().fetch_asset_fills_page().items[0].venue_fill_id
    assert first == second  # SnapTrade's own `id` is never used for identity
    assert third != first


def test_content_hash_ignores_none_vs_missing_consistently():
    assert content_hash("a", None, "c") == content_hash("a", None, "c")
    assert content_hash("a", "b") != content_hash("a", "c")


def test_asset_fills_pagination_advances_offset_by_page_size(monkeypatch):
    import alphamarket.connectors.snaptrade as st

    monkeypatch.setattr(st, "PAGE_SIZE", 2)
    c = _connector()
    calls: list[int] = []

    def fake_activities(account_ref, types, since, offset):
        calls.append(offset)
        rows = _fixture("snaptrade_activities_fills.json")["data"]
        if offset == 0:
            return rows  # exactly PAGE_SIZE (2) -> more pages
        return []  # second page: fewer than PAGE_SIZE -> done

    monkeypatch.setattr(c, "_account_activities", fake_activities)
    page1 = c.fetch_asset_fills_page()
    assert page1.next_cursor == "2" and len(page1.items) == 2
    page2 = c.fetch_asset_fills_page(cursor=page1.next_cursor)
    assert page2.next_cursor is None and page2.items == []
    assert calls == [0, 2]


# ---- events --------------------------------------------------------------------


@respx.mock
def test_options_activity_is_dropped_not_raised():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    events = _connector().fetch_events_page().items
    # 13 rows in the fixture, 1 is OPTIONEXPIRATION -> 12 events survive.
    assert len(events) == 12
    assert all(e.description != "Option expired worthless" for e in events)


@respx.mock
def test_contribution_and_withdrawal_are_deposit_withdrawal_cash_legs():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    by_desc = {e.description: e for e in _connector().fetch_events_page().items}
    contrib = by_desc["ACH deposit"]
    assert contrib.kind is LegKind.DEPOSIT
    assert contrib.legs[0].asset == "USD" and contrib.legs[0].delta_atoms == 500_000_000  # $500.00
    withdrawal = by_desc["ACH withdrawal"]
    assert withdrawal.kind is LegKind.WITHDRAWAL
    assert withdrawal.legs[0].delta_atoms == -200_000_000  # -$200.00


@respx.mock
def test_dividend_interest_fee_tax_cash_legs_hand_computed():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    by_desc = {e.description: e for e in _connector().fetch_events_page().items}

    div = by_desc["AAPL cash dividend"]
    assert div.kind is LegKind.DIVIDEND
    assert div.legs[0].asset == "USD" and div.legs[0].delta_atoms == 12_340_000  # $12.34

    interest = by_desc["Cash sweep interest"]
    assert interest.kind is LegKind.INTEREST
    assert interest.legs[0].delta_atoms == 50_000  # $0.05

    fee = by_desc["Account fee"]
    assert fee.kind is LegKind.FEE
    # amount was reported as -2.50; FEE legs are always negative (base.py),
    # and -abs(-2.50) = -2.50 -> -2_500_000 atoms either way.
    assert fee.legs[0].delta_atoms == -2_500_000

    tax = by_desc["NRA withholding"]
    assert tax.kind is LegKind.TAX
    assert tax.legs[0].delta_atoms == -1_850_000  # -$1.85


@respx.mock
def test_split_and_stock_dividend_are_quantity_only_legs():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    by_desc = {e.description: e for e in _connector().fetch_events_page().items}

    split = by_desc["TSLA 2-for-1 split"]
    assert split.kind is LegKind.SPLIT
    (leg,) = split.legs
    assert leg.asset == "TSLA" and leg.kind is LegKind.SPLIT and leg.delta_atoms == 15_000_000
    assert leg.decimals == 6  # no cash leg: a quantity change with no cash (base.py)

    bonus = by_desc["AAPL bonus shares"]
    assert bonus.kind is LegKind.SPLIT
    assert bonus.legs[0].asset == "AAPL" and bonus.legs[0].delta_atoms == 2_000_000


@respx.mock
def test_reinvested_dividend_nets_cash_to_zero_and_adds_shares():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    by_desc = {e.description: e for e in _connector().fetch_events_page().items}
    rei = by_desc["AAPL dividend reinvestment"]
    assert rei.kind is LegKind.DIVIDEND
    assert len(rei.legs) == 3
    div_leg, quote_leg, base_leg = rei.legs
    assert div_leg.kind is LegKind.DIVIDEND and div_leg.delta_atoms == 12_340_000  # $12.34 reported
    assert quote_leg.kind is LegKind.FILL_QUOTE and quote_leg.delta_atoms == -12_340_000
    assert base_leg.kind is LegKind.FILL_BASE and base_leg.asset == "AAPL"
    assert base_leg.delta_atoms == 81_000  # 0.081 shares x 1e6
    # Net cash impact of the reinvestment is zero: income and reinvestment cancel.
    assert div_leg.delta_atoms + quote_leg.delta_atoms == 0


@respx.mock
def test_external_transfer_in_and_out_signed_by_type_not_reported_sign():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    by_desc = {e.description: e for e in _connector().fetch_events_page().items}
    tin = by_desc["ACATS transfer in from another broker"]
    assert tin.kind is LegKind.TRANSFER_IN
    assert tin.legs[0].asset == "GOOGL" and tin.legs[0].delta_atoms == 5_000_000
    tout = by_desc["ACATS transfer out to another broker"]
    assert tout.kind is LegKind.TRANSFER_OUT
    # Reported units was already -3; TRANSFER_OUT still normalises to a
    # negative delta via -abs(units), so a broker that reported +3 here would
    # give the same, correct, outflow.
    assert tout.legs[0].delta_atoms == -3_000_000


@respx.mock
def test_adjustment_with_a_symbol_is_a_quantity_leg():
    _mock_activities(_fixture("snaptrade_activities_events.json"))
    by_desc = {e.description: e for e in _connector().fetch_events_page().items}
    adj = by_desc["Broker rounding correction"]
    assert adj.kind is LegKind.ADJUSTMENT
    assert adj.legs[0].asset == "AAPL" and adj.legs[0].delta_atoms == 1_000  # 0.001 shares x 1e6


def test_adjustment_without_a_symbol_falls_back_to_a_cash_leg():
    c = _connector()
    row = {
        "type": "ADJUSTMENT",
        "symbol": None,
        "units": None,
        "price": None,
        "amount": 0.02,
        "currency": {"code": "USD"},
        "trade_date": None,
        "settlement_date": "2026-09-20T00:00:00.000Z",
        "description": "Cash-only correction",
    }
    event = SnapTradeConnector._to_event(c, ACCOUNT_REF, row)
    assert event.kind is LegKind.ADJUSTMENT
    assert event.legs[0].asset == "USD" and event.legs[0].delta_atoms == 20_000  # $0.02


def test_events_carry_ref_is_content_hash():
    c = _connector()
    row = {
        "type": "INTEREST",
        "symbol": None,
        "units": None,
        "price": None,
        "amount": 1.0,
        "currency": {"code": "USD"},
        "trade_date": None,
        "settlement_date": "2026-09-21T00:00:00.000Z",
        "description": "x",
    }
    event = SnapTradeConnector._to_event(c, ACCOUNT_REF, row)
    assert event.ref_is_content_hash is True


# ---- registration / connection portal / deletion -------------------------------


@respx.mock
def test_register_user_returns_user_secret():
    respx.post(f"{BASE_URL}/snapTrade/registerUser").mock(
        return_value=httpx.Response(
            200, json={"userId": "snaptrade-user-123", "userSecret": "adf2aa34-8219-40f7"}
        )
    )
    user_id, user_secret = register_user(CLIENT_ID, CONSUMER_KEY, "snaptrade-user-123")
    assert user_id == "snaptrade-user-123"
    assert user_secret == "adf2aa34-8219-40f7"


@respx.mock
def test_register_user_conflict_raises():
    respx.post(f"{BASE_URL}/snapTrade/registerUser").mock(return_value=httpx.Response(409))
    with pytest.raises(SnapTradeUserExists):
        register_user(CLIENT_ID, CONSUMER_KEY, "dupe")


@respx.mock
def test_connection_portal_requests_read_only():
    route = respx.post(f"{BASE_URL}/snapTrade/login").mock(
        return_value=httpx.Response(200, json={"redirectURI": "https://connect.snaptrade.com/x"})
    )
    url = generate_connection_portal_url(CLIENT_ID, CONSUMER_KEY, USER_ID, USER_SECRET)
    assert url == "https://connect.snaptrade.com/x"
    body = json.loads(route.calls[0].request.content)
    assert body["connectionType"] == "read"


@respx.mock
def test_delete_user_queues_deletion():
    respx.delete(f"{BASE_URL}/snapTrade/deleteUser").mock(
        return_value=httpx.Response(200, json={"status": "deleted", "userId": USER_ID})
    )
    result = delete_user(CLIENT_ID, CONSUMER_KEY, USER_ID)
    assert result["status"] == "deleted"


# ---- market data (no public unauthenticated endpoint exists) -------------------


def test_market_data_has_nothing_public_to_offer():
    md = SnapTradeMarketData()
    assert md.fetch_market("AAPL") is None
    assert md.fetch_instruments() == []
    start, end = datetime(2026, 1, 1, tzinfo=UTC).date(), datetime(2026, 1, 2, tzinfo=UTC).date()
    assert md.fetch_daily_closes("AAPL", start, end) == []
    assert md.fetch_quote("AAPL") is None


# ---- registry --------------------------------------------------------------------


def test_registry_profile_fields():
    profile = get_profile("snaptrade")
    assert profile.readonly_proof.value == "aggregator_flag"
    assert profile.daily_resolution is True
    assert profile.instrument.value == "asset"
    assert profile.tos_doc == "docs/venues/snaptrade.md"
    assert profile.rate_budgets[0].capacity == 250


def test_registry_build_connector_needs_settings(monkeypatch):
    from alphamarket import config

    monkeypatch.setattr(config.get_settings(), "snaptrade_client_id", "")
    with pytest.raises(ValueError, match="snaptrade_client_id"):
        build_connector("snaptrade", "prod", USER_ID, USER_SECRET)


def test_registry_build_connector_and_market_data(monkeypatch):
    from alphamarket import config

    monkeypatch.setattr(config.get_settings(), "snaptrade_client_id", CLIENT_ID)
    monkeypatch.setattr(config.get_settings(), "snaptrade_consumer_key", CONSUMER_KEY)
    conn = build_connector("snaptrade", "prod", USER_ID, USER_SECRET, account_ref=ACCOUNT_REF)
    assert isinstance(conn, SnapTradeConnector)
    assert conn.account_ref == ACCOUNT_REF
    assert conn.user_id == USER_ID
    assert isinstance(build_market_data("snaptrade", "prod"), SnapTradeMarketData)


def test_date_only_uses_end_of_day_utc():
    d = _date_only("2026-09-10T00:00:00.000Z")
    assert d == datetime(2026, 9, 10, 23, 59, 59, tzinfo=UTC)
    assert d.tzinfo is UTC
