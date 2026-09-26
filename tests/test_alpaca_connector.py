"""Alpaca connector (OAuth, US equities). respx only -- invariant #6.

Fixtures are built from Alpaca's documented example responses; see
tests/fixtures/alpaca/README.md. Every expected atom count is hand-computed
in a comment: shares at 9 dp, USD at 6 dp."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.connectors.alpaca import (
    ALPACA_DATA_URL,
    AlpacaConnector,
    AlpacaMarketData,
    UnsupportedActivity,
    activity_time,
    asset_code,
    fill_notional_atoms,
    map_fill,
    map_non_trade,
    parse_token_secret,
    split_ratio,
)
from alphamarket.connectors.base import (
    Action,
    CorporateActionKind,
    CredentialExpired,
    CredentialKind,
    InstrumentKind,
    InstrumentType,
    LegKind,
    PaperFeeModel,
    RateLimited,
    RateScope,
    ReadOnlyProof,
)
from alphamarket.connectors.registry import build_connector, build_market_data, get_profile

PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"
FIXTURES = Path(__file__).parent / "fixtures" / "alpaca"
TOKEN = "test-token-not-real"


def _fx(name: str):
    return json.loads((FIXTURES / name).read_text())


def _conn(env="paper", scopes=(), **kw) -> AlpacaConnector:
    return AlpacaConnector(env=env, access_token=TOKEN, granted_scopes=scopes, **kw)


def _legs(item) -> dict[tuple[str, str], int]:
    return {(leg.kind, leg.asset): leg.delta_atoms for leg in item.legs}


class _Recorder:
    def __init__(self):
        self.seen: list[str] = []

    def before(self, endpoint):
        self.seen.append(endpoint)

    def after(self, endpoint, extra_cost=0, used=None):
        pass


# ---- auth header ------------------------------------------------------------------


@respx.mock
def test_requests_carry_the_bearer_token_and_nothing_else():
    route = respx.get(f"{PAPER}/v2/account").mock(
        return_value=httpx.Response(200, json=_fx("account.json"))
    )
    assert _conn().fetch_account_id() == "e6fe16f3-64a4-4921-8928-cadf02f92f98"
    headers = route.calls.last.request.headers
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert "APCA-API-SECRET-KEY" not in headers


def test_repr_never_shows_the_token():
    assert TOKEN not in repr(_conn(account_id="acct-1"))


# ---- fills ------------------------------------------------------------------------


def test_whole_share_buy_legs():
    fill = map_fill(_fx("activities_fills.json")[1])
    # AAPL buy 2 @ 174.78: +2 shares = 2 x 10^9 atoms; cash 2 x 174.78 = 349.56 USD
    # = 349_560_000 atoms at 6 dp, paid (negative).
    assert fill.venue_fill_id == "20220202135509981::2d7be4ff-d1f3-43e9-856a-0f5cf5c5088e"
    assert fill.order_id == "b5abe576-6a8a-49f3-a353-46b72c1ccae9"
    assert fill.action is Action.BUY
    assert fill.qty_atoms == 2_000_000_000
    assert fill.price == "174.78"
    assert fill.instrument.type is InstrumentType.EQUITY
    assert fill.instrument.qty_decimals == 9
    assert _legs(fill) == {
        (LegKind.FILL_BASE, "AAPL"): 2_000_000_000,
        (LegKind.FILL_QUOTE, "USD"): -349_560_000,
    }
    assert {leg.decimals for leg in fill.legs} == {9, 6}
    assert fill.filled_at == datetime(2022, 2, 2, 18, 55, 9, 981482, tzinfo=UTC)
    # Regulatory fees arrive later as FEE activities, never on the fill.
    assert all(leg.kind is not LegKind.FEE for leg in fill.legs)


def test_penny_stock_buy_legs():
    fill = map_fill(_fx("activities_fills.json")[0])
    # LPCN buy 1 @ 1.63: +1 share = 1_000_000_000; cash -1.63 USD = -1_630_000.
    assert _legs(fill) == {
        (LegKind.FILL_BASE, "LPCN"): 1_000_000_000,
        (LegKind.FILL_QUOTE, "USD"): -1_630_000,
    }


def test_fractional_sell_rounds_cash_half_even_to_micros():
    fill = map_fill(_fx("activities_fills.json")[2])
    # 0.123456789 x 174.78 = 21.481481286 (x174) + 0.09629629542 (x0.78)
    #                      = 21.57777758142 USD -> 6 dp half-even: 21.577778
    assert fill.action is Action.SELL
    assert fill.qty_atoms == 123_456_789
    assert _legs(fill) == {
        (LegKind.FILL_BASE, "AAPL"): -123_456_789,
        (LegKind.FILL_QUOTE, "USD"): 21_577_778,
    }
    # 2022-02-03T15:00:00.000000123Z: nanoseconds truncated, never rounded up.
    assert fill.filled_at == datetime(2022, 2, 3, 15, 0, 0, 0, tzinfo=UTC)


def test_fill_notional_is_exact_when_representable():
    # 3 x 910.10 = 2730.30 -> 2_730_300_000; 0.5 x 0.0001 = 0.00005 -> 50 atoms.
    assert fill_notional_atoms("3", "910.10") == 2_730_300_000
    assert fill_notional_atoms("0.5", "0.0001") == 50
    # 0.0000005 x 1 = 5e-7 USD: half-even to 6 dp -> 0 (ties to even).
    assert fill_notional_atoms("0.0000005", "1") == 0


def test_short_sales_and_options_are_unsupported_not_mapped():
    rows = _fx("activities_fills.json")
    short, option = map_fill(rows[3]), map_fill(rows[4])
    assert isinstance(short, UnsupportedActivity) and short.reason == "short_sale"
    assert short.symbol == "TSLA"
    assert isinstance(option, UnsupportedActivity) and option.reason == "option"
    crypto = map_fill({**rows[0], "symbol": "BTC/USD", "id": "x::1"})
    assert isinstance(crypto, UnsupportedActivity) and crypto.reason == "crypto"


@respx.mock
def test_fills_page_is_one_request_oldest_first_with_resume_cursor():
    route = respx.get(f"{PAPER}/v2/account/activities").mock(
        return_value=httpx.Response(200, json=_fx("activities_fills.json"))
    )
    conn = _conn()
    page = conn.fetch_asset_fills_page()
    assert route.call_count == 1
    params = route.calls.last.request.url.params
    assert params["activity_types"] == "FILL"
    assert params["direction"] == "asc"
    assert params["page_size"] == "100"
    assert "page_token" not in params and "after" not in params
    assert [f.venue_fill_id.split("::")[0] for f in page.items] == [
        "20190524113406977",
        "20220202135509981",
        "20220203150000000",
    ]
    # 5 rows < 100: this pass is done; resume after the LAST row seen, which
    # is the skipped option fill (skipped rows advance the cursor too).
    assert page.next_cursor is None
    assert page.resume_cursor == "20220205150000000::5c1a7e2e-0000-4000-8000-000000000003"
    skipped = conn.unsupported_activities()
    assert [(u.reason, u.symbol) for u in skipped] == [
        ("short_sale", "TSLA"),
        ("option", "AAPL230721C00150000"),
    ]
    assert conn.unsupported_activities() == []  # drained


def _fill_row(i: int) -> dict:
    return {
        "activity_type": "FILL",
        "id": f"2022010100000{i:04d}::id-{i}",
        "price": "10",
        "qty": "1",
        "side": "buy",
        "symbol": "SPY",
        "transaction_time": f"2022-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
        "order_id": f"o-{i}",
        "type": "fill",
    }


@respx.mock
def test_full_page_hands_back_the_last_id_as_cursor_and_cursor_beats_since():
    full = [_fill_row(i) for i in range(100)]
    route = respx.get(f"{PAPER}/v2/account/activities")
    route.side_effect = [httpx.Response(200, json=full), httpx.Response(200, json=[])]
    conn = _conn()
    since = datetime(2021, 12, 31, 12, 0, tzinfo=UTC)
    first = conn.fetch_asset_fills_page(since=since)
    assert route.calls[0].request.url.params["after"] == "2021-12-31T12:00:00Z"
    assert len(first.items) == 100
    assert first.next_cursor == "20220101000000099::id-99"
    assert first.resume_cursor is None

    second = conn.fetch_asset_fills_page(cursor=first.next_cursor, since=since)
    params = route.calls[1].request.url.params
    assert params["page_token"] == "20220101000000099::id-99"
    assert "after" not in params  # cursor beats since
    # Empty final page: resume from the cursor we were given.
    assert second.items == []
    assert second.next_cursor is None
    assert second.resume_cursor == "20220101000000099::id-99"


@respx.mock
def test_fetch_asset_fills_follows_pages_to_the_end():
    route = respx.get(f"{PAPER}/v2/account/activities")
    route.side_effect = [
        httpx.Response(200, json=[_fill_row(i) for i in range(100)]),
        httpx.Response(200, json=[_fill_row(100)]),
    ]
    fills = _conn().fetch_asset_fills()
    assert len(fills) == 101
    assert len({f.venue_fill_id for f in fills}) == 101  # ids unique per account


@respx.mock
def test_empty_incremental_sync_with_since_resumes_from_since():
    respx.get(f"{PAPER}/v2/account/activities").mock(return_value=httpx.Response(200, json=[]))
    page = _conn().fetch_asset_fills_page(since=datetime(2026, 1, 1, tzinfo=UTC))
    assert page.items == [] and page.next_cursor is None and page.resume_cursor is None


# ---- non-trade activities ---------------------------------------------------------


@respx.mock
def test_events_page_maps_every_supported_type():
    route = respx.get(f"{PAPER}/v2/account/activities").mock(
        return_value=httpx.Response(200, json=_fx("activities_non_trade.json"))
    )
    conn = _conn()
    page = conn.fetch_events_page()
    assert route.calls.last.request.url.params["category"] == "non_trade_activity"
    got = [(e.kind, _legs(e)) for e in page.items]
    assert got == [
        # CSD 5000 -> +5000 USD = 5_000_000_000 atoms
        (LegKind.DEPOSIT, {(LegKind.DEPOSIT, "USD"): 5_000_000_000}),
        # DIV (CDIV) 1.02 -> +1_020_000
        (LegKind.DIVIDEND, {(LegKind.DIVIDEND, "USD"): 1_020_000}),
        # DIVNRA -0.31 -> TAX -310_000 (withholding, never a fee)
        (LegKind.TAX, {(LegKind.TAX, "USD"): -310_000}),
        # FEE (TAF) -0.02 -> -20_000
        (LegKind.FEE, {(LegKind.FEE, "USD"): -20_000}),
        # INT 0.37 -> +370_000
        (LegKind.INTEREST, {(LegKind.INTEREST, "USD"): 370_000}),
        # JNLC -250 -> cash journaled out of this account: -250_000_000
        (LegKind.TRANSFER_OUT, {(LegKind.TRANSFER_OUT, "USD"): -250_000_000}),
        # CSW -100 -> -100_000_000
        (LegKind.WITHDRAWAL, {(LegKind.WITHDRAWAL, "USD"): -100_000_000}),
        # SPLIT AAPL qty 15 -> +15 shares = 15_000_000_000 at 9 dp, no cash
        (LegKind.SPLIT, {(LegKind.SPLIT, "AAPL"): 15_000_000_000}),
    ]
    dividend = page.items[1]
    assert dividend.legs[0].instrument_symbol == "T"
    assert dividend.venue_ref == "20190802011955195::6a6a6a6a-0000-4000-8000-0000000d1v01"
    # Date-only rows land at 23:59:59 UTC of the reported date.
    assert page.items[0].occurred_at == datetime(2019, 7, 12, 23, 59, 59, tzinfo=UTC)
    assert page.resume_cursor == "20230725000000000::6a6a6a6a-0000-4000-8000-0000000mrg01"
    reasons = [(u.activity_type, u.reason) for u in conn.unsupported_activities()]
    assert reasons == [
        ("DIV", "stock_dividend"),
        ("OPEXP", "option"),
        ("CSD", "canceled"),
        ("MA", "unsupported_type"),
    ]


def test_cash_in_lieu_on_a_split_is_a_second_leg():
    row = {
        "activity_type": "SSP",
        "id": "20240610000000000::split-2",
        "date": "2024-06-10",
        "net_amount": "12.34",
        "qty": "-0.5",
        "symbol": "NVDA",
        "status": "executed",
    }
    event = map_non_trade(row)
    # -0.5 share = -500_000_000 atoms; 12.34 USD = 12_340_000 atoms
    assert _legs(event) == {
        (LegKind.SPLIT, "NVDA"): -500_000_000,
        (LegKind.SPLIT, "USD"): 12_340_000,
    }


def test_share_journal_and_acats_are_transfers_of_shares():
    event = map_non_trade(
        {
            "activity_type": "ACATS",
            "id": "20240101000000000::acats-1",
            "date": "2024-01-02",
            "net_amount": "0",
            "qty": "10.5",
            "symbol": "MSFT",
            "status": "executed",
        }
    )
    # +10.5 shares = 10_500_000_000 atoms
    assert event.kind is LegKind.TRANSFER_IN
    assert _legs(event) == {(LegKind.TRANSFER_IN, "MSFT"): 10_500_000_000}


def test_fee_refund_and_pass_through_rebate_are_adjustments():
    base = {"id": "a::1", "date": "2024-01-02", "status": "executed"}
    refund = map_non_trade({**base, "activity_type": "FEE", "net_amount": "0.02"})
    rebate = map_non_trade({**base, "activity_type": "PTR", "net_amount": "0.01"})
    assert refund.kind is LegKind.ADJUSTMENT and _legs(refund) == {
        (LegKind.ADJUSTMENT, "USD"): 20_000
    }
    assert rebate.kind is LegKind.ADJUSTMENT


def test_activity_time_rules():
    assert activity_time({"date": "2019-08-01"}) == datetime(2019, 8, 1, 23, 59, 59, tzinfo=UTC)
    assert activity_time({"date": "2019-08-01T14:01:04Z"}) == datetime(
        2019, 8, 1, 14, 1, 4, tzinfo=UTC
    )


def test_net_amount_finer_than_micros_is_refused_loudly():
    with pytest.raises(ValueError):
        map_non_trade(
            {"activity_type": "INT", "id": "x::2", "date": "2024-01-02", "net_amount": "0.0000001"}
        )


# ---- balances ---------------------------------------------------------------------


@respx.mock
def test_balances_vector_cash_positions_and_equity():
    respx.get(f"{PAPER}/v2/account").mock(
        return_value=httpx.Response(200, json=_fx("account.json"))
    )
    respx.get(f"{PAPER}/v2/positions").mock(
        return_value=httpx.Response(200, json=_fx("positions.json"))
    )
    rec = _Recorder()
    conn = _conn()
    conn.set_throttle(rec)
    bal = conn.fetch_balances()
    assert rec.seen == ["Account", "Positions"]
    assets = {a.asset: a for a in bal.assets}
    # cash "-23140.2" (margin) -> -23_140_200_000 atoms at 6 dp
    assert (assets["USD"].atoms, assets["USD"].decimals) == (-23_140_200_000, 6)
    # AAPL qty 5 -> 5_000_000_000 at 9 dp; SPY 2.123456789 -> 2_123_456_789
    assert (assets["AAPL"].atoms, assets["AAPL"].decimals) == (5_000_000_000, 9)
    assert assets["AAPL"].entry_price == "100.0"
    assert assets["AAPL"].mark_price == "120.0"
    assert assets["SPY"].atoms == 2_123_456_789
    # equity "103820.56" -> 103_820_560_000 micros; the M7 balance is equity.
    assert bal.equity_usd_micros == 103_820_560_000
    assert conn.account_id == "e6fe16f3-64a4-4921-8928-cadf02f92f98"


@respx.mock
def test_m7_balance_is_broker_equity():
    respx.get(f"{PAPER}/v2/account").mock(
        return_value=httpx.Response(200, json=_fx("account.json"))
    )
    respx.get(f"{PAPER}/v2/positions").mock(return_value=httpx.Response(200, json=[]))
    assert _conn().check_connection().balance_micros == 103_820_560_000


def test_asset_codes():
    assert asset_code("aapl", "us_equity") == "AAPL"
    assert asset_code("BTC/USD", "crypto") == "BTC"
    assert asset_code("BTCUSD", "crypto") == "BTC"
    assert asset_code("BRK.B") == "BRK.B"


# ---- errors -----------------------------------------------------------------------


@respx.mock
def test_429_raises_rate_limited_with_retry_after():
    respx.get(f"{PAPER}/v2/account/activities").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "slow"})
    )
    with pytest.raises(RateLimited) as exc:
        _conn().fetch_events_page()
    assert exc.value.retry_after_seconds == 7.0
    assert exc.value.endpoint == "Activities"


@respx.mock
def test_429_without_retry_after_falls_back_to_reset_header():
    reset = int(datetime.now(UTC).timestamp()) + 30
    respx.get(f"{PAPER}/v2/account").mock(
        return_value=httpx.Response(429, headers={"X-RateLimit-Reset": str(reset)})
    )
    with pytest.raises(RateLimited) as exc:
        _conn().fetch_account()
    assert 25 <= exc.value.retry_after_seconds <= 30


@respx.mock
def test_401_means_the_grant_is_gone():
    respx.get(f"{PAPER}/v2/account").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(CredentialExpired):
        _conn().fetch_account()


# ---- read-only proof --------------------------------------------------------------


def _mock_account(base=PAPER, **overrides):
    return respx.get(f"{base}/v2/account").mock(
        return_value=httpx.Response(200, json={**_fx("account.json"), **overrides})
    )


@respx.mock
def test_probe_refused_for_lack_of_scope_is_read_only():
    _mock_account()
    probe = respx.post(f"{PAPER}/v2/orders").mock(
        return_value=httpx.Response(403, json={"code": 40310000, "message": "forbidden"})
    )
    rec = _Recorder()
    conn = _conn(scopes=())
    conn.set_throttle(rec)
    ev = conn.prove_read_only()
    assert ev.method is ReadOnlyProof.ORDER_PROBE
    assert ev.read_only is True
    assert ev.details["probe_status"] == 403
    assert ev.details["granted_scopes"] == ""
    assert rec.seen == ["Account", "OrderProbe"]
    # What is sent can never execute: zero quantity, unlisted symbol, paper.
    sent = json.loads(probe.calls.last.request.content)
    assert sent["qty"] == "0"
    assert sent["symbol"] == "KOINEPROBE0"
    assert sent["client_order_id"].startswith("koine-readonly-probe-")
    assert conn.check_write_scope() is False
    assert TOKEN not in json.dumps(ev.details)


@respx.mock
def test_probe_that_reaches_validation_means_writable():
    _mock_account()
    respx.post(f"{PAPER}/v2/orders").mock(
        return_value=httpx.Response(422, json={"code": 40010001, "message": "qty must be > 0"})
    )
    ev = _conn(scopes=None).prove_read_only()
    assert ev.read_only is False
    assert ev.details["probe_message"] == "qty must be > 0"


@respx.mock
def test_buying_power_403_is_a_business_refusal_so_writable():
    _mock_account()
    respx.post(f"{PAPER}/v2/orders").mock(
        return_value=httpx.Response(403, json={"message": "insufficient buying power"})
    )
    assert _conn().prove_read_only().read_only is False


@respx.mock
def test_blocked_account_makes_a_refusal_inconclusive():
    _mock_account(trading_blocked=True)
    respx.post(f"{PAPER}/v2/orders").mock(return_value=httpx.Response(403, json={}))
    ev = _conn().prove_read_only()
    assert ev.read_only is None
    assert _conn().check_write_scope() is None


@respx.mock(assert_all_called=False)
def test_write_scope_in_the_grant_is_writable_without_any_request(respx_mock):
    route = respx_mock.route()  # anything at all
    ev = _conn(scopes=("trading",)).prove_read_only()
    assert ev.read_only is False
    assert ev.details["write_scopes_granted"] == "trading"
    assert route.call_count == 0


@respx.mock
def test_live_accounts_are_never_probed():
    _mock_account(base=LIVE)
    ev = _conn(env="live", scopes=()).prove_read_only()
    assert ev.read_only is True  # scope list alone: no write scope granted
    assert "never probed" in ev.details["probe"]
    assert not any(c.request.method == "POST" for c in respx.calls)
    # Unknown scopes on live: undetermined.
    assert _conn(env="live", scopes=None).prove_read_only().read_only is None


# ---- registry ---------------------------------------------------------------------


def test_profile_is_complete():
    p = get_profile("alpaca")
    assert p.credential is CredentialKind.OAUTH2
    assert p.instrument is InstrumentKind.ASSET
    assert p.readonly_proof is ReadOnlyProof.ORDER_PROBE
    assert p.paper_fee_model is PaperFeeModel.US_EQUITY_REGULATORY
    assert p.tos_doc == "docs/venues/alpaca.md"
    assert (Path(__file__).parent.parent / p.tos_doc).exists()
    assert p.envs == ("paper", "live")
    assert p.transfers_observable and p.balance_observable
    per_account, shared = p.rate_budgets
    assert per_account.scope is RateScope.CREDENTIAL and per_account.capacity == 200
    assert per_account.cost("Activities") == 1 and per_account.cost("Bars") == 0
    assert shared.cost("Bars") == 1 and shared.cost("Activities") == 0


def test_registry_builds_from_the_vault_token_json():
    secret = json.dumps({"access_token": TOKEN, "scope": "", "expires_at": None})
    conn = build_connector("alpaca", "live", "acct-9", secret)
    assert isinstance(conn, AlpacaConnector)
    assert conn.base_url == LIVE
    assert conn.granted_scopes == ()
    assert conn.account_id == "acct-9"
    assert parse_token_secret(TOKEN) == (TOKEN, None)
    with pytest.raises(ValueError):
        build_connector("alpaca", "prod", "acct", secret)


# ---- market data ------------------------------------------------------------------


def _md() -> AlpacaMarketData:
    return AlpacaMarketData(key_id="koine-key", secret_key="koine-secret")


@respx.mock
def test_instruments_step_by_fractionability():
    route = respx.get(f"{PAPER}/v2/assets").mock(
        return_value=httpx.Response(200, json=_fx("assets.json"))
    )
    insts = {i.symbol: i for i in _md().fetch_instruments()}
    assert route.calls.last.request.headers["APCA-API-KEY-ID"] == "koine-key"
    assert set(insts) == {"AAPL", "BRK.A"}  # untradable XYZQ dropped
    # Fractionable: 1 atom = 1e-9 share, $1 minimum; whole shares: 10^9 atoms.
    assert (insts["AAPL"].qty_step_atoms, insts["AAPL"].min_notional) == (1, "1")
    assert insts["BRK.A"].qty_step_atoms == 1_000_000_000
    assert insts["AAPL"].type is InstrumentType.EQUITY and insts["AAPL"].quote_asset == "USD"
    assert _md().pricing_symbol("AAPL", list(insts.values())) == "AAPL"


@respx.mock
def test_daily_closes_follow_page_tokens_in_new_york_days():
    route = respx.get(f"{ALPACA_DATA_URL}/v2/stocks/bars")
    route.side_effect = [
        httpx.Response(200, json=_fx("bars_daily.json")),
        httpx.Response(200, json=_fx("bars_daily_page2.json")),
    ]
    rec = _Recorder()
    md = _md()
    md.set_throttle(rec)
    closes = md.fetch_daily_closes("AAPL", date(2022, 1, 3), date(2022, 1, 5))
    # 05:00Z = 00:00 New York: the bar belongs to that New York date.
    assert [(c.day, c.close) for c in closes] == [
        (date(2022, 1, 3), "182.01"),
        (date(2022, 1, 4), "179.7"),
        (date(2022, 1, 5), "174.92"),
    ]
    first, second = (c.request.url.params for c in route.calls)
    assert first["timeframe"] == "1Day" and first["adjustment"] == "raw"
    assert second["page_token"] == "QUFQTHxEfDIwMjItMDEtMDRUMDU6MDA6MDAuMDAwMDAwMDAwWg=="
    assert rec.seen == ["Bars", "Bars"]


@respx.mock
def test_latest_quote():
    respx.get(f"{ALPACA_DATA_URL}/v2/stocks/quotes/latest").mock(
        return_value=httpx.Response(200, json=_fx("latest_quotes.json"))
    )
    q = _md().fetch_quote("AAPL")
    assert (q.bid, q.ask, q.last) == ("172.62", "172.7", None)
    assert q.as_of == datetime(2022, 8, 17, 10, 7, 40, 286587, tzinfo=UTC)
    assert _md().fetch_quote("NOPE") is None


@respx.mock
def test_corporate_actions_splits_dividends_symbol_changes():
    route = respx.get(f"{ALPACA_DATA_URL}/v1/corporate-actions").mock(
        return_value=httpx.Response(200, json=_fx("corporate_actions.json"))
    )
    actions = _md().fetch_corporate_actions(date(2023, 5, 1), date(2023, 8, 31), ["SRE"])
    assert route.calls.last.request.url.params["symbols"] == "SRE"
    got = [
        (a.symbol, a.kind, a.ex_date, a.ratio_new, a.ratio_old, a.cash_per_share, a.new_symbol)
        for a in actions
    ]
    assert got == [
        ("FCF", CorporateActionKind.CASH_DIVIDEND, date(2023, 5, 4), None, None, "0.125", None),
        ("BSAQ", CorporateActionKind.SYMBOL_CHANGE, date(2023, 8, 15), None, None, None, "VFS"),
        ("SRE", CorporateActionKind.SPLIT, date(2023, 8, 22), 2, 1, None, None),
        ("MNTS", CorporateActionKind.SPLIT, date(2023, 8, 24), 1, 50, None, None),
    ]
    assert actions[0].pay_date == date(2023, 5, 19)


def test_split_ratio_reduces_decimal_rates():
    # 1.5 new per 1 old = 3:2; 4 per 2 = 2:1.
    assert split_ratio(Decimal("1.5"), 1) == (3, 2)
    assert split_ratio(4, 2) == (2, 1)


@respx.mock
def test_market_data_429():
    respx.get(f"{ALPACA_DATA_URL}/v2/stocks/quotes/latest").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "3"})
    )
    with pytest.raises(RateLimited):
        _md().fetch_quote("AAPL")


def test_market_data_is_built_from_koines_own_key():
    md = build_market_data("alpaca", "paper")
    assert isinstance(md, AlpacaMarketData)
    with pytest.raises(RuntimeError):  # no key configured in tests
        md.fetch_quote("AAPL")
