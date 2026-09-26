"""IBKR Flex connector tests, respx only (invariant #6). No IBKR account
exists yet, so the statement fixtures are reconstructed from IBKR's own
published field names, cross-checked against the open-source `ibflex`
client (see tests/fixtures/ibkr/README.md for provenance and links)."""

from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.connectors.base import (
    Action,
    BalanceUnavailable,
    LegKind,
    RateLimited,
    ReadOnlyProof,
)
from alphamarket.connectors.ibkr import (
    GET_STATEMENT_URL,
    SEND_REQUEST_URL,
    IbkrConnector,
    IbkrMarketData,
    IbkrStatementError,
)
from alphamarket.money import to_atoms, to_micros

FIXTURES = Path(__file__).parent / "fixtures" / "ibkr"


def _xml(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _connector(account_ref: str | None = "U1234567") -> IbkrConnector:
    conn = IbkrConnector(query_id="123456", token="t0ken", poll_interval_seconds=0.0)
    conn.account_ref = account_ref
    return conn


def _mock_happy_path():
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    return respx.get(GET_STATEMENT_URL).mock(
        return_value=httpx.Response(200, content=_xml("get_statement_success.xml"))
    )


# ---- transport: SendRequest/GetStatement, params, polling, rate limits -----


@respx.mock
def test_send_request_and_get_statement_use_documented_query_params():
    send_route = respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    stmt_route = respx.get(GET_STATEMENT_URL).mock(
        return_value=httpx.Response(200, content=_xml("get_statement_success.xml"))
    )
    _connector().fetch_balances()

    send_params = dict(send_route.calls[0].request.url.params)
    assert send_params == {"t": "t0ken", "q": "123456", "v": "3", "p": "1"}
    stmt_params = dict(stmt_route.calls[0].request.url.params)
    assert stmt_params == {"t": "t0ken", "q": "5678901234", "v": "3"}


@respx.mock
def test_resume_cursor_requests_only_the_day_after_the_last_statement():
    send_route = respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    respx.get(GET_STATEMENT_URL).mock(
        return_value=httpx.Response(200, content=_xml("get_statement_success.xml"))
    )
    _connector().fetch_asset_fills_page(cursor="20260228")
    params = dict(send_route.calls[0].request.url.params)
    assert params["fd"] == "20260301"  # the day after the cursor
    assert params["td"] == date.today().strftime("%Y%m%d")


@respx.mock
def test_final_page_resume_cursor_is_the_statement_to_date():
    _mock_happy_path()
    page = _connector().fetch_asset_fills_page()
    assert page.next_cursor is None  # one statement is always the whole page
    assert page.resume_cursor == "20260301"  # FlexStatement toDate


@respx.mock
def test_statement_not_ready_polls_then_succeeds():
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    stmt_route = respx.get(GET_STATEMENT_URL).mock(
        side_effect=[
            httpx.Response(200, content=_xml("get_statement_in_progress.xml")),
            httpx.Response(200, content=_xml("get_statement_in_progress.xml")),
            httpx.Response(200, content=_xml("get_statement_success.xml")),
        ]
    )
    balances = _connector().fetch_balances()
    assert stmt_route.call_count == 3
    assert balances.equity_usd_micros == to_micros("5084.20")


@respx.mock
def test_ibkr_error_1018_too_many_requests_raises_rate_limited():
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    respx.get(GET_STATEMENT_URL).mock(
        return_value=httpx.Response(200, content=_xml("get_statement_too_many_requests.xml"))
    )
    with pytest.raises(RateLimited) as exc:
        _connector().fetch_balances()
    assert exc.value.retry_after_seconds == 10.0


@respx.mock
def test_http_429_raises_rate_limited_with_retry_after_header():
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "30"})
    )
    with pytest.raises(RateLimited) as exc:
        _connector().fetch_balances()
    assert exc.value.retry_after_seconds == 30.0
    assert exc.value.endpoint == "SendRequest"


@respx.mock
def test_fatal_error_code_raises_ibkr_statement_error():
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_fail_bad_token.xml"))
    )
    with pytest.raises(IbkrStatementError) as exc:
        _connector().fetch_balances()
    assert exc.value.code == "1003"


# ---- Trades -> NormalizedAssetFill, hand-computed legs ---------------------


@respx.mock
def test_trades_map_to_asset_fills_with_hand_computed_legs():
    _mock_happy_path()
    fills = _connector().fetch_asset_fills_page().items
    assert [f.venue_fill_id for f in fills] == [
        "0001a2b3.65432101.01.01",  # ibExecID preferred over tradeID
        "0001a2b3.65432101.02.01",
    ]
    buy, sell = fills

    assert buy.action is Action.BUY
    # 10 shares at instrument decimals=6: 10 * 1e6 = 10,000,000 atoms.
    assert buy.qty_atoms == to_atoms("10", 6) == 10_000_000
    assert buy.price == "150.25"
    assert buy.filled_at == datetime(2026, 1, 10, 14, 30, 0, tzinfo=UTC)
    base_leg = next(leg for leg in buy.legs if leg.kind == LegKind.FILL_BASE)
    quote_leg = next(leg for leg in buy.legs if leg.kind == LegKind.FILL_QUOTE)
    fee_leg = next(leg for leg in buy.legs if leg.kind == LegKind.FEE)
    assert base_leg.asset == "AAPL"
    assert base_leg.delta_atoms == 10_000_000  # +qty for a buy
    # proceeds="-1502.50" = -(10 * 150.25); matches to_atoms exactly.
    assert quote_leg.asset == "USD"  # the settlement currency, not the symbol
    assert quote_leg.delta_atoms == to_atoms("-1502.50", 6) == -1_502_500_000
    assert fee_leg.delta_atoms == to_atoms("-1.00", 6) == -1_000_000
    assert not any(leg.kind == LegKind.TAX for leg in buy.legs)  # taxes="0" -> no leg

    assert sell.action is Action.SELL
    assert sell.qty_atoms == 5_000_000  # 5 * 1e6
    sell_base = next(leg for leg in sell.legs if leg.kind == LegKind.FILL_BASE)
    sell_quote = next(leg for leg in sell.legs if leg.kind == LegKind.FILL_QUOTE)
    assert sell_base.delta_atoms == -5_000_000  # -qty for a sell
    # proceeds="775.00" = 5 * 155.00
    assert sell_quote.delta_atoms == to_atoms("775.00", 6) == 775_000_000


def test_quote_leg_uses_currency_not_symbol():
    """Same rule as above (test_trades_map_to_asset_fills...), isolated as a
    direct unit test of `_to_asset_fill` with no HTTP layer involved."""
    conn = IbkrConnector(query_id="q", token="t")
    fill = conn._to_asset_fill(
        {
            "symbol": "AAPL",
            "currency": "USD",
            "buySell": "BUY",
            "quantity": "10",
            "tradePrice": "150.25",
            "proceeds": "-1502.50",
            "ibCommission": "-1.00",
            "ibCommissionCurrency": "USD",
            "taxes": "0",
            "tradeID": "1001",
            "ibExecID": "exec-1",
            "dateTime": "20260110;143000",
        }
    )
    quote_leg = next(leg for leg in fill.legs if leg.kind == LegKind.FILL_QUOTE)
    assert quote_leg.asset == "USD"


@respx.mock
def test_asset_category_filter_skips_non_equity_rows():
    """assetCategory must be STK; a row for anything else is dropped, not
    misrepresented as an equity fill."""
    conn = _connector(account_ref="U1234567")
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    body = (
        _xml("get_statement_success.xml")
        .decode()
        .replace(
            'assetCategory="STK" symbol="AAPL" currency="USD" buySell="BUY" quantity="10"',
            'assetCategory="OPT" symbol="AAPL" currency="USD" buySell="BUY" quantity="10"',
            1,
        )
    )
    respx.get(GET_STATEMENT_URL).mock(return_value=httpx.Response(200, content=body.encode()))
    fills = conn.fetch_asset_fills_page().items
    # Only the SELL trade (still STK) survives; the BUY row was retyped OPT.
    assert len(fills) == 1
    assert fills[0].action is Action.SELL


@respx.mock
def test_account_ref_filters_trades_and_positions():
    _mock_happy_path()
    other = _connector(account_ref="U9999999")
    fills = other.fetch_asset_fills_page().items
    assert len(fills) == 1
    assert fills[0].instrument.symbol == "MSFT"


# ---- CashTransactions -> NormalizedEvent, hand-computed legs ---------------


@respx.mock
def test_cash_transactions_map_to_events_with_hand_computed_legs():
    _mock_happy_path()
    events = {e.venue_ref: e for e in _connector().fetch_events_page().items}

    deposit = events["cash-dep-01"]
    assert deposit.kind == LegKind.DEPOSIT
    assert deposit.legs[0].delta_atoms == to_atoms("5000.00", 6) == 5_000_000_000
    # Date-only row: 23:59:59 UTC of the reported date (contract rule).
    assert deposit.occurred_at == datetime(2026, 1, 5, 23, 59, 59, tzinfo=UTC)

    dividend = events["cash-d-01"]
    assert dividend.kind == LegKind.DIVIDEND
    assert dividend.legs[0].delta_atoms == to_atoms("12.34", 6) == 12_340_000
    assert dividend.legs[0].instrument_symbol == "AAPL"

    tax = events["cash-t-01"]
    assert tax.kind == LegKind.TAX
    assert tax.legs[0].delta_atoms == to_atoms("-1.85", 6) == -1_850_000

    interest = events["cash-int-01"]
    assert interest.kind == LegKind.INTEREST
    assert interest.legs[0].delta_atoms == to_atoms("3.21", 6) == 3_210_000

    # The other account's deposit is filtered out.
    assert "cash-dep-99" not in events


@respx.mock
def test_unmapped_cash_transaction_type_becomes_adjustment_not_dropped():
    conn = _connector()
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_success.xml"))
    )
    body = (
        _xml("get_statement_success.xml")
        .decode()
        .replace('type="Broker Interest Received"', 'type="Some New IBKR Cash Type"')
    )
    respx.get(GET_STATEMENT_URL).mock(return_value=httpx.Response(200, content=body.encode()))
    events = {e.venue_ref: e for e in conn.fetch_events_page().items}
    adj = events["cash-int-01"]
    assert adj.kind == LegKind.ADJUSTMENT
    assert "Some New IBKR Cash Type" in (adj.description or "")


@respx.mock
def test_corporate_action_split_leg_hand_computed():
    _mock_happy_path()
    events = {e.venue_ref: e for e in _connector().fetch_events_page().items}
    split = events["ca-001"]
    assert split.kind == LegKind.SPLIT
    assert split.legs[0].asset == "XYZ"
    assert split.legs[0].delta_atoms == to_atoms("300", 6) == 300_000_000
    assert split.occurred_at == datetime(2026, 2, 10, 23, 59, 59, tzinfo=UTC)
    assert split.ref_is_content_hash is False


# ---- balances / positions / NAV --------------------------------------------


@respx.mock
def test_fetch_balances_picks_latest_equity_summary_row_and_positions():
    _mock_happy_path()
    balances = _connector().fetch_balances()
    # Two EquitySummaryByReportDateInBase rows exist (20260228, 20260301);
    # the connector must pick the later one, not document order.
    assert balances.as_of.date() == date(2026, 3, 1)
    assert balances.equity_usd_micros == to_micros("5084.20") == 5_084_200_000
    cash = next(a for a in balances.assets if a.asset == "USD")
    assert cash.atoms == to_atoms("4284.20", 6) == 4_284_200_000
    position = next(a for a in balances.assets if a.asset == "AAPL")
    assert position.atoms == to_atoms("5", 6) == 5_000_000
    assert position.entry_price == "150.25"
    assert position.mark_price == "160.00"
    # The other account's MSFT position must not leak in.
    assert not any(a.asset == "MSFT" for a in balances.assets)


@respx.mock
def test_check_connection_uses_equity_usd_micros():
    _mock_happy_path()
    balance = _connector().check_connection()
    assert balance.balance_micros == to_micros("5084.20")


def test_fetch_balance_raises_when_no_cash_asset_present():
    class _NoCash(IbkrConnector):
        def fetch_balances(self):
            from alphamarket.connectors.base import NormalizedBalances

            return NormalizedBalances(as_of=datetime.now(UTC), assets=[])

    with pytest.raises(BalanceUnavailable):
        _NoCash(query_id="q", token="t").fetch_balance()


# ---- read-only proof -------------------------------------------------------


@respx.mock
def test_prove_read_only_success_records_useful_non_secret_details():
    _mock_happy_path()
    evidence = _connector().prove_read_only()
    assert evidence.method is ReadOnlyProof.TOKEN_TYPE
    assert evidence.read_only is True
    assert evidence.details["account_id"] == "U1234567"
    assert evidence.details["when_generated"] == "20260301;180000"
    assert evidence.details["token_expiry_observable"] is False
    assert "t0ken" not in str(evidence.details)  # never secrets


@respx.mock
def test_prove_read_only_undetermined_on_fatal_error():
    respx.get(SEND_REQUEST_URL).mock(
        return_value=httpx.Response(200, content=_xml("send_request_fail_bad_token.xml"))
    )
    evidence = _connector().prove_read_only()
    assert evidence.read_only is None


def test_check_write_scope_is_always_false_with_no_network_call():
    # A Flex token has no endpoint that could ever accept a trade or
    # withdrawal instruction, so this makes no HTTP request at all; running
    # it with no respx routes mocked would raise if it tried to.
    assert _connector().check_write_scope() is False


# ---- market data stub -------------------------------------------------------


def test_market_data_is_a_deliberate_stub():
    md = IbkrMarketData()
    assert md.venue == "ibkr"
    assert md.fetch_market("AAPL") is None
    assert md.fetch_instruments() == []
    assert md.fetch_daily_closes("AAPL", date(2026, 1, 1), date(2026, 1, 2)) == []
    assert md.fetch_quote("AAPL") is None
    assert md.fetch_funding_history("AAPL", datetime.now(UTC), datetime.now(UTC)) == []
    assert md.fetch_corporate_actions(date(2026, 1, 1), date(2026, 1, 2)) == []


# ---- registry wiring --------------------------------------------------------


def test_registered_in_registry_with_v2_profile_fields():
    from alphamarket.connectors.base import CredentialKind, InstrumentKind, PaperFeeModel
    from alphamarket.connectors.registry import (
        build_connector,
        build_market_data,
        get_profile,
    )

    profile = get_profile("ibkr")
    assert profile.credential is CredentialKind.REPORT_TOKEN
    assert profile.instrument is InstrumentKind.ASSET
    assert profile.readonly_proof is ReadOnlyProof.TOKEN_TYPE
    assert profile.transfers_observable is True
    assert profile.daily_resolution is True
    assert profile.requires_partner_approval is True
    assert profile.tos_doc == "docs/venues/ibkr.md"
    assert profile.paper_fee_model is PaperFeeModel.US_EQUITY_REGULATORY
    assert profile.rate_budgets and profile.rate_budgets[0].cost("SendRequest") == 1

    conn = build_connector("ibkr", "prod", "123456", "t0ken", account_ref="U1234567")
    assert isinstance(conn, IbkrConnector)
    assert conn.account_ref == "U1234567"

    with pytest.raises(ValueError, match="Flex Web Service token"):
        build_connector("ibkr", "prod", "123456", None)

    md = build_market_data("ibkr", "prod")
    assert isinstance(md, IbkrMarketData)
