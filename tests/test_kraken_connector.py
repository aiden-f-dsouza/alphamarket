"""Kraken connector against docs-example and recorded fixtures (tests/fixtures/kraken/),
never live calls -- invariant #6. Private endpoints are served by FakeKraken, a
respx side effect that verifies every API-Sign and applies Kraken's documented
window/offset semantics (start exclusive, end inclusive, newest first, 50 per
page) to fixture rows."""

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
import respx

from alphamarket.connectors.base import (
    Action,
    InstrumentKind,
    LegKind,
    PaperFeeModel,
    RateLimited,
    RateScope,
    ReadOnlyProof,
)
from alphamarket.connectors.kraken import (
    KRAKEN_API_URL,
    KrakenAuthError,
    KrakenConnector,
    KrakenError,
    KrakenMarketData,
    LedgerRow,
    NonceCounter,
    canonical_asset,
    ledger_event,
    sign,
    trade_fill,
    underlying_asset,
)
from alphamarket.connectors.registry import build_connector, build_market_data, get_profile

FIXTURES = Path(__file__).parent / "fixtures" / "kraken"
API = KRAKEN_API_URL
KEY = "test-api-key"
# Kraken's published signing example key (docs: Spot REST authentication).
SECRET = "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
NOW = 1790000000.9  # 2026-09-22; pinned `end` = 1790000000


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeKraken:
    """Private API double over fixture rows. Records (method, params)."""

    def __init__(self, ledger=None, trades=None, balance=None, overrides=None):
        self.ledger = dict(
            ledger if ledger is not None else _fixture("account_ledgers.json")["result"]["ledger"]
        )
        self.trades = dict(
            trades if trades is not None else _fixture("account_trades.json")["result"]["trades"]
        )
        self.balance = (
            balance if balance is not None else _fixture("account_balance.json")["result"]
        )
        self.overrides = overrides or {}
        self.calls: list[tuple[str, dict]] = []
        self.nonces: list[int] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = request.content.decode()
        params = dict(parse_qsl(body))
        assert request.headers["API-Key"] == KEY
        assert request.headers["API-Sign"] == sign(path, body, params["nonce"], SECRET)
        assert body.startswith("nonce=")
        self.nonces.append(int(params["nonce"]))
        method = path.rsplit("/", 1)[-1]
        self.calls.append((method, params))
        if method in self.overrides:
            out = self.overrides[method]
            out = out(params) if callable(out) else out
            return out if isinstance(out, httpx.Response) else httpx.Response(200, json=out)
        if method == "Balance":
            return httpx.Response(200, json={"error": [], "result": self.balance})
        if method == "Ledgers":
            rows = self.ledger
            if params.get("type", "all") != "all":
                rows = {k: v for k, v in rows.items() if v["type"] == params["type"]}
            rows = self._window(rows, params)
            page = dict(rows[int(params.get("ofs", 0)) :][:50])
            return httpx.Response(
                200, json={"error": [], "result": {"ledger": page, "count": len(rows)}}
            )
        if method == "TradesHistory":
            rows = self._window(self.trades, params)
            page = dict(rows[int(params.get("ofs", 0)) :][:50])
            if params.get("ledgers") != "true":
                page = {k: {f: x for f, x in v.items() if f != "ledgers"} for k, v in page.items()}
            return httpx.Response(
                200, json={"error": [], "result": {"trades": page, "count": len(rows)}}
            )
        return httpx.Response(200, json={"error": [f"EGeneral:Unknown method {method}"]})

    @staticmethod
    def _window(rows, params):
        def t(v):
            return Decimal(str(v["time"]))

        items = [
            (k, v)
            for k, v in rows.items()
            if ("start" not in params or t(v) > Decimal(params["start"]))
            and ("end" not in params or t(v) <= Decimal(params["end"]))
        ]
        return sorted(items, key=lambda kv: t(kv[1]), reverse=True)

    def of(self, method):
        return [p for m, p in self.calls if m == method]


def _connector(**kw) -> KrakenConnector:
    return KrakenConnector(
        KEY, SECRET, nonces=NonceCounter(clock=lambda: 1_000), clock=lambda: NOW, **kw
    )


def _mock(fake: FakeKraken):
    respx.post(url__regex=rf"{API}/0/private/\w+").mock(side_effect=fake)


def _by_kind(legs):
    return {(leg.kind, leg.asset): leg for leg in legs}


# ---- signing, nonces, asset codes --------------------------------------------


def test_signature_matches_krakens_published_example():
    # docs.kraken.com Spot REST authentication, worked example.
    postdata = "nonce=1616492376594&ordertype=limit&pair=XBTUSD&price=37500&type=buy&volume=1.25"
    expected = (
        "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ=="
    )
    assert sign("/0/private/AddOrder", postdata, 1616492376594, SECRET) == expected


def test_nonces_strictly_increase_per_key_even_with_a_stuck_clock():
    counter = NonceCounter(clock=lambda: 5_000_000)  # 5_000 microseconds, forever
    assert [counter.next("a") for _ in range(3)] == [5_000, 5_001, 5_002]
    assert counter.next("b") == 5_000  # independent per key


def test_bad_secret_is_rejected_without_echoing_it():
    with pytest.raises(ValueError) as exc:
        KrakenConnector(KEY, "not base64 !!")
    assert "not base64 !!" not in str(exc.value)
    assert SECRET not in repr(_connector())


def test_canonical_asset_codes_and_variants():
    assert canonical_asset("XXBT") == "BTC"
    assert canonical_asset("XETH") == "ETH"
    assert canonical_asset("ZUSD") == "USD"
    assert canonical_asset("XXDG") == "DOGE"
    assert canonical_asset("USDC") == "USDC"
    assert canonical_asset("XBT.M") == "BTC.M"
    assert canonical_asset("ETH2.S") == "ETH2.S"
    assert canonical_asset("USD.HOLD") == "USD.HOLD"
    assert underlying_asset("ETH2.S") == "ETH"
    assert underlying_asset("ETH2") == "ETH"
    assert underlying_asset("DOT28.S") == "DOT"
    assert underlying_asset("SOL03.S") == "SOL"
    assert underlying_asset("BTC.M") == "BTC"
    assert underlying_asset("API3.S") == "API3"  # one trailing digit is part of the name
    assert underlying_asset("BTC") == "BTC"


def test_alias_table_agrees_with_recorded_assets_altnames():
    # Every recorded code maps to its altname, renamed XBT->BTC / XDG->DOGE.
    # Variants rename their root only ("XBT.M" -> "BTC.M").
    renames = {"XBT": "BTC", "XDG": "DOGE", "FEE": "KFEE"}
    for code, info in _fixture("assets.json")["result"].items():
        root, dot, suffix = info["altname"].partition(".")
        assert canonical_asset(code) == renames.get(root, root) + dot + suffix, code


# ---- balances -----------------------------------------------------------------


@respx.mock
def test_fetch_balances_from_docs_example():
    _mock(FakeKraken(balance=_fixture("docs_balance.json")["result"]))
    conn = _connector()
    bal = {a.asset: (a.atoms, a.decimals) for a in conn.fetch_balances().assets}
    assert bal["BTC"] == (10_111_908_877_900, 10)  # 1011.1908877900 at 10 dp
    assert bal["USD"] == (1_712_886_158, 4)  # 171288.6158 at 4 dp
    assert bal["ETH"] == (8_185_500_000_000, 10)
    assert bal["EUR"] == (5_048_618_946, 4)
    assert bal["USDT"] == (50_000_000_000_000, 8)
    assert bal["ETH2.S"] == (1_983_970_800_000, 10)
    assert bal["ETH2"] == (25_885_574_330, 10)
    assert bal["USD.M"] == (12_130_292_780, 4)
    # M7 cash: 171288.6158 USD at 4 dp -> x100 -> 171_288_615_800 micros.
    assert conn.fetch_balance().balance_micros == 171_288_615_800


@respx.mock
def test_check_connection_accepts_an_account_with_no_usd():
    # A new or EUR-only account: Kraken's Balance simply has no ZUSD entry.
    # That is a valid key with 0 USD, not a failed authentication.
    _mock(FakeKraken(balance={"ZEUR": "150.0000", "XXBT": "0.0100000000"}))
    bal = _connector().check_connection()
    assert bal.balance_micros == 0
    assert bal.as_of == datetime.fromtimestamp(NOW, UTC)


@respx.mock
def test_check_connection_reports_usd_in_micros():
    # 171288.6158 USD at 4 dp = 1_712_886_158 atoms; x 10**(6-4) = 171_288_615_800 micros.
    _mock(FakeKraken(balance=_fixture("docs_balance.json")["result"]))
    assert _connector().check_connection().balance_micros == 171_288_615_800


@respx.mock
def test_pasted_credentials_with_whitespace_still_sign_correctly():
    # FakeKraken checks API-Key == KEY and API-Sign against the clean SECRET.
    _mock(FakeKraken())
    wrapped = SECRET[:44] + "\n" + SECRET[44:] + "\n"
    conn = KrakenConnector(
        f"  {KEY}\n", wrapped, nonces=NonceCounter(clock=lambda: 1_000), clock=lambda: NOW
    )
    assert conn.fetch_balances().assets


# ---- fills ----------------------------------------------------------------------


@respx.mock
def test_fills_use_ledger_amounts_and_running_balances():
    fake = FakeKraken()
    _mock(fake)
    fills = _connector().fetch_asset_fills()
    assert [f.venue_fill_id for f in fills] == [
        "T1BUYB-AAAAA-AAAAAA",
        "T2BUYE-AAAAA-AAAAAA",
        "T3SELL-AAAAA-AAAAAA",
    ]
    t1, t2, t3 = fills
    assert t1.action is Action.BUY and t1.order_id == "O1BUYB-AAAAA-AAAAAA"
    assert t1.instrument.symbol == "XXBTZUSD"
    assert (t1.instrument.base_asset, t1.instrument.quote_asset) == ("BTC", "USD")
    assert t1.instrument.qty_decimals == 8
    assert t1.qty_atoms == 2_000_000  # vol 0.02000000 at 8 dp
    assert t1.price == "30010.00000"
    assert t1.liquidity == "taker"
    assert t1.filled_at == datetime(2026, 8, 29, 10, 41, 40, 500_000, tzinfo=UTC)
    legs = _by_kind(t1.legs)
    # Ledger amounts, not trade fields: BTC at 10 dp, USD at 4 dp.
    base = legs[(LegKind.FILL_BASE, "BTC")]
    assert (base.delta_atoms, base.decimals, base.balance_after_atoms) == (
        200_000_000,
        10,
        200_000_000,
    )
    quote = legs[(LegKind.FILL_QUOTE, "USD")]
    assert (quote.delta_atoms, quote.decimals, quote.balance_after_atoms) == (-6_002_000, 4, None)
    fee = legs[(LegKind.FEE, "USD")]
    # Ledger fee 1.5605 (trade says 1.56052: 5 dp cost precision). Running
    # balance after both USD legs: 2000.0000 - 600.2000 - 1.5605 = 1398.2395.
    assert (fee.delta_atoms, fee.balance_after_atoms) == (-15_605, 13_982_395)
    assert all(leg.instrument_symbol == "XXBTZUSD" for leg in t1.legs)

    legs = _by_kind(t2.legs)
    assert legs[(LegKind.FILL_BASE, "ETH")].delta_atoms == 5_000_000_000  # 0.5 ETH at 10 dp
    assert legs[(LegKind.FILL_QUOTE, "USD")].delta_atoms == -10_000_000
    # 1398.2395 - 1000.0000 - 2.6000 = 395.6395
    assert legs[(LegKind.FEE, "USD")].balance_after_atoms == 3_956_395

    assert t3.action is Action.SELL and t3.liquidity == "maker"
    legs = _by_kind(t3.legs)
    assert legs[(LegKind.FILL_BASE, "BTC")].delta_atoms == -100_000_000
    assert legs[(LegKind.FILL_BASE, "BTC")].balance_after_atoms == 100_000_000
    assert legs[(LegKind.FILL_QUOTE, "USD")].delta_atoms == 3_100_000
    # 395.6395 + 310.0000 - 0.8060 = 704.8335
    assert legs[(LegKind.FEE, "USD")].balance_after_atoms == 7_048_335

    # One Ledgers(type=trade) and one TradesHistory request, window pinned.
    (ledgers,) = fake.of("Ledgers")
    assert ledgers["type"] == "trade" and ledgers["end"] == "1790000000" and ledgers["ofs"] == "0"
    assert "start" not in ledgers
    (th,) = fake.of("TradesHistory")
    assert th["ledgers"] == "true" and th["consolidate_taker"] == "false"
    # floor(1788000100.5) - 2 = 1788000098; ceil(1788090000.75) + 1 = 1788090002
    assert (th["start"], th["end"]) == ("1788000098", "1788090002")


@respx.mock
def test_fill_pages_are_one_request_each_and_resume():
    fake = FakeKraken()
    _mock(fake)
    conn = _connector()
    first = conn.fetch_asset_fills_page()
    assert first.items == [] and first.next_cursor is not None and first.resume_cursor is None
    assert [m for m, _ in fake.calls] == ["Ledgers"]
    second = conn.fetch_asset_fills_page(first.next_cursor)
    assert [m for m, _ in fake.calls] == ["Ledgers", "TradesHistory"]
    assert len(second.items) == 3 and second.next_cursor is None
    # Next pass starts one second before the newest trade row: floor(1788090000.75) - 1.
    assert json.loads(second.resume_cursor) == {"v": 1, "s": 1788089999}
    again = conn.fetch_asset_fills_page(second.resume_cursor)
    assert fake.of("Ledgers")[-1]["start"] == "1788089999"
    follow = conn.fetch_asset_fills_page(again.next_cursor)
    assert [f.venue_fill_id for f in follow.items] == [
        "T3SELL-AAAAA-AAAAAA"
    ]  # overlap, deduped by id


@respx.mock
def test_since_sets_an_exclusive_start_one_second_early():
    fake = FakeKraken()
    _mock(fake)
    since = datetime(2026, 8, 29, 11, 0, tzinfo=UTC)  # 1788001200
    fills = _connector().fetch_asset_fills(since=since)
    assert fake.of("Ledgers")[0]["start"] == "1788001199"
    assert [f.venue_fill_id for f in fills] == ["T3SELL-AAAAA-AAAAAA"]


def _big_account():
    """1 deposit + 30 BTC buys, 0.001 BTC each at 30000 + i USD, fee 0.0780 USD,
    except trade 0 whose fee is paid in KFEE (a third, fee-only ledger row).
    61 trade rows, so the first full page ends in the middle of trade 24."""
    ledger, trades = {}, {}
    usd, btc, kfee = Decimal("10000.0000"), Decimal("0"), Decimal("100.00")
    ledger["LDEP"] = dict(
        refid="D",
        time=1788000000,
        type="deposit",
        subtype="",
        aclass="currency",
        asset="ZUSD",
        amount="10000.0000",
        fee="0.0000",
        balance=f"{usd:.4f}",
    )
    for i in range(30):
        t = 1788000010 + 10 * i + 0.5
        cost = Decimal(30000 + i) * Decimal("0.001")
        fee = Decimal("0") if i == 0 else Decimal("0.0780")
        btc += Decimal("0.001")
        usd -= cost + fee
        txid = f"T{i:02d}"
        ids = [f"L{i:02d}B", f"L{i:02d}Q"]
        ledger[ids[0]] = dict(
            refid=txid,
            time=t,
            type="trade",
            subtype="",
            aclass="currency",
            asset="XXBT",
            amount="0.0010000000",
            fee="0.0000000000",
            balance=f"{btc:.10f}",
        )
        ledger[ids[1]] = dict(
            refid=txid,
            time=t,
            type="trade",
            subtype="",
            aclass="currency",
            asset="ZUSD",
            amount=f"{-cost:.4f}",
            fee=f"{fee:.4f}",
            balance=f"{usd:.4f}",
        )
        if i == 0:
            kfee -= Decimal("7.80")
            ids.append("L00K")
            ledger["L00K"] = dict(
                refid=txid,
                time=t,
                type="trade",
                subtype="",
                aclass="currency",
                asset="KFEE",
                amount="0.00",
                fee="7.80",
                balance=f"{kfee:.2f}",
            )
        trades[txid] = dict(
            ordertxid=f"O{i:02d}",
            pair="XXBTZUSD",
            time=t,
            type="buy",
            ordertype="market",
            price=f"{30000 + i}.00000",
            cost=f"{cost:.5f}",
            fee="0.07800",
            vol="0.00100000",
            margin="0.00000",
            misc="",
            maker=False,
            ledgers=ids,
        )
    return ledger, trades


@respx.mock
def test_fill_backfill_walks_oldest_page_first_and_keeps_trades_whole():
    ledger, trades = _big_account()
    fake = FakeKraken(ledger=ledger, trades=trades)
    _mock(fake)
    conn = _connector()
    pages, cursor = [], None
    while True:
        page = conn.fetch_asset_fills_page(cursor)
        pages.append(page)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    # probe (61 > 50 rows) | oldest 50 minus trade 24's first row | its trades |
    # newest 12 rows (final) | their trades.
    assert [len(p.items) for p in pages] == [0, 0, 24, 0, 6]
    assert [p["ofs"] for p in fake.of("Ledgers")] == ["0", "11", "0"]
    assert len(fake.calls) == 5
    fills = [f for p in pages for f in p.items]
    assert [f.venue_fill_id for f in fills] == [f"T{i:02d}" for i in range(30)]
    t0 = _by_kind(fills[0].legs)
    assert t0[(LegKind.FEE, "KFEE")].delta_atoms == -780  # 7.80 KFEE at 2 dp
    assert t0[(LegKind.FEE, "KFEE")].balance_after_atoms == 9_220  # 100.00 - 7.80
    assert (LegKind.FEE, "USD") not in t0
    last = _by_kind(fills[-1].legs)
    # 30 x 0.001 BTC = 0.0300000000 at 10 dp.
    assert last[(LegKind.FILL_BASE, "BTC")].balance_after_atoms == 300_000_000
    # USD: 10000 - sum(30 + 0.001 i, i<30) - 29 x 0.0780
    #    = 10000 - (900 + 0.435) - 2.2620 = 9097.3030 -> 90_973_030 at 4 dp.
    assert last[(LegKind.FEE, "USD")].balance_after_atoms == 90_973_030


@respx.mock
def test_window_count_change_restarts_the_walk_then_gives_up():
    ledger, trades = _big_account()
    fake = FakeKraken(ledger=ledger, trades=trades)
    _mock(fake)
    conn = _connector()
    probe = conn.fetch_asset_fills_page()
    # A row appears inside the pinned window (Kraken back-fills history).
    ledger_row = dict(ledger["L05B"], refid="T05", time=1788000061.0)
    fake.ledger["LLATE"] = ledger_row
    restarted = conn.fetch_asset_fills_page(probe.next_cursor)
    assert restarted.items == []
    state = json.loads(restarted.next_cursor)["w"]
    assert (state["n"], state["k"], state["r"]) == (None, 0, 1)

    counter = iter(range(100, 200))
    fake.overrides["Ledgers"] = lambda p: {
        "error": [],
        "result": {"ledger": {}, "count": next(counter)},
    }
    cursor = probe.next_cursor
    with pytest.raises(KrakenError, match="count keeps changing"):
        for _ in range(10):
            cursor = conn.fetch_asset_fills_page(cursor).next_cursor


@respx.mock
def test_trade_rows_without_a_trade_fail_loudly():
    trades = _fixture("account_trades.json")["result"]["trades"]
    del trades["T2BUYE-AAAAA-AAAAAA"]
    _mock(FakeKraken(trades=trades))
    with pytest.raises(KrakenError, match="T2BUYE"):
        _connector().fetch_asset_fills()


def test_fee_in_base_and_docs_trade_example(caplog):
    trade = _fixture("docs_trades_history.json")["result"]["trades"]["THVRQM-33VKH-UCI7BS"]
    rows = [
        LedgerRow(
            "LA",
            "THVRQM-33VKH-UCI7BS",
            "1688667796.8802",
            "trade",
            "",
            "XXBT",
            "0.0200000000",
            "0.0000200000",
            "0.0199800000",
        ),
        LedgerRow(
            "LB",
            "THVRQM-33VKH-UCI7BS",
            "1688667796.8802",
            "trade",
            "",
            "ZUSD",
            "-600.2000",
            "0.0000",
            "399.8000",
        ),
    ]
    with caplog.at_level(logging.WARNING):
        fill = trade_fill("THVRQM-33VKH-UCI7BS", trade, rows)
    assert caplog.records == []  # fee charged in BTC: the USD fee check is skipped
    legs = _by_kind(fill.legs)
    assert fill.liquidity == "maker"
    assert legs[(LegKind.FILL_BASE, "BTC")].delta_atoms == 200_000_000
    assert legs[(LegKind.FILL_BASE, "BTC")].balance_after_atoms is None
    # 0.0000200000 BTC at 10 dp; balance 0.02 - 0.00002 = 0.01998
    assert legs[(LegKind.FEE, "BTC")].delta_atoms == -200_000
    assert legs[(LegKind.FEE, "BTC")].balance_after_atoms == 199_800_000
    assert legs[(LegKind.FILL_QUOTE, "USD")].balance_after_atoms == 3_998_000
    assert (LegKind.FEE, "USD") not in legs  # zero fee rows add no leg


def test_crosscheck_logs_disagreement_but_ledger_wins(caplog):
    trade = dict(_fixture("account_trades.json")["result"]["trades"]["T1BUYB-AAAAA-AAAAAA"])
    trade["cost"] = "601.20000"  # off by 1.0000 USD, far beyond one 4-dp atom
    rows = [
        LedgerRow.parse(i, r)
        for i, r in _fixture("account_ledgers.json")["result"]["ledger"].items()
        if r["refid"] == "T1BUYB-AAAAA-AAAAAA"
    ]
    with caplog.at_level(logging.WARNING):
        fill = trade_fill("T1BUYB-AAAAA-AAAAAA", trade, rows)
    assert "quote amount != cost" in caplog.text
    assert _by_kind(fill.legs)[(LegKind.FILL_QUOTE, "USD")].delta_atoms == -6_002_000


# ---- events ---------------------------------------------------------------------


@respx.mock
def test_events_map_non_trade_ledger_rows():
    fake = FakeKraken()
    _mock(fake)
    page = _connector().fetch_events_page()
    assert page.next_cursor is None
    assert json.loads(page.resume_cursor) == {"v": 1, "s": 1788099999}
    assert fake.of("Ledgers")[0]["type"] == "all"
    events = page.items
    assert [(e.venue_ref, e.kind) for e in events] == [
        ("L01DEP-AAAAA-AAAAAA", LegKind.DEPOSIT),
        ("L06SOT-AAAAA-AAAAAA", LegKind.CONVERT),  # spottostaking
        ("L07SIN-AAAAA-AAAAAA", LegKind.CONVERT),  # stakingfromspot
        ("L08STK-AAAAA-AAAAAA", LegKind.STAKING),
        ("L11WDR-AAAAA-AAAAAA", LegKind.WITHDRAWAL),
    ]
    dep, out, into, reward, wdr = events
    (leg,) = dep.legs
    assert (leg.asset, leg.delta_atoms, leg.decimals, leg.balance_after_atoms) == (
        "USD",
        20_000_000,
        4,
        20_000_000,
    )  # 2000.0000 at 4 dp
    (leg,) = out.legs
    assert (leg.asset, leg.delta_atoms, leg.balance_after_atoms) == (
        "ETH",
        -2_500_000_000,
        2_500_000_000,
    )
    (leg,) = into.legs
    assert (leg.asset, leg.delta_atoms) == ("ETH2.S", 2_500_000_000)
    (leg,) = reward.legs
    # 0.0001250000 ETH2.S at 10 dp; balance 0.2501250000
    assert (leg.asset, leg.delta_atoms, leg.balance_after_atoms) == (
        "ETH2.S",
        1_250_000,
        2_501_250_000,
    )
    legs = _by_kind(wdr.legs)
    assert legs[(LegKind.WITHDRAWAL, "USD")].delta_atoms == -1_000_000
    assert legs[(LegKind.WITHDRAWAL, "USD")].balance_after_atoms is None
    # 704.8335 - 100.0000 - 0.3500 = 604.4835
    assert legs[(LegKind.FEE, "USD")].delta_atoms == -3_500
    assert legs[(LegKind.FEE, "USD")].balance_after_atoms == 6_044_835
    assert "withdrawal" in wdr.description and "FTWDRAW-00000-000011" in wdr.description
    assert dep.occurred_at == datetime(2026, 8, 29, 10, 40, 0, 100_000, tzinfo=UTC)


@respx.mock
def test_replayed_legs_equal_the_balance_vector():
    _mock(FakeKraken())
    conn = _connector()
    held: dict[str, int] = {}
    for leg in [leg for f in conn.fetch_asset_fills() for leg in f.legs] + [
        leg for e in conn.fetch_events() for leg in e.legs
    ]:
        held[leg.asset] = held.get(leg.asset, 0) + leg.delta_atoms  # one scale per asset here
    venue = {a.asset: a.atoms for a in conn.fetch_balances().assets if a.atoms}
    assert (
        held
        == venue
        == {
            "USD": 6_044_835,  # 604.4835
            "BTC": 100_000_000,  # 0.0100000000
            "ETH": 2_500_000_000,
            "ETH2.S": 2_501_250_000,
        }
    )


@respx.mock
def test_event_backfill_pages_oldest_first():
    ledger = {
        f"L{i:03d}": dict(
            refid=f"D{i}",
            time=1788000000 + i,
            type="deposit",
            subtype="",
            aclass="currency",
            asset="ZUSD",
            amount="1.0000",
            fee="0.0000",
            balance=f"{i + 1}.0000",
        )
        for i in range(55)
    }
    fake = FakeKraken(ledger=ledger)
    _mock(fake)
    conn = _connector()
    p1 = conn.fetch_events_page()
    p2 = conn.fetch_events_page(p1.next_cursor)
    p3 = conn.fetch_events_page(p2.next_cursor)
    assert (len(p1.items), len(p2.items), len(p3.items)) == (0, 50, 5)
    assert p3.next_cursor is None and p3.resume_cursor is not None
    assert [p["ofs"] for p in fake.of("Ledgers")] == ["0", "5", "0"]
    refs = [e.venue_ref for e in p2.items + p3.items]
    assert refs == [f"L{i:03d}" for i in range(55)]
    assert p3.items[-1].legs[0].balance_after_atoms == 550_000  # 55.0000


@pytest.mark.parametrize(
    ("type_", "subtype", "amount", "fee", "kind"),
    [
        ("earn", "reward", "0.0100", "0.0000", LegKind.STAKING),
        ("earn", "allocation", "-1.0000", "0.0000", LegKind.CONVERT),
        ("earn", "autoallocate", "1.0000", "0.0000", LegKind.CONVERT),
        ("transfer", "spotfromfutures", "5.0000", "0.0000", LegKind.TRANSFER_IN),
        ("transfer", "spottofutures", "-5.0000", "0.0000", LegKind.TRANSFER_OUT),
        ("transfer", "", "3.0000", "0.0000", LegKind.TRANSFER_IN),  # airdrop
        ("spend", "", "-10.0000", "0.1000", LegKind.CONVERT),
        ("receive", "", "0.5000", "0.0000", LegKind.CONVERT),
        ("adjustment", "", "-0.0001", "0.0000", LegKind.ADJUSTMENT),
        ("dividend", "", "0.2000", "0.0000", LegKind.DIVIDEND),
        ("margin", "", "-2.0000", "0.1000", LegKind.REALIZED_PNL),
        ("invite bonus", "", "10.0000", "0.0000", LegKind.ADJUSTMENT),
    ],
)
def test_event_kind_mapping(type_, subtype, amount, fee, kind):
    row = LedgerRow("LX", "R", "1788000000", type_, subtype, "ZUSD", amount, fee, "100.0000")
    event = ledger_event(row)
    assert event.kind is kind
    assert event.legs[0].kind is kind and event.legs[0].delta_atoms == int(Decimal(amount) * 10_000)


def test_rollover_is_one_interest_leg_net_of_fee():
    # Margin rollover: amount 0.0000, fee 0.1234 -> interest 0 - 0.1234 = -1_234 atoms.
    row = LedgerRow("LR", "R", "1788000000", "rollover", "", "ZUSD", "0.0000", "0.1234", "99.8766")
    (leg,) = ledger_event(row).legs
    assert (leg.kind, leg.delta_atoms, leg.balance_after_atoms) == (
        LegKind.INTEREST,
        -1_234,
        998_766,
    )


def test_docs_ledger_example_rows_parse():
    raw = _fixture("docs_ledgers.json")["result"]["ledger"]
    row = LedgerRow.parse("L4UESK-KG3EQ-UFO4T5", raw["L4UESK-KG3EQ-UFO4T5"])
    assert (row.code, row.decimals, row.atoms(row.amount), row.atoms(row.fee)) == (
        "GBP",
        4,
        -245_000,
        490,
    )


# ---- errors and rate limits ----------------------------------------------------


@respx.mock
def test_http_429_raises_rate_limited_with_retry_after():
    respx.post(f"{API}/0/private/Balance").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "7"})
    )
    with pytest.raises(RateLimited) as exc:
        _connector().fetch_balances()
    assert exc.value.retry_after_seconds == 7.0 and exc.value.endpoint == "Balance"


@respx.mock
def test_in_body_rate_limit_errors_raise_rate_limited():
    fake = FakeKraken(overrides={"Ledgers": {"error": ["EAPI:Rate limit exceeded"]}})
    _mock(fake)
    with pytest.raises(RateLimited) as exc:
        _connector().fetch_events_page()
    assert exc.value.retry_after_seconds == 12.0 and exc.value.endpoint == "Ledgers"

    fake.overrides["Ledgers"] = {"error": ["EService:Throttled: 1790000030"]}
    with pytest.raises(RateLimited) as exc:
        _connector().fetch_events_page()
    assert exc.value.retry_after_seconds >= 1.0


@respx.mock
def test_md_418_is_rate_limited_too():
    respx.get(f"{API}/0/public/Ticker").mock(return_value=httpx.Response(418))
    with pytest.raises(RateLimited):
        KrakenMarketData().fetch_quote("XXBTZUSD")


@respx.mock
def test_invalid_key_raises_auth_error():
    _mock(FakeKraken(overrides={"Balance": {"error": ["EAPI:Invalid key"]}}))
    with pytest.raises(KrakenAuthError):
        _connector().check_connection()


class _Recorder:
    def __init__(self):
        self.seen = []

    def before(self, endpoint):
        self.seen.append(endpoint)

    def after(self, endpoint, extra_cost=0, used=None):
        pass


@respx.mock
def test_throttle_is_charged_before_every_request():
    fake = FakeKraken(
        overrides={
            "AddOrder": {"error": ["EGeneral:Permission denied"]},
            "WithdrawMethods": {"error": ["EGeneral:Permission denied"]},
        }
    )
    _mock(fake)
    conn = _connector()
    rec = _Recorder()
    conn.set_throttle(rec)
    conn.fetch_asset_fills()
    conn.prove_read_only()
    assert rec.seen == [
        "Ledgers",
        "TradesHistory",
        "Balance",
        "TradesHistory",
        "Ledgers",
        "AddOrder",
        "WithdrawMethods",
    ]
    assert [m for m, _ in fake.calls] == rec.seen
    assert fake.nonces == sorted(set(fake.nonces))  # strictly increasing


# ---- read-only proof -------------------------------------------------------------

DENIED = {"error": ["EGeneral:Permission denied"]}


@respx.mock
def test_prove_read_only_confirms_a_query_only_key():
    fake = FakeKraken(overrides={"AddOrder": DENIED, "WithdrawMethods": DENIED})
    _mock(fake)
    conn = _connector()
    ev = conn.prove_read_only()
    assert ev.method is ReadOnlyProof.ORDER_PROBE and ev.read_only is True
    assert ev.details == {
        "balance": "ok",
        "trades_history": "ok",
        "ledgers": "ok",
        "add_order_validate": "EGeneral:Permission denied",
        "withdraw_methods": "EGeneral:Permission denied",
        "can_trade": False,
        "can_withdraw": False,
        "reads_ok": True,
    }
    (order,) = fake.of("AddOrder")
    assert order["validate"] == "true"  # never reaches the matching engine
    assert (order["pair"], order["ordertype"], order["price"]) == ("XBTUSD", "limit", "1.0")
    assert conn.check_write_scope() is False
    assert SECRET not in json.dumps(ev.details)


@respx.mock
def test_prove_read_only_rejects_trade_or_withdraw_capable_keys():
    validated = {"error": [], "result": {"descr": {"order": "buy 0.00010000 XBTUSD @ limit 1.0"}}}
    _mock(FakeKraken(overrides={"AddOrder": validated, "WithdrawMethods": DENIED}))
    ev = _connector().prove_read_only()
    assert ev.read_only is False and ev.details["can_trade"] is True
    assert _connector().check_write_scope() is True

    respx.reset()
    _mock(
        FakeKraken(
            overrides={
                "AddOrder": DENIED,
                "WithdrawMethods": _fixture("docs_withdraw_methods.json"),
            }
        )
    )
    ev = _connector().prove_read_only()
    assert ev.read_only is False and ev.details["can_withdraw"] is True

    respx.reset()
    # Any non-permission answer means the key got past the permission check.
    _mock(
        FakeKraken(
            overrides={
                "AddOrder": {"error": ["EOrder:Insufficient funds"]},
                "WithdrawMethods": DENIED,
            }
        )
    )
    assert _connector().prove_read_only().read_only is False


@respx.mock
def test_prove_read_only_is_undetermined_when_reads_are_missing():
    _mock(FakeKraken(overrides={"AddOrder": DENIED, "WithdrawMethods": DENIED, "Ledgers": DENIED}))
    ev = _connector().prove_read_only()
    assert ev.read_only is None and ev.details["reads_ok"] is False
    assert ev.details["ledgers"] == "EGeneral:Permission denied"
    assert _connector().check_write_scope() is None


# ---- market data (real public payloads recorded 2026-09-26) ------------------------


@respx.mock
def test_instruments_from_recorded_asset_pairs():
    respx.get(f"{API}/0/public/AssetPairs").mock(
        return_value=httpx.Response(200, json=_fixture("asset_pairs.json"))
    )
    md = KrakenMarketData()
    instruments = {i.symbol: i for i in md.fetch_instruments()}
    assert set(instruments) == {"SOLUSD", "USDCUSD", "XETHXXBT", "XETHZUSD", "XXBTZUSD"}
    btc = instruments["XXBTZUSD"]
    assert (btc.base_asset, btc.quote_asset, btc.qty_decimals) == ("BTC", "USD", 8)
    assert btc.min_qty_atoms == 5_000  # ordermin 0.00005 at 8 dp
    assert btc.min_notional == "0.5"
    assert (instruments["XETHXXBT"].base_asset, instruments["XETHXXBT"].quote_asset) == (
        "ETH",
        "BTC",
    )
    assert instruments["SOLUSD"].min_qty_atoms == 6_000_000  # 0.06 SOL
    listed = list(instruments.values())
    assert md.pricing_symbol("ETH", listed) == "XETHZUSD"
    assert md.pricing_symbol("ETH2.S", listed) == "XETHZUSD"
    assert md.pricing_symbol("BTC.M", listed) == "XXBTZUSD"
    assert md.pricing_symbol("USD.HOLD", listed) is None
    assert md.pricing_symbol("USDC", listed) is None
    assert md.pricing_symbol("DOT.S", listed) is None  # not listed here -> CoinGecko fallback


@respx.mock
def test_assets_decimals_from_recorded_assets():
    respx.get(f"{API}/0/public/Assets").mock(
        return_value=httpx.Response(200, json=_fixture("assets.json"))
    )
    decimals = KrakenMarketData().fetch_assets()
    assert decimals["BTC"] == 10 and decimals["USD"] == 4 and decimals["USDC"] == 8
    assert decimals["ETH2.S"] == 10 and decimals["BTC.M"] == 10 and decimals["KFEE"] == 2


@respx.mock
def test_daily_closes_skip_the_running_day():
    route = respx.get(f"{API}/0/public/OHLC").mock(
        return_value=httpx.Response(200, json=_fixture("ohlc_xxbtzusd_1440.json"))
    )
    closes = KrakenMarketData().fetch_daily_closes("XXBTZUSD", date(2026, 9, 23), date(2026, 9, 30))
    # Bars open 2026-09-22..26; `last` = 1790294400 (09-25) so 09-26 is still open.
    assert [(c.day, c.close) for c in closes] == [
        (date(2026, 9, 23), "84384.6"),
        (date(2026, 9, 24), "84380.0"),
        (date(2026, 9, 25), "84090.5"),
    ]
    params = dict(route.calls.last.request.url.params)
    # 2026-09-23T00:00Z = 1790121600; since is exclusive so one second earlier.
    assert params == {"pair": "XXBTZUSD", "interval": "1440", "since": "1790121599"}


@respx.mock
def test_quote_from_recorded_ticker_and_unknown_pair():
    respx.get(f"{API}/0/public/Ticker", params={"pair": "XXBTZUSD"}).mock(
        return_value=httpx.Response(200, json=_fixture("ticker_xxbtzusd.json"))
    )
    respx.get(f"{API}/0/public/Ticker", params={"pair": "NOPEUSD"}).mock(
        return_value=httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"]})
    )
    md = KrakenMarketData()
    q = md.fetch_quote("XXBTZUSD")
    assert (q.bid, q.ask, q.last) == ("84261.30000", "84261.40000", "84261.60000")
    assert md.fetch_quote("NOPEUSD") is None


# ---- registry ------------------------------------------------------------------------


def test_registry_profile_and_builders():
    profile = get_profile("kraken")
    assert profile.instrument is InstrumentKind.ASSET
    assert profile.readonly_proof is ReadOnlyProof.ORDER_PROBE
    assert profile.paper_fee_model is PaperFeeModel.BPS_OF_NOTIONAL
    assert profile.tos_doc == "docs/venues/kraken.md"
    assert (Path(__file__).parents[1] / profile.tos_doc).exists()
    assert profile.requires_partner_approval  # docs/venues/kraken.md: needs_approval
    assert profile.transfers_observable and profile.balance_observable
    scopes = {b.scope: b for b in profile.rate_budgets}
    cred = scopes[RateScope.CREDENTIAL]
    assert (cred.capacity, cred.refill_per_minute) == (15, 20)
    assert cred.cost("Ledgers") == cred.cost("TradesHistory") == 4
    assert cred.cost("Balance") == 1
    assert scopes[RateScope.IP].cost("OHLC") == 1
    conn = build_connector("kraken", "prod", KEY, SECRET)
    assert isinstance(conn, KrakenConnector) and conn.venue == "kraken"
    with pytest.raises(ValueError):
        build_connector("kraken", "prod", KEY, None)
    assert isinstance(build_market_data("kraken", "prod"), KrakenMarketData)
