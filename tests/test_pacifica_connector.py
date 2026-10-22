"""Pacifica connector against public payloads captured 2026-10-22
(tests/fixtures/pacifica/, provenance in its README) and docs examples, never
live calls (invariant 6). Expected atoms are hand-computed in comments."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from alphamarket import reconcile
from alphamarket.connectors import pacifica as pf
from alphamarket.connectors.addresses import b58encode
from alphamarket.connectors.base import (
    Action,
    InstrumentType,
    LegKind,
    MarginTier,
    RateLimited,
    RateScope,
    ReadOnlyProof,
    StreamProtocolError,
)
from alphamarket.connectors.pacifica import (
    AccountNotFound,
    PacificaConnector,
    PacificaFillStream,
    PacificaMarketData,
    claim_message,
    encode_claim_proof,
    stream_row_to_rest,
)
from alphamarket.demo import synthetic_solana_address
from alphamarket.ledger import balance_vector
from alphamarket.money import from_atoms

FIXTURES = Path(__file__).parent / "fixtures" / "pacifica"
API = "https://api.pacifica.fi/api/v1"
ADDRESS = synthetic_solana_address("pacifica-test00")
M = 1_000_000


def fx(name: str):
    return json.loads((FIXTURES / f"{name}_2026-10-22.json").read_text())


def ms(year, month, day, hour=0, minute=0, second=0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=UTC).timestamp() * 1000)


class FakeApi:
    """respx side effect for every GET on the API host: answers by path from a
    queue (the last answer repeats), records every request."""

    def __init__(self, **routes):
        self.routes: dict[str, list] = {"/info": [fx("info")]}
        for path, answers in routes.items():
            self.routes["/" + path.replace("__", "/")] = (
                answers if isinstance(answers, list) else [answers]
            )
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v1")
        queue = self.routes[path]
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def paths(self) -> list[str]:
        return [r.url.path.removeprefix("/api/v1") for r in self.requests]

    def params(self, path: str) -> list[dict[str, str]]:
        return [dict(r.url.params) for r in self.requests if r.url.path.endswith(path)]


def mock(**routes) -> FakeApi:
    fake = FakeApi(**routes)
    respx.route(host="api.pacifica.fi").mock(side_effect=fake)
    return fake


def legs(obj) -> dict[tuple[str, str], tuple[int, int]]:
    return {(leg.kind.value, leg.asset): (leg.delta_atoms, leg.decimals) for leg in obj.legs}


def connector(address: str = ADDRESS) -> PacificaConnector:
    return PacificaConnector(address)


def page_of(rows, *, more=False, cursor="1111next"):
    return {
        "success": True,
        "data": rows,
        "next_cursor": cursor if more else None,
        "has_more": more,
    }


# ---- rate budget and transport ------------------------------------------------------


def test_rate_budget_matches_the_measured_costs():
    budget = pf.RATE_BUDGET
    assert budget.scope is RateScope.IP
    assert (budget.capacity, budget.refill_per_minute) == (100, 100)
    # Measured 2026-10-22 (docs/venues/pacifica.md "Rate limits"): a history page
    # and the leaderboard cost 9 credits whatever the limit, charged rounded up.
    for endpoint in ("leaderboard", "trades_history", "funding_history", "balance_history"):
        assert budget.cost(endpoint) == 10
    assert budget.cost("funding_rate_history") == 10
    assert budget.cost("kline") == 3
    for endpoint in ("account", "positions", "info", "prices", "book"):
        assert budget.cost(endpoint) == 1
    # a full backfill page of the leaderboard plus two history pages fits one window
    assert budget.capacity >= 10 + 10 + 10


class Recorder:
    def __init__(self):
        self.before_calls: list[str] = []

    def before(self, endpoint: str) -> None:
        self.before_calls.append(endpoint)

    def after(self, endpoint: str, extra_cost: int = 0, used: int | None = None) -> None:
        pass


@respx.mock
def test_a_get_sends_no_body_and_no_content_length_and_charges_the_throttle():
    fake = mock(trades__history=page_of(fx("trades_history_small")["data"]))
    conn = connector()
    rec = Recorder()
    conn.set_throttle(rec)
    conn.fetch_asset_fills_page()
    for request in fake.requests:  # CloudFront answers 403 to a GET with a body
        assert request.method == "GET"
        assert request.content == b""
        assert "content-length" not in request.headers
    # the history page is charged before it is sent, then /info once for lots
    assert rec.before_calls == ["trades_history", "info"]
    assert fake.params("/trades/history") == [{"account": ADDRESS, "limit": str(pf.PAGE_LIMIT)}]


@respx.mock
def test_429_waits_the_ratelimit_header_t_or_retry_after():
    mock(
        trades__history=httpx.Response(
            429, json={"success": False}, headers={"ratelimit": '"credits";r=0;t=42'}
        )
    )
    with pytest.raises(RateLimited) as exc:
        connector().fetch_asset_fills_page()
    assert exc.value.retry_after_seconds == 42.0
    assert exc.value.endpoint == "trades_history"
    respx.reset()
    mock(
        trades__history=httpx.Response(
            429, json={}, headers={"Retry-After": "7", "ratelimit": '"credits";r=0;t=42'}
        )
    )
    with pytest.raises(RateLimited) as exc:
        connector().fetch_asset_fills_page()
    assert exc.value.retry_after_seconds == 7.0


@respx.mock
def test_a_403_fails_closed_without_a_retry_or_workaround():
    fake = mock(trades__history=httpx.Response(403, json={"success": False, "code": 403}))
    with pytest.raises(httpx.HTTPStatusError):
        connector().fetch_asset_fills_page()
    assert fake.paths() == ["/trades/history"]  # nothing else is tried


def test_only_a_valid_solana_address_builds_a_connector():
    with pytest.raises(ValueError):
        PacificaConnector("0x" + "ab" * 20)
    with pytest.raises(ValueError):
        PacificaConnector("short")
    assert PacificaConnector(f"  {ADDRESS}  ").address == ADDRESS  # case kept, trimmed


# ---- P1: fills ------------------------------------------------------------------------


@respx.mock
def test_close_short_pnl_is_net_of_the_fee_and_the_two_legs_sum_to_it():
    mock()
    row = fx("trades_history")["data"][0]  # SOL close_short 200 @ 103.66, entry 107.490396
    fill = connector().to_fill(row)
    # gross = (107.490396 - 103.66) x 200 = 3.830396 x 200 = 766.0792
    # fee   = 7.04888; gross - fee = 759.03032; the venue reports pnl 759.03031 (the
    # 1e-5 is its rounding of entry_price to 6 dp: 200 x 5e-7 = 1e-4 at most)
    # REALIZED_PNL = pnl + fee = 759.03031 + 7.04888 = 766.07919
    assert fill.venue_fill_id == "305692737"
    assert fill.order_id == "13847106762"
    assert fill.action is Action.BUY and fill.liquidity == "taker"
    assert fill.price == "103.66" and fill.qty_atoms == 20_000  # 200 at the lot's 2 dp
    assert legs(fill) == {
        ("fill_base", "SOL-PERP"): (20_000, 2),  # buying back a short: +200
        ("realized_pnl", "USDC"): (766_079_190, 6),  # 766.07919
        ("fee", "USDC"): (-7_048_880, 6),  # -7.04888
    }
    realized = legs(fill)[("realized_pnl", "USDC")][0]
    fee = legs(fill)[("fee", "USDC")][0]
    assert realized + fee == 759_030_310  # the venue's pnl 759.03031, exactly
    assert from_atoms(realized, 6) - (Decimal("107.490396") - Decimal("103.66")) * 200 == Decimal(
        "-0.00001"
    )
    assert fill.filled_at == datetime(2026, 10, 22, 17, 18, 20, 801000, tzinfo=UTC)
    inst = fill.instrument
    assert (inst.symbol, inst.base_asset, inst.quote_asset) == ("SOL", "SOL-PERP", "USDC")
    assert inst.type is InstrumentType.PERP
    assert (inst.qty_decimals, inst.qty_step_atoms, inst.min_notional) == (2, 1, "10")
    assert inst.copyable is True


@respx.mock
def test_an_open_fill_has_pnl_minus_fee_so_it_books_only_the_fee():
    mock()
    row = fx("trades_history")["data"][1]  # BTC open_short 0.00168 @ 81270
    fill = connector().to_fill(row)
    # pnl -0.046421 = -fee, so REALIZED_PNL = pnl + fee = 0 and no leg is written.
    # sell 0.00168 BTC at the lot's 5 dp: 168 atoms; fee 0.046421 -> -46_421 micros.
    # (0.00168 x 81270 = 136.5336 notional x 4 bps = 0.0546 would be the level-0
    # rate; the account pays its own tier: the connector books what the venue charged.)
    assert fill.action is Action.SELL and fill.qty_atoms == 168
    assert legs(fill) == {("fill_base", "BTC-PERP"): (-168, 5), ("fee", "USDC"): (-46_421, 6)}
    assert fill.instrument.qty_decimals == 5


@respx.mock
def test_every_recorded_row_books_to_the_venue_pnl_exactly():
    mock()
    conn = connector()
    rows = [
        *fx("trades_history")["data"],
        *fx("trades_history_small")["data"],
        *fx("trades_history_privacy_mode")["data"],
    ]
    assert len(rows) == 9
    for row in rows:
        fill = conn.to_fill(row)
        by_kind = {(k, a): v[0] for (k, a), v in legs(fill).items()}
        realized = by_kind.get(("realized_pnl", "USDC"), 0)
        fee = by_kind.get(("fee", "USDC"), 0)
        assert from_atoms(realized + fee, 6) == Decimal(row["pnl"]), row["history_id"]
        assert from_atoms(-fee, 6) == Decimal(row["fee"])
    assert conn.drain_unsupported() == []


@respx.mock
def test_a_maker_rebate_is_a_positive_fee_leg_and_the_arithmetic_holds():
    mock()
    frame = fx("ws_account_trades")["frames"][0]["data"][0]  # SNDK close_long maker, a real frame
    fill = connector(frame["u"]).to_fill(stream_row_to_rest(frame))
    # fee -0.025137 (a rebate), pnl -15.063983 (= gross - fee):
    # gross = (1252.8 - 1290.400598) x 0.4013 = -37.600598 x 0.4013 = -15.08912
    # (37.600598 x 0.4 = 15.0402392; x 0.0013 = 0.0488807774)
    # REALIZED_PNL = pnl + fee = -15.063983 - 0.025137 = -15.08912
    # FEE = -fee = +0.025137; the two sum to pnl: -15.08912 + 0.025137 = -15.063983
    assert fill.liquidity == "maker" and fill.action is Action.SELL
    assert legs(fill) == {
        ("fill_base", "SNDK-PERP"): (-4013, 4),  # 0.4013 at SNDK's lot 0.0001: 4 dp
        ("realized_pnl", "USDC"): (-15_089_120, 6),
        ("fee", "USDC"): (25_137, 6),
    }
    assert fill.instrument.copyable is False  # an rfq (real-world asset) market


@respx.mock
def test_maker_open_with_a_rebate_books_only_the_positive_fee():
    mock()
    frame = fx("ws_account_trades")["frames"][2]["data"][0]  # XMR open_long maker
    fill = connector(frame["u"]).to_fill(stream_row_to_rest(frame))
    # fee -0.01591, pnl +0.01591 = -fee: realized = 0.01591 - 0.01591 = 0
    assert legs(fill) == {
        ("fill_base", "XMR-PERP"): (52, 2),  # 0.52 at XMR's lot 0.01
        ("fee", "USDC"): (15_910, 6),
    }


@respx.mock
def test_documented_liquidation_and_settlement_causes_are_fills():
    mock()
    conn = connector()
    base = fx("trades_history")["data"][0]
    causes = ("market_liquidation", "backstop_liquidation", "insolvency_liquidation", "settlement")
    for n, cause in enumerate(causes):
        assert conn.to_fill({**base, "cause": cause, "history_id": 900 + n}) is not None, cause
    # a backstop or insolvency liquidation also forfeits the cash left in the account,
    # which the default balance history does not show: the fill books, that is flagged
    flags = conn.drain_unsupported()
    assert [(f.venue_ref, f.activity_type) for f in flags] == [
        ("forfeit:901", "backstop_liquidation_cash"),
        ("forfeit:902", "insolvency_liquidation_cash"),
    ]


@respx.mock
def test_recorded_liquidations_book_exactly_the_cash_the_balance_history_shows():
    mock()
    rows = fx("trades_history_liquidation")["data"]
    balance = fx("balance_history_liquidation")["data"]
    conn = connector()
    fills = {r["history_id"]: conn.to_fill(r) for r in rows}
    market = [r for r in rows if r["cause"] == "market_liquidation"]
    assert len(market) == 4
    trade_amounts = {b["amount"] for b in balance if b["event_type"] == "trade"}
    for row in market:
        fill = fills[row["history_id"]]
        cash = sum(leg.delta_atoms for leg in fill.legs if leg.asset == "USDC")
        assert from_atoms(cash, 6) == Decimal(row["pnl"])  # REALIZED + FEE = the venue's pnl
        assert row["pnl"] in trade_amounts  # and it is what hit `balance`
        assert fill.liquidity == "taker" and fill.order_id == "13567101909"
    # 299883211: close_long 0.03035 BTC at 80814 against entry 82285.808148, fee 0.981082
    #   gross = (80814 - 82285.808148) x 0.03035 = -44.6693772918
    #   REALIZED = pnl + fee = -63.064664 + 0.981082 = -62.083582
    #   so the venue's liquidation penalty is 62.083582 - 44.6693772918 = 17.4142047082,
    #   which is 0.71% of the 2452.7049 notional: it is inside `pnl`, and booked.
    assert legs(fills[299883211]) == {
        ("fill_base", "BTC-PERP"): (-3035, 5),
        ("realized_pnl", "USDC"): (-62_083_582, 6),
        ("fee", "USDC"): (-981_082, 6),
    }
    assert Decimal("-62.083582") - Decimal("-44.6693772918") == Decimal("-17.4142047082")
    exact = Decimal("0.0071") * Decimal("2452.7049")  # 17.41420479: within venue rounding
    assert abs(Decimal("17.4142047082") - exact) < Decimal("1e-6")
    # settlement (ADL): fee 0 and pnl = gross exactly: (90817 - 83824) x 0.00001 = 0.06993
    assert legs(fills[293899026]) == {
        ("fill_base", "BTC-PERP"): (1, 5),
        ("realized_pnl", "USDC"): (69_930, 6),
    }
    # backstop: the fill's pnl -125.72372 (gross (2397.9 - 2409.058283) x 11.2673 =
    # -125.7237220459, fee 0) is the first `backstop_liquidation` balance event; the second,
    # -146.762287, takes the balance to 0 and is in no fill: flagged as forfeited cash.
    assert legs(fills[277560070])[("realized_pnl", "USDC")] == (-125_723_720, 6)
    forfeit = {
        b["amount"]: b["balance"] for b in balance if b["event_type"] == "backstop_liquidation"
    }
    # (the venue stamps the two events out of chain order, so they are matched by amount)
    assert forfeit == {"-125.72372": "146.762287", "-146.762287": "0"}
    assert Decimal(forfeit["-125.72372"]) + Decimal("-146.762287") == 0  # the balance is emptied
    [flag] = conn.drain_unsupported()
    assert (flag.venue_ref, flag.activity_type) == (
        "forfeit:277560070",
        "backstop_liquidation_cash",
    )
    # a stream frame for the same fill flags nothing: the poller does
    assert (
        PacificaConnector(synthetic_solana_address("x")).to_fill(rows[-1], flag=False) is not None
    )


@respx.mock
def test_an_unknown_cause_is_flagged_never_booked_and_a_bad_vocabulary_raises():
    mock()
    conn = connector()
    base = fx("trades_history")["data"][0]
    for cause in ("game_settlement", "fulfill_rfq", "something_new", None):
        assert conn.to_fill({**base, "cause": cause, "history_id": 7}) is None
    [flag] = conn.drain_unsupported()  # one ref (fill:7): the repeats collapse
    assert (flag.venue_ref, flag.symbol, flag.reason) == ("fill:7", "SOL", "unsupported_type")
    assert conn.drain_unsupported() == []  # drained once
    with pytest.raises(ValueError, match="event_type"):
        conn.to_fill({**base, "event_type": "fulfill_rfq"})
    with pytest.raises(ValueError, match="side"):
        conn.to_fill({**base, "side": "liquidate"})


@respx.mock
def test_spot_fills_are_flagged_not_ledgered():
    mock()
    conn = connector()
    base = fx("trades_history")["data"][0]
    assert conn.to_fill({**base, "spot_fee": "0.01", "history_id": 8}) is None
    assert conn.to_fill({**base, "symbol": "SOL-USDC", "history_id": 9}) is None  # a spot market
    assert [f.venue_ref for f in conn.drain_unsupported()] == ["fill:8", "fill:9"]


@respx.mock
def test_a_symbol_info_no_longer_lists_widens_and_is_not_copyable():
    mock()
    row = {**fx("trades_history")["data"][0], "symbol": "OLDCOIN", "amount": "1.5"}
    fill = connector().to_fill(row)
    assert fill.instrument.copyable is False
    assert (fill.qty_atoms, fill.instrument.qty_decimals) == (
        150_000_000,
        8,
    )  # 1.5 at the 8 dp fallback


# ---- fills paging: newest first, walked back to the previous sync's high-water mark ----


@respx.mock
def test_a_full_backfill_walks_newest_to_oldest_and_returns_a_resume_cursor():
    newest = fx("trades_history")  # has_more True, next_cursor 1111U1i46
    oldest = fx("trades_history_small")  # the same account's first rows, has_more False
    fake = mock(trades__history=[newest, oldest])
    conn = connector()
    first = conn.fetch_asset_fills_page()
    # oldest-first inside the page: created_at 1792688433328 (two rows, same ms: ids
    # 305683495 then 305683497) then 1792689500801
    assert [f.venue_fill_id for f in first.items] == ["305683495", "305683497", "305692737"]
    assert first.next_cursor is not None and first.resume_cursor is None
    second = conn.fetch_asset_fills_page(first.next_cursor)
    assert fake.params("/trades/history")[1]["cursor"] == "1111U1i46"  # the venue's keyset cursor
    # small account: ids 10407603 and 10407605 share 1757417983986 (id breaks the tie)
    assert [f.venue_fill_id for f in second.items] == ["10407603", "10407605", "10419061"]
    assert second.next_cursor is None
    # resume: stop = newest created_at seen on the FIRST page minus the 10 minute overlap
    # 1792689500801 - 600_000 = 1792688900801
    assert json.loads(second.resume_cursor) == {"s": 1_792_688_900_801}


@respx.mock
def test_an_incremental_sync_stops_at_the_overlap_and_dedupes_by_id():
    rows = fx("trades_history")["data"]  # created_at: ...500801, ...433328, ...433328
    fake = mock(trades__history=page_of(rows, more=True))  # more pages exist
    conn = connector()
    resume = json.dumps({"s": 1_792_688_900_801})
    page = conn.fetch_asset_fills_page(resume)
    # the first row is newer than the stop; the other two are older: the walk ends
    # on this page even though the venue says has_more, and returns only the newer.
    assert [f.venue_fill_id for f in page.items] == ["305692737"]
    assert page.next_cursor is None
    assert json.loads(page.resume_cursor) == {"s": 1_792_688_900_801}  # max(stop, hwm - overlap)
    assert len(fake.requests) == 2  # the page and /info; no second page
    # An empty stream keeps its stop.
    respx.reset()
    mock(trades__history=page_of([]))
    empty = conn.fetch_asset_fills_page(resume)
    assert empty.items == [] and json.loads(empty.resume_cursor) == {"s": 1_792_688_900_801}


@respx.mock
def test_a_first_sync_honours_since_as_the_stop():
    mock(trades__history=page_of(fx("trades_history_small")["data"], more=True))
    since = datetime.fromtimestamp(1_757_417_983, UTC)  # 1757417983000 ms
    page = connector().fetch_asset_fills_page(since=since)
    # rows at or after 1757417983000: all three (...983986, ...983986, ...437835)
    # nothing older than the stop on the page, and the venue has more: keep walking
    assert len(page.items) == 3 and json.loads(page.next_cursor)["c"] == "1111next"
    later = datetime.fromtimestamp(1_757_420_000, UTC)
    respx.reset()
    mock(trades__history=page_of(fx("trades_history_small")["data"], more=True))
    page = connector().fetch_asset_fills_page(since=later)
    assert [f.venue_fill_id for f in page.items] == ["10419061"]  # only the close


@respx.mock
def test_a_restarted_backfill_reproduces_identical_legs():
    newest, oldest = fx("trades_history"), fx("trades_history_small")
    runs = []
    for _ in range(2):
        respx.reset()
        mock(trades__history=[newest, oldest])
        conn = connector()
        first = conn.fetch_asset_fills_page()
        second = conn.fetch_asset_fills_page(first.next_cursor)
        runs.append([f.model_dump() for f in (*first.items, *second.items)])
    assert runs[0] == runs[1]


# ---- P2: funding and balance events ------------------------------------------------------


@respx.mock
def test_funding_payout_is_the_signed_cash_effect_with_the_history_id_as_ref():
    mock(funding__history=fx("funding_history"), account__balance__history=page_of([]))
    conn = connector()
    first = conn.fetch_events_page()
    # oldest first: SOL ask +4.070849 (…14079), XRP bid -0.362887 (…18580), ZEC ask +0.453054
    assert [e.venue_ref for e in first.items] == ["60839549", "60840560", "60840763"]
    assert [legs(e)[("funding", "USDC")] for e in first.items] == [
        (4_070_849, 6),
        (-362_887, 6),
        (453_054, 6),
    ]
    assert first.items[1].description == "funding XRP rate 0.0000125 long 22035.08"
    assert first.items[0].legs[0].instrument_symbol == "SOL"
    assert first.items[0].occurred_at == datetime(2026, 10, 22, 17, 0, 14, 79000, tzinfo=UTC)
    assert first.next_cursor is not None  # the funding stream has more (older) pages


@respx.mock
def test_balance_history_books_each_row_once_when_it_reaches_the_balance():
    mock(account__balance__history=fx("balance_history"))
    conn = connector()
    state = {"p": "b", "b": {"s": 0}, "f": {"s": 0}}
    page = conn.fetch_events_page(json.dumps(state))
    # oldest first. A 300,000 deposit was credited in two parts: the credited
    # 19,833.236692 at once ("deposit", pending 280,166.763308) and the rest at its
    # "deposit_release". Each row's `amount` is the change to `balance`.
    kinds = [(e.kind, legs(e)[(e.kind.value, "USDC")][0]) for e in page.items]
    assert kinds == [
        (LegKind.DEPOSIT, 56_000_000_000),  # 56,000
        (LegKind.DEPOSIT, 175_000_011_200),  # 175,000.0112
        (LegKind.DEPOSIT, 19_833_236_692),  # the credited part
        (LegKind.WITHDRAWAL, -50_000_000_000),  # -50,000
        (LegKind.DEPOSIT, 280_166_763_308),  # deposit_release of the pending part
    ]
    assert page.items[2].description == "deposit pending 280166.763308"
    assert (
        page.items[4].venue_ref
        == "balance:1791244975783:deposit_release:280166.763308:701155.141285"
    )
    # no id field on the wire: the ref is built from the row's own venue values
    assert len({e.venue_ref for e in page.items}) == 5
    assert all(not e.ref_is_content_hash for e in page.items)
    # 56,000 + 175,000.0112 + 19,833.236692 - 50,000 + 280,166.763308 = 481,000.0112
    assert sum(legs(e)[(e.kind.value, "USDC")][0] for e in page.items) == 481_000_011_200
    assert conn.drain_unsupported() == []


@respx.mock
def test_both_streams_then_the_resume_cursor_keeps_each_streams_overlap():
    mock(
        funding__history=fx("funding_history_small"),
        account__balance__history=fx("balance_history_small"),
    )
    conn = connector()
    first = conn.fetch_events_page()
    assert [e.venue_ref for e in first.items] == ["765653"]  # one funding row, has_more false
    state = json.loads(first.next_cursor)
    assert state["p"] == "b" and "c" not in state["f"]
    second = conn.fetch_events_page(first.next_cursor)
    assert [(e.kind, e.venue_ref) for e in second.items] == [
        (LegKind.DEPOSIT, "balance:1757417793036:deposit:5000:5000")
    ]
    assert second.next_cursor is None
    # stop per stream = its newest row - 600,000 ms:
    # funding 1757419201374 - 600000 = 1757418601374; deposit 1757417793036 - 600000 = 1757417193036
    assert json.loads(second.resume_cursor) == {
        "p": "f",
        "f": {"s": 1_757_418_601_374},
        "b": {"s": 1_757_417_193_036},
    }
    # the next sync starts the funding stream again from that stop
    third = conn.fetch_events_page(second.resume_cursor)
    assert [e.venue_ref for e in third.items] == [
        "765653"
    ]  # inside the overlap: deduped by the ledger


@respx.mock
def test_other_balance_event_types():
    mock()
    conn = connector()
    row = {
        "amount": "1.5",
        "balance": "10",
        "pending_balance": "0",
        "created_at": 1_790_000_000_000,
    }

    def event(event_type, amount="1.5"):
        return conn.to_balance_event({**row, "event_type": event_type, "amount": amount})

    assert event("subaccount_transfer", "250").kind is LegKind.TRANSFER_IN
    assert event("subaccount_transfer", "-250").kind is LegKind.TRANSFER_OUT
    assert legs(event("subaccount_transfer", "-250")) == {
        ("transfer_out", "USDC"): (-250_000_000, 6)
    }
    assert event("payout").kind is LegKind.TRANSFER_IN  # an affiliate payout is not a return
    assert event("loan_interest_payout", "0.000026").kind is LegKind.INTEREST
    # covered by the fills and funding streams (only returned with include_trades)
    for covered in (
        "trade",
        "funding",
        "market_liquidation",
        "backstop_liquidation",
        "adl_liquidation",
    ):
        assert event(covered, "-3") is None
    assert conn.drain_unsupported() == []
    assert event("brand_new_type") is None
    [flag] = conn.drain_unsupported()
    assert flag.activity_type == "brand_new_type" and flag.reason == "unsupported_type"


@respx.mock
def test_the_small_account_replays_to_its_balance_within_the_documented_gap():
    """A complete 13-month-old account (deposit 5,000, three trades, one funding
    row): the replayed cash against GET /account's `balance`."""
    mock(
        trades__history=fx("trades_history_small"),
        funding__history=fx("funding_history_small"),
        account__balance__history=fx("balance_history_small"),
        account=fx("account_small"),
        positions=fx("positions_small"),
    )
    conn = connector()
    fills = conn.fetch_asset_fills()
    events = conn.fetch_events()
    all_legs = [leg for f in fills for leg in f.legs] + [leg for e in events for leg in e.legs]
    computed = balance_vector(all_legs)
    # 5000 - 0.176423 - 0.303393 - 0.014988 + (0.0114 - 0.47982 = -0.46842)
    #   = 4999.036776
    assert computed["USDC"].atoms == 4_999_036_776 and computed["USDC"].decimals == 6
    # fills: +2.03 +3.49 -5.52 = 0 SOL: flat, as the venue's positions are
    assert computed["SOL-PERP"].atoms == 0
    venue = conn.fetch_balances()
    assert [(a.asset, a.atoms) for a in venue.assets] == [("USDC", 4_999_073_822)]
    # The venue's cash is 0.037046 higher: hourly loan_interest_payout rows (4,588 of
    # them, 0.037029 in total, measured with include_trades=true) that the default
    # balance history does not return, plus 0.000017 of venue rounding.
    gap = 4_999_073_822 - computed["USDC"].atoms
    assert gap == 37_046
    checks = reconcile.compare_vectors(computed, venue.assets)
    assert all(c.ok for c in checks)  # 0.1% of 4,999.07 = 4.999 USDC of tolerance
    assert gap < reconcile.asset_tolerance_atoms("USDC", 4_999_073_822, 6)


# ---- P3: balances ------------------------------------------------------------------------


@respx.mock
def test_balances_cash_positions_and_equity():
    mock(account=fx("account"), positions=fx("positions"))
    bal = connector().fetch_balances()
    by_asset = {a.asset: a for a in bal.assets}
    # cross margin only (every position isolated: false, margin "0"): cash = balance
    assert (by_asset["USDC"].atoms, by_asset["USDC"].decimals) == (778_932_769_959, 6)
    assert bal.equity_usd_micros == 829_653_941_231  # account_equity
    # ask = short (negative), bid = long; lot-size decimals from /info
    assert (by_asset["ZEC-PERP"].atoms, by_asset["ZEC-PERP"].decimals) == (-2996, 2)  # 29.96
    assert by_asset["ZEC-PERP"].entry_price == "1312.36"
    assert (by_asset["BTC-PERP"].atoms, by_asset["BTC-PERP"].decimals) == (-606_768, 5)  # 6.06768
    assert (by_asset["XRP-PERP"].atoms, by_asset["XRP-PERP"].decimals) == (2_203_508, 2)
    assert by_asset["SOL-PERP"].atoms == -293_731  # 2937.31 short
    assert len(bal.assets) == 1 + 8
    assert bal.as_of == datetime.fromtimestamp(1_792_689_733.235, UTC)


@respx.mock
def test_isolated_margin_is_still_cash():
    """Live check 2026-10-22: equity = balance + uPnL + isolated margin, so
    `balance` excludes the margin posted in isolated positions, which is still
    the trader's cash and is in the ledger replay."""
    mock(account=fx("account_isolated"), positions=fx("positions_isolated"))
    bal = connector().fetch_balances()
    by_asset = {a.asset: a for a in bal.assets}
    # BTC short isolated, margin 810.171523; SOL short isolated, margin 1242.873061;
    # HYPE long cross, margin 0. 951.518416 + 810.171523 + 1242.873061 = 3004.563000
    assert (by_asset["USDC"].atoms, by_asset["USDC"].decimals) == (3_004_563_000, 6)
    assert bal.equity_usd_micros == 3_002_639_487  # uPnL on the positions is -1.923513
    assert by_asset["BTC-PERP"].atoms == -9_910  # 0.0991 short at 5 dp
    assert by_asset["HYPE-PERP"].atoms == 5_492  # 54.92 long at 2 dp


@respx.mock
def test_spot_balances_are_flagged_and_left_out():
    account = fx("account")
    account["data"]["spot_balances"] = [
        {"symbol": "SOL", "amount": "1.5"},
        {"symbol": "ETH", "amount": "0"},
    ]
    mock(account=account, positions=fx("positions_small"))
    conn = connector()
    bal = conn.fetch_balances()
    assert [a.asset for a in bal.assets] == ["USDC"]
    [flag] = conn.drain_unsupported()  # the zero balance is not activity
    assert (flag.venue_ref, flag.activity_type, flag.symbol) == ("spot:SOL", "spot_balance", "SOL")


@respx.mock
def test_check_connection_and_a_missing_account():
    mock(account=fx("account_small"))
    got = connector().check_connection()
    assert got.balance_micros == 4_999_073_822  # account_equity
    respx.reset()
    mock(account=httpx.Response(404, json=fx("account_not_found")))
    with pytest.raises(AccountNotFound, match="no account"):
        connector().check_connection()


def test_no_credential_means_nothing_can_write():
    conn = connector()
    assert conn.check_write_scope() is False
    evidence = conn.prove_read_only()
    assert evidence.method is ReadOnlyProof.NONE and evidence.read_only is True
    assert evidence.details["can_trade"] is False and evidence.details["can_withdraw"] is False
    assert evidence.details["address"] == ADDRESS


# ---- P5: ownership -----------------------------------------------------------------------


def solana_wallet():
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return key, b58encode(pub)


def test_claim_message_names_pacifica_and_keeps_the_address_case():
    key, address = solana_wallet()
    text = claim_message(address, "koine-abc")
    assert text.splitlines()[:4] == [
        "Koine ownership claim",
        "Venue: Pacifica",
        f"Address: {address}",
        "Code: koine-abc",
    ]
    assert address != address.lower()  # a mixed-case address is written as stored
    assert connector(address).claim_text("koine-abc") == text


def test_verify_claim_accepts_an_ed25519_signature_in_any_encoding():
    import base64

    key, address = solana_wallet()
    conn = connector(address)
    signature = key.sign(claim_message(address, "koine-abc").encode())
    for encoded in (
        b58encode(signature),
        base64.b64encode(signature).decode(),
        "0x" + signature.hex(),
    ):
        payload = conn.claim_payload("koine-abc", encoded)
        assert payload == encode_claim_proof("koine-abc", encoded)
        assert conn.verify_claim(payload) is True


def test_verify_claim_rejects_other_codes_other_signers_and_bare_codes():
    key, address = solana_wallet()
    other_key, _ = solana_wallet()
    conn = connector(address)
    good = b58encode(key.sign(claim_message(address, "koine-abc").encode()))
    assert conn.verify_claim(encode_claim_proof("koine-other", good)) is False  # another code
    forged = b58encode(other_key.sign(claim_message(address, "koine-abc").encode()))
    assert conn.verify_claim(encode_claim_proof("koine-abc", forged)) is False  # another key
    wrong_text = b58encode(key.sign(b"Koine ownership claim\nVenue: Lighter\n"))
    assert conn.verify_claim(encode_claim_proof("koine-abc", wrong_text)) is False
    assert conn.verify_claim("koine-abc") is False  # no signature yet
    assert conn.verify_claim(encode_claim_proof("koine-abc", "not-a-signature")) is False


# ---- P7: stream --------------------------------------------------------------------------


def test_the_stream_subscribes_pings_and_ignores_acks():
    stream = connector().stream_source()
    assert isinstance(stream, PacificaFillStream)
    assert stream.url() == "wss://ws.pacifica.fi/ws"
    assert json.loads(stream.subscribe_frames()[0]) == {
        "method": "subscribe",
        "params": {"source": "account_trades", "account": ADDRESS},
    }
    assert json.loads(stream.ping_frame()) == {"method": "ping"}
    ws = fx("ws_account_trades")
    assert stream.parse(json.dumps(ws["ack"])) == []
    assert stream.parse(json.dumps(ws["pong"])) == []
    assert stream.parse(json.dumps({"channel": "prices", "data": []})) == []
    with pytest.raises(StreamProtocolError, match="Invalid subscription parameters"):
        stream.parse(json.dumps(ws["error"]))
    with pytest.raises(StreamProtocolError):
        stream.parse("not json")


@respx.mock
def test_a_stream_frame_gives_the_same_fills_as_the_rest_row():
    mock()
    frame = fx("ws_account_trades")["frames"][3]  # ZEC taker close_long, two rows
    user = frame["data"][0]["u"]
    conn = connector(user)
    [event] = conn.stream_source().parse(json.dumps(frame))
    assert [f.venue_fill_id for f in event.fills] == ["305746710", "305746712"]
    assert event.nudge is False and event.ref == "305746712"
    first = event.fills[0]
    # close_long 0.08 ZEC @ 1218.3, taker: fee 0.031188, pnl -9.785687
    # REALIZED = pnl + fee = -9.785687 + 0.031188 = -9.754499; FEE = -0.031188
    assert first.action is Action.SELL and first.liquidity == "taker"
    assert legs(first) == {
        ("fill_base", "ZEC-PERP"): (-8, 2),
        ("realized_pnl", "USDC"): (-9_754_499, 6),
        ("fee", "USDC"): (-31_188, 6),
    }
    # the same row served by REST normalizes to identical legs and ids
    rest_row = {
        "history_id": 305746710,
        "order_id": 13849543593,
        "symbol": "ZEC",
        "amount": "0.08",
        "price": "1218.3",
        "entry_price": "1340.231237",
        "fee": "0.031188",
        "pnl": "-9.785687",
        "event_type": "fulfill_taker",
        "side": "close_long",
        "created_at": 1792695898933,
        "cause": "normal",
    }
    assert conn.to_fill(rest_row).model_dump() == first.model_dump()
    # a frame for someone else's subscription is ignored
    assert connector().stream_source().parse(json.dumps(frame)) == []


@respx.mock
def test_a_frame_of_unsupported_rows_nudges_and_does_not_queue_a_flag():
    mock()
    frame = fx("ws_account_trades")["frames"][3]
    for row in frame["data"]:
        row["tc"] = "game_settlement"
    conn = connector(frame["data"][0]["u"])
    [event] = conn.stream_source().parse(json.dumps(frame))
    assert event.fills == [] and event.nudge is True
    assert conn.drain_unsupported() == []  # the poller flags it; the stream never drains


# ---- P6: market data ---------------------------------------------------------------------


@respx.mock
def test_instruments_come_from_info_with_lot_decimals_and_copyability():
    mock()
    listed = {i.symbol: i for i in PacificaMarketData().fetch_instruments()}
    assert "SOL-USDC" not in listed  # a spot market
    btc = listed["BTC"]
    # lot_size 0.00001 -> 5 dp, 1 atom step; min order $10
    assert (btc.base_asset, btc.qty_decimals, btc.qty_step_atoms, btc.min_notional) == (
        "BTC-PERP",
        5,
        1,
        "10",
    )
    assert listed["ETH"].qty_decimals == 4 and listed["XRP"].qty_decimals == 2
    for crypto in ("BTC", "SOL", "ETH", "XRP", "ZEC", "BNB", "HYPE", "XMR", "PAXG"):
        assert listed[crypto].copyable is True, crypto
    # every `rfq` market is a real-world asset: stocks, FX, metals
    for rwa in ("GOOGL", "SNDK", "EURUSD", "XAU"):
        assert listed[rwa].copyable is False, rwa
    assert all(i.type is InstrumentType.PERP and i.quote_asset == "USDC" for i in listed.values())


@respx.mock
def test_quote_is_the_first_level_of_each_side():
    mock(book=fx("book_BTC"))
    quote = PacificaMarketData().fetch_quote("BTC")
    assert (quote.bid, quote.ask, quote.last) == ("81383", "81384", None)
    assert quote.as_of == datetime.fromtimestamp(1_792_689_735.867, UTC)
    respx.reset()
    mock(
        book={"success": True, "data": {"s": "BTC", "l": [[], []], "t": 1}},
        info__prices=fx("info_prices"),
    )
    q = PacificaMarketData().fetch_quote("SOL")
    assert (q.bid, q.ask, q.last) == (None, None, "103.53")  # no book: the mark in info_prices


@respx.mock
def test_daily_closes_drop_the_open_candle(monkeypatch):
    kline = fx("kline_BTC_1d")
    mock(kline=kline)
    days = [datetime.fromtimestamp(c["t"] / 1000, UTC).date() for c in kline["data"]]
    # the last candle ends after "now" (patched to its middle): still open, not a close
    open_candle = kline["data"][-1]
    monkeypatch.setattr(pf, "_now_ms", lambda: (open_candle["t"] + open_candle["T"]) // 2)
    closes = PacificaMarketData().fetch_daily_closes("BTC", days[0], days[-1])
    assert [c.day for c in closes] == days[:-1]
    assert [c.close for c in closes] == [c["c"] for c in kline["data"][:-1]]
    request = respx.calls.last.request
    assert dict(request.url.params)["interval"] == "1d"
    assert int(dict(request.url.params)["start_time"]) == kline["data"][0]["t"]


@respx.mock
def test_funding_history_pages_back_to_start_and_returns_oldest_first():
    page = fx("funding_rate_history_BTC")
    rows = page["data"]  # newest first, hourly
    mock(funding_rate__history=page)
    start = datetime.fromtimestamp(rows[2]["created_at"] / 1000, UTC)
    end = datetime.fromtimestamp(rows[0]["created_at"] / 1000, UTC)
    rates = PacificaMarketData().fetch_funding_history("BTC", start, end)
    # rows[3] and rows[4] are older than start: the walk ends on this page
    assert len(rates) == 3
    assert [r.at for r in rates] == sorted(r.at for r in rates)
    assert rates[0].rate == rows[2]["funding_rate"] == "0.0000125"
    assert rates[-1].mark_price == rows[0]["oracle_price"]
    assert len(respx.calls) == 1
    # a window that reaches past the page asks for the next (older) one
    respx.reset()
    mock(funding_rate__history=[page, page_of([])])
    old_start = datetime.fromtimestamp(rows[-1]["created_at"] / 1000 - 1, UTC)
    PacificaMarketData().fetch_funding_history("BTC", old_start, end)
    assert (
        len(respx.calls) == 2 and respx.calls[1].request.url.params["cursor"] == page["next_cursor"]
    )


@respx.mock
def test_margin_tiers_from_max_leverage():
    mock()
    md = PacificaMarketData()
    assert md.fetch_margin_tiers("BTC") == [MarginTier(lower_bound="0", max_leverage=50)]
    assert md.fetch_margin_tiers("SOL")[0].max_leverage == 20
    assert md.fetch_margin_tiers("NOPE") == []


# ---- P4/P8: leaderboard and privacy ------------------------------------------------------

PERIODS = ("day", "week", "month", "all")


@respx.mock
def test_leaderboard_drops_usernames_and_privacy_rows_and_ranks_public_rows():
    mock(leaderboard=fx("leaderboard"))
    md = PacificaMarketData(leaderboard_keep=100, leaderboard_min_account_value="10000")
    assert md.private_addresses() is None  # nothing fetched yet
    boards = md.fetch_leaderboards(PERIODS)
    assert set(boards) == {"day", "week", "month", "all"}
    rows = fx("leaderboard")["data"]
    private = {r["address"] for r in rows if r["privacy_mode"]}
    assert len(private) == 3 and md.private_addresses() == frozenset(private)
    for entries in boards.values():
        assert not {e.address for e in entries} & private  # privacy rows never reach a board
        assert all(e.roi is None for e in entries)
        assert "username" not in json.dumps([e.model_dump() for e in entries])
    # 12 public rows. ALL period, rank by pnl_all_time descending among them:
    #  2,544,489.28 r1; 431,937.91 r2; 365,882.22 r3; 342,772.72 r4; 215,006.35 r5;
    #  204,521.60 r6; 200,687.43 r7; 183,050.14 r8; 35,639.75 r9; -4,090.06 r10;
    #  -36,732.12 r11; -102,964.12 r12.
    # Offered: account value >= 10,000 and volume in the period: rows at r1, r4, r6,
    # r7, r8, r9, r11, r12 (r2, r3, r5, r10 hold under $10,000).
    assert [e.rank for e in boards["all"]] == [1, 4, 6, 7, 8, 9, 11, 12]
    top = boards["all"][0]
    assert (top.pnl, top.volume, top.account_value) == (
        "2544489.280320",  # the venue prints 5 dp, figures are held at 6
        "463821763.982504",
        "613790.112344",
    )
    # DAY: pnl_1d desc among public rows: 208,034.36 r1; 90,089.18 r2; 43,818.30 r3;
    # 16,490.45 r4; 4,005.76 r5; 13.84 r6; 0.011375 r7; 0 r8 (doc order); 0 r9;
    # -0.003142 r10; -702.25 r11; -7,707.71 r12. Offered: no volume that day for r7
    # and r12, and the $199 account r6 is under the $10,000 floor: r1-r5 and r11.
    assert [e.rank for e in boards["day"]] == [1, 2, 3, 4, 5, 11]
    # figures are the venue's, rounded half-even to 6 dp as strings
    assert boards["day"][0].pnl == "208034.362496"


@respx.mock
def test_leaderboard_keep_and_floor_none():
    mock(leaderboard=fx("leaderboard"))
    md = PacificaMarketData(leaderboard_keep=2)
    top = md.fetch_leaderboards(("all",))["all"]
    assert [e.rank for e in top] == [1, 2]  # no floor: the second public row is eligible
    respx.reset()
    mock(leaderboard=fx("leaderboard"))
    day = PacificaMarketData(leaderboard_keep=100).fetch_leaderboards(("day",))["day"]
    # no floor: the $199 account with volume that day counts (r6); the row with a
    # negative volume figure (r8) is not a trader
    assert [e.rank for e in day] == [1, 2, 3, 4, 5, 6, 11]
    assert PacificaMarketData().fetch_leaderboards(()) == {}


@respx.mock
def test_owner_private_answers_one_address_from_one_download():
    fake = mock(leaderboard=fx("leaderboard"))
    rows = fx("leaderboard")["data"]
    md = PacificaMarketData()
    assert md.owner_private(rows[0]["address"]) is True
    assert md.owner_private(rows[2]["address"]) is False
    assert md.owner_private(synthetic_solana_address("not-on-the-board")) is None
    assert md.owner_private("0x" + "ab" * 20) is None  # not a Solana address at all
    assert fake.paths().count("/leaderboard") == 3  # one download per call, none for junk


@respx.mock
def test_a_malformed_board_row_is_skipped_not_fatal():
    board = fx("leaderboard")
    board["data"].append({"address": "short", "pnl_7d": "1"})  # not an address
    board["data"].append(  # a figure that is not a number
        {**board["data"][2], "address": synthetic_solana_address("x1"), "pnl_7d": "n/a"}
    )
    mock(leaderboard=board)
    out = PacificaMarketData(leaderboard_keep=100).fetch_leaderboards(("week",))
    # With no floor, the 12 public rows with volume_7d > 0: rows 2, 4, 5, 6, 7, 9 and
    # 11 (the others traded nothing that week or carry a negative figure) = 7; the two
    # appended rows are dropped.
    assert len(out["week"]) == 7


def test_budget_lives_in_the_profile():
    from alphamarket.connectors.registry import get_profile

    profile = get_profile("pacifica")
    assert profile.rate_budgets == (pf.RATE_BUDGET,)
    assert pf.WS_USERS_PER_IP == profile.stream_users_per_ip == 90


def test_the_outbound_limiter_spends_ten_heavy_pages_per_minute_and_then_stops():
    from types import SimpleNamespace

    from alphamarket.connectors.base import BudgetExhausted
    from alphamarket.venue_limits import MemoryBuckets, VenueLimiter

    clock = SimpleNamespace(ms=1_800_000_000_000)
    limiter = VenueLimiter(
        MemoryBuckets(),
        "203.0.113.9",
        budgets_for=lambda v: (pf.RATE_BUDGET,),
        clock_ms=lambda: clock.ms,
    )
    for _ in range(10):  # 100 credits / 10 per heavy page
        limiter.acquire("pacifica", None, "trades_history")
    with pytest.raises(BudgetExhausted) as exc:
        limiter.acquire("pacifica", None, "balance_history")  # the same bucket
    assert exc.value.retry_after_seconds > 0
    clock.ms += 6_000  # 100 credits a minute refill 10 credits in 6 s: one more heavy page
    limiter.acquire("pacifica", None, "trades_history")
    # cheap calls still draw from the same IP bucket, 1 credit each
    clock.ms += 6_000
    for _ in range(10):
        limiter.acquire("pacifica", None, "account")
    with pytest.raises(BudgetExhausted):
        limiter.acquire("pacifica", None, "positions")
