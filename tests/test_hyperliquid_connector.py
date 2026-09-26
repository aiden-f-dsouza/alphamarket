"""Hyperliquid connector against public payloads captured 2026-09-26
(tests/fixtures/hyperliquid/, provenance in its README) and docs examples,
never live calls (invariant 6). Expected atoms are hand-computed in comments."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import respx

from alphamarket.connectors import hyperliquid as hl
from alphamarket.connectors.base import (
    Action,
    InstrumentType,
    LegKind,
    RateLimited,
    RateScope,
    ReadOnlyProof,
)
from alphamarket.connectors.hyperliquid import (
    INFO_URL,
    HyperliquidConnector,
    HyperliquidMarketData,
    claim_message,
    encode_claim_proof,
    extra_weight,
    recover_signer,
)
from alphamarket.connectors.registry import build_connector, build_market_data, get_profile
from alphamarket.money import from_atoms

FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"
ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"  # public leaderboard trader
LEDGER_B = "0x178d245216beb3d7e3e64dce8494b1efe04a9266"  # owner of the variety rows
LIQUIDATED = "0xf649b3c03a43dc9bf4562cd0a3bfe6b1aa03422f"
CAPTURED_MS = 1790463930949  # clearinghouseState "time" of the capture

# eth-account's published personal_sign vector ("I♥SF", docs of
# Account.recover_message): signer 0x5ce9454909639D2D17A3F753ce7d93fa0b9aB12E.
VECTOR_SIGNER = "0x5ce9454909639d2d17a3f753ce7d93fa0b9ab12e"
VECTOR_SIGNATURE = (
    "0xe6ca9bba58c88611fad66a6ce8f996908195593807c4b38bd528d2cff09d4eb3"
    "3e5bfbbf4d3e39b1a2fd816a7680c19ebebaf3a141b239934ad43cb33fcec8ce1c"
)
CODE = "koine-1a2b3c4d"
# The same vector key signing claim_message(<address>, CODE); only the
# signatures are stored, never the key.
SIG_OWN = (
    "0x613ea6e5c9a243786407a4eb7aa87a09631699d117e30ec8d9a1eb8e23d631e2"
    "093753f4fcbcac3b0a5584085c0d0ba5505d8e90edd7f803fa40ee29c5fcdbce1b"
)
SIG_FOR_ADDRESS = (
    "0x93c7eb710bfd659b50420a271b4e5dc6f384c3759fd39b7c0d468ba914e4f045"
    "66fa06ad5bd25aec302d42314fe734adf9048959495d72476afebface65f09011b"
)


def fx(name: str):
    return json.loads((FIXTURES / f"{name}_2026-09-26.json").read_text())


class FakeInfo:
    """respx side effect for POST /info: answers by request `type` (plus dex
    for meta/clearinghouseState), from a queue when several pages are
    scripted, and records every request body."""

    def __init__(self, **answers):
        self.answers = {
            "meta": fx("meta"),
            "meta:xyz": fx("meta_xyz"),
            "spotMeta": fx("spotMeta"),
            **answers,
        }
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        key = body["type"]
        if body.get("dex"):
            key = f"{key}:{body['dex']}"
        answer = self.answers[key]
        if isinstance(answer, list) and answer and isinstance(answer[0], Page):
            answer = answer.pop(0).payload  # scripted page: returned as is
        elif isinstance(answer, list) and "startTime" in body:
            # like the venue: only rows inside [startTime, endTime]
            end = body.get("endTime", 10**15)
            answer = [r for r in answer if body["startTime"] <= r["time"] <= end]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def of(self, kind: str) -> list[dict]:
        return [b for b in self.bodies if b["type"] == kind]


class Page:
    """One scripted response in a FakeInfo queue."""

    def __init__(self, payload):
        self.payload = payload


def mock(fake: FakeInfo) -> FakeInfo:
    respx.post(INFO_URL).mock(side_effect=fake)
    return fake


def legs(obj) -> dict[tuple[str, str], tuple[int, int]]:
    return {(leg.kind.value, leg.asset): (leg.delta_atoms, leg.decimals) for leg in obj.legs}


def fill_row(**overrides):
    base = fx("userFillsByTime")[0]
    return {**base, **overrides}


# ---- rate budget ------------------------------------------------------------------


def test_rate_budget_matches_the_documented_weights():
    budget = hl.RATE_BUDGET
    assert budget.scope is RateScope.IP
    assert (budget.capacity, budget.refill_per_minute) == (1200, 1200)
    assert budget.cost("clearinghouseState") == 2
    assert budget.cost("spotClearinghouseState") == 2
    assert budget.cost("allMids") == budget.cost("l2Book") == 2
    assert budget.cost("userRole") == 60
    assert budget.cost("userFillsByTime") == budget.cost("meta") == 20
    # +1 per whole 20 items: 60 fills -> 60 // 20 = 3; 19 -> 0; 2000 -> 100.
    assert extra_weight("userFillsByTime", 60) == 3
    assert extra_weight("userFunding", 19) == 0
    assert extra_weight("userFillsByTime", 2000) == 100
    # candleSnapshot +1 per 60: 120 -> 2, 7 -> 0; ledger updates have none.
    assert extra_weight("candleSnapshot", 120) == 2
    assert extra_weight("candleSnapshot", 7) == 0
    assert extra_weight("userNonFundingLedgerUpdates", 500) == 0


class Recorder:
    def __init__(self):
        self.seen: list[str] = []

    def before(self, endpoint):
        self.seen.append(endpoint)

    def after(self, endpoint, extra_cost=0, used=None):
        self.seen.append(f"after:{endpoint}:{extra_cost}")


@respx.mock
def test_throttle_runs_before_every_request_and_charges_items_after():
    mock(FakeInfo(userFillsByTime=fx("userFillsByTime")))
    conn = HyperliquidConnector(ADDRESS)
    rec = Recorder()
    conn.set_throttle(rec)
    conn.fetch_asset_fills_page()
    # 60 fills -> +3 after the page; then metadata, each charged before.
    assert rec.seen[:2] == ["userFillsByTime", "after:userFillsByTime:3"]
    assert sorted(rec.seen[2:]) == ["meta", "meta", "spotMeta"]
    assert len(respx.calls) == 4


@respx.mock
@pytest.mark.parametrize("status", [429, 418])
def test_rate_limit_statuses_raise_rate_limited_with_retry_after(status):
    mock(FakeInfo(userFillsByTime=httpx.Response(status, headers={"Retry-After": "7"})))
    with pytest.raises(RateLimited) as exc:
        HyperliquidConnector(ADDRESS).fetch_asset_fills_page()
    assert exc.value.retry_after_seconds == 7.0
    assert exc.value.endpoint == "userFillsByTime"


@respx.mock
def test_rate_limit_without_header_uses_default_wait():
    mock(FakeInfo(allMids=httpx.Response(429)))
    with pytest.raises(RateLimited) as exc:
        HyperliquidMarketData().fetch_mids()
    assert exc.value.retry_after_seconds == hl.DEFAULT_RETRY_AFTER


# ---- fills -> legs ------------------------------------------------------------------


@respx.mock
def test_perp_fills_map_to_position_realized_pnl_and_fee_legs():
    mock(FakeInfo(userFillsByTime=fx("userFillsByTime")))
    page = HyperliquidConnector(ADDRESS).fetch_asset_fills_page()
    assert len(page.items) == 60
    assert page.items == sorted(page.items, key=lambda f: f.filled_at)

    # Row 0: XPL "Close Long", side A, sz 73874.0 (szDecimals 0),
    # closedPnl -286.852742, fee 1.220361 USDC, crossed false.
    xpl = page.items[0]
    assert xpl.venue_fill_id == "229662172402780"
    assert xpl.order_id == "556513484966"
    assert xpl.action is Action.SELL
    assert xpl.qty_atoms == 73_874
    assert xpl.price == "0.11013"
    assert xpl.liquidity == "maker"
    assert xpl.filled_at == datetime(2026, 9, 25, 15, 2, 1, 663_000, tzinfo=UTC)
    assert xpl.instrument.type is InstrumentType.PERP
    assert xpl.instrument.base_asset == "XPL-PERP"
    assert legs(xpl) == {
        ("fill_base", "XPL-PERP"): (-73_874, 0),
        # -286.852742 USDC at 8 dp = -28_685_274_200
        ("realized_pnl", "USDC"): (-28_685_274_200, 8),
        # 1.220361 USDC at 8 dp = 122_036_100, negative
        ("fee", "USDC"): (-122_036_100, 8),
    }
    # Methodology worked example: cash -286.852742 - 1.220361 = -288.073103
    usdc = sum(leg.delta_atoms for leg in xpl.legs if leg.asset == "USDC")
    assert usdc == -28_807_310_300

    # An opening fill has no realized P&L leg. PUMP "Open Long", side B,
    # sz 409390.0 @ 0.004492, fee 0.82754 -> 82_754_000 at 8 dp.
    pump = next(f for f in page.items if f.venue_fill_id == "833210960580588")
    assert pump.action is Action.BUY
    assert legs(pump) == {
        ("fill_base", "PUMP-PERP"): (409_390, 0),
        ("fee", "USDC"): (-82_754_000, 8),
    }


@respx.mock
def test_hip3_perp_uses_its_dex_meta_and_collateral():
    fake = mock(FakeInfo(userFillsByTime=fx("userFillsByTime")))
    page = HyperliquidConnector(ADDRESS).fetch_asset_fills_page()
    # xyz:RKLB "Close Long": sz 2.74 at szDecimals 2 = 274 atoms,
    # closedPnl 2.27146 -> 227_146_000, fee 0.018261 -> 1_826_100 (8 dp).
    rklb = next(f for f in page.items if f.venue_fill_id == "244719860298587")
    assert rklb.instrument.symbol == "xyz:RKLB"
    assert rklb.instrument.base_asset == "xyz:RKLB-PERP"
    assert rklb.instrument.qty_decimals == 2
    assert legs(rklb) == {
        ("fill_base", "xyz:RKLB-PERP"): (-274, 2),
        ("realized_pnl", "USDC"): (227_146_000, 8),
        ("fee", "USDC"): (-1_826_100, 8),
    }
    assert {b.get("dex", "") for b in fake.of("meta")} == {"", "xyz"}


@respx.mock
def test_spot_buy_pays_quote_and_the_fee_in_base_token():
    mock(FakeInfo(userFillsByTime=fx("userFillsByTime")))
    page = HyperliquidConnector(ADDRESS).fetch_asset_fills_page()
    # "@210" = UXPL/USDC (spotMeta universe 210 -> tokens [343, 0]).
    # Buy 8221.0 @ 0.11678: base UXPL weiDecimals 6 -> 8_221_000_000;
    # quote 8221.0 x 0.11678 = 960.04838 USDC -> 96_004_838_000 at 8 dp;
    # fee 5.754699 UXPL -> 5_754_699 at 6 dp. qty_atoms at szDecimals 1: 82_210.
    buy = next(f for f in page.items if f.venue_fill_id == "990154881185303")
    assert buy.instrument.type is InstrumentType.SPOT
    assert (buy.instrument.base_asset, buy.instrument.quote_asset) == ("UXPL", "USDC")
    assert buy.qty_atoms == 82_210
    assert buy.instrument.qty_decimals == 1
    assert legs(buy) == {
        ("fill_base", "UXPL"): (8_221_000_000, 6),
        ("fill_quote", "USDC"): (-96_004_838_000, 8),
        ("fee", "UXPL"): (-5_754_699, 6),
    }


@respx.mock
def test_spot_sell_docs_example_keeps_exact_quote_precision():
    # Docs example spot fill: "@107" (HYPE/USDC) sell 43.84 @ 18.62041381,
    # fee 0.304521 USDC. Quote 43.84 x 18.62041381 = 816.3189414304 has
    # 10 dp, finer than USDC's 8, so the leg keeps 10 dp instead of rounding.
    row = {
        "coin": "@107",
        "px": "18.62041381",
        "sz": "43.84",
        "side": "A",
        "time": 1735969713869,
        "startPosition": "10659.65434798",
        "dir": "Sell",
        "closedPnl": "8722.988077",
        "hash": "0x2222138cc516e3fe746c0411dd733f02e60086f43205af2ae37c93f6a792430b",
        "oid": 59071663721,
        "crossed": True,
        "fee": "0.304521",
        "tid": 907359904431134,
        "feeToken": "USDC",
    }
    mock(FakeInfo(userFillsByTime=[row]))
    (fill,) = HyperliquidConnector(ADDRESS).fetch_asset_fills_page().items
    assert fill.action is Action.SELL
    assert fill.qty_atoms == 4_384  # HYPE szDecimals 2
    assert legs(fill) == {
        ("fill_base", "HYPE"): (-4_384_000_000, 8),  # weiDecimals 8
        ("fill_quote", "USDC"): (8_163_189_414_304, 10),
        ("fee", "USDC"): (-30_452_100, 8),
    }
    # Spot closedPnl is informational: the cash is the FILL_QUOTE leg.
    assert ("realized_pnl", "USDC") not in legs(fill)


@respx.mock
def test_builder_fee_is_inside_fee_and_a_negative_fee_is_a_rebate():
    rebate = fill_row(tid=1, fee="-0.01")
    builder = fill_row(tid=2, fee="0.05", builderFee="0.01")
    spaced = fill_row(tid=3, fee="0.02", feeToken=" USDC")  # docs' stray space
    mock(FakeInfo(userFillsByTime=[rebate, builder, spaced]))
    items = HyperliquidConnector(ADDRESS).fetch_asset_fills_page().items
    by_id = {f.venue_fill_id: legs(f) for f in items}
    assert by_id["1"][("fee", "USDC")] == (1_000_000, 8)  # +0.01 rebate
    assert by_id["2"][("fee", "USDC")] == (-5_000_000, 8)  # 0.05 total, not 0.06
    assert by_id["3"][("fee", "USDC")] == (-2_000_000, 8)


@respx.mock
def test_liquidation_fills_carry_the_cash_and_the_event_carries_none():
    fake = FakeInfo(
        userFillsByTime=fx("userFillsByTime_liquidation"),
        userFunding=[],
        userNonFundingLedgerUpdates=fx("userNonFundingLedgerUpdates_liquidation"),
    )
    mock(fake)
    conn = HyperliquidConnector(LIQUIDATED)
    market, backstop = conn.fetch_asset_fills_page().items
    # Market liquidation: buy 0.0135 ETH (szDecimals 4 -> 135), closedPnl
    # -2.79585 -> -279_585_000, fee 0.013315 -> -1_331_500.
    assert legs(market) == {
        ("fill_base", "ETH-PERP"): (135, 4),
        ("realized_pnl", "USDC"): (-279_585_000, 8),
        ("fee", "USDC"): (-1_331_500, 8),
    }
    # Backstop: the vault takes the position at 2243.92 vs mark 2236.9, so
    # the remaining equity leaves through closedPnl: (2243.92 - 2236.9) x
    # 0.0382 = 0.268164 of the 0.268296 account value. No fee.
    assert legs(backstop) == {
        ("fill_base", "ETH-PERP"): (382, 4),
        ("realized_pnl", "USDC"): (-641_454_400, 8),
    }
    page = conn.fetch_events_page()
    page = conn.fetch_events_page(page.next_cursor)
    (event,) = page.items
    assert event.kind is LegKind.LIQUIDATION
    assert event.legs == []
    assert "Cross liquidation of ETH -0.0382" in event.description
    assert "0.268296" in event.description


def test_fixture_decimals_round_trip_exactly():
    """from_atoms(to_atoms(s, d), d) == s for every recorded quantity."""
    conn = HyperliquidConnector(ADDRESS)
    conn._spot = hl.SpotInfo.parse(fx("spotMeta"))
    conn._dexes = {
        "": hl.PerpDexInfo.parse("", fx("meta")),
        "xyz": hl.PerpDexInfo.parse("xyz", fx("meta_xyz")),
    }
    for row in fx("userFillsByTime"):
        for key in ("sz", "closedPnl", "fee"):
            value, dp = hl.atoms(row[key], 8)
            assert from_atoms(value, dp) == Decimal(row[key])
        fill = conn.to_fill(row)
        assert from_atoms(fill.qty_atoms, fill.instrument.qty_decimals) == Decimal(row["sz"])


# ---- pagination -----------------------------------------------------------------


def _timed(rows_and_times):
    return [fill_row(tid=tid, time=t) for tid, t in rows_and_times]


@respx.mock
def test_fill_pages_pin_the_end_skip_boundary_duplicates_and_resume(monkeypatch):
    monkeypatch.setattr(hl, "FILLS_PAGE_LIMIT", 3)
    a, b, c, d, e = _timed([(1, 1000), (2, 2000), (3, 2000), (4, 3000), (5, 4000)])
    fake = mock(
        FakeInfo(
            userFillsByTime=[
                Page([a, b, c]),  # full -> more
                Page([b, c, d]),  # startTime inclusive: b, c come back
                Page([d, e]),  # short -> final
                Page([e]),  # next sync from the resume cursor
            ]
        )
    )
    conn = HyperliquidConnector(ADDRESS)
    p1 = conn.fetch_asset_fills_page()
    p2 = conn.fetch_asset_fills_page(p1.next_cursor)
    p3 = conn.fetch_asset_fills_page(p2.next_cursor)
    assert [[f.venue_fill_id for f in p.items] for p in (p1, p2, p3)] == [
        ["1", "2", "3"],
        ["4"],
        ["5"],
    ]
    assert p1.resume_cursor is None and p2.resume_cursor is None
    assert p3.next_cursor is None and p3.resume_cursor is not None
    reqs = fake.of("userFillsByTime")
    assert [r["startTime"] for r in reqs] == [0, 2000, 3000]
    assert len({r["endTime"] for r in reqs}) == 1  # upper bound pinned by page 1
    assert json.loads(p3.resume_cursor) == {"f": {"s": 4000, "x": ["5"]}}

    p4 = conn.fetch_asset_fills_page(p3.resume_cursor, since=datetime(2020, 1, 1, tzinfo=UTC))
    assert p4.items == []  # e already delivered; cursor beats since
    assert fake.of("userFillsByTime")[-1]["startTime"] == 4000


@respx.mock
def test_since_sets_start_time_and_a_full_single_millisecond_page_steps_on(monkeypatch):
    monkeypatch.setattr(hl, "FILLS_PAGE_LIMIT", 2)
    # since -> startTime in ms: 2026-09-25T15:02:01.663Z = 1790348521663
    assert hl._dt_to_ms(datetime(2026, 9, 25, 15, 2, 1, 663_000, tzinfo=UTC)) == 1790348521663
    fake = mock(FakeInfo(userFillsByTime=[Page(_timed([(1, 5000), (2, 5000)])), Page([])]))
    conn = HyperliquidConnector(ADDRESS)
    p1 = conn.fetch_asset_fills_page(since=datetime(1970, 1, 1, 0, 0, 5, tzinfo=UTC))
    assert fake.of("userFillsByTime")[0]["startTime"] == 5000
    assert [f.venue_fill_id for f in p1.items] == ["1", "2"]
    # Whole page inside one ms cannot advance by time: step 1 ms.
    assert json.loads(p1.next_cursor)["f"] == {"s": 5001, "x": []}
    p2 = conn.fetch_asset_fills_page(p1.next_cursor)
    assert p2.items == [] and p2.next_cursor is None


@respx.mock
def test_events_page_funding_then_ledger_then_resume():
    fake = mock(
        FakeInfo(
            userFunding=fx("userFunding"),
            userNonFundingLedgerUpdates=fx("userNonFundingLedgerUpdates"),
        )
    )
    conn = HyperliquidConnector(ADDRESS)
    p1 = conn.fetch_events_page()
    assert len(p1.items) == 15 and all(e.kind is LegKind.FUNDING for e in p1.items)
    assert json.loads(p1.next_cursor)["p"] == "ledger"
    p2 = conn.fetch_events_page(p1.next_cursor)
    assert len(p2.items) == 39
    assert p2.next_cursor is None
    resume = json.loads(p2.resume_cursor)
    assert resume["p"] == "funding" and "e" not in resume
    assert resume["f"]["s"] == 1790434800060  # last funding hour
    assert resume["l"] == {
        "s": 1790143870799,
        "x": [
            "1790143870799:send:0xf441362bc075a961f5ba04450370fc02028b00115b78c8349809e17e7f79834c"
        ],
    }
    assert (
        fake.of("userFunding")[0]["endTime"] == fake.of("userNonFundingLedgerUpdates")[0]["endTime"]
    )
    # Next sync: both streams resume at their boundary ms, whose rows were
    # already delivered, so nothing is emitted twice.
    p3 = conn.fetch_events_page(p2.resume_cursor)
    assert p3.items == []
    p4 = conn.fetch_events_page(p3.next_cursor)
    assert p4.items == []
    assert fake.of("userFunding")[-1]["startTime"] == 1790434800060
    assert fake.of("userNonFundingLedgerUpdates")[-1]["startTime"] == 1790143870799


@respx.mock
def test_funding_rows_have_one_id_per_time_and_coin():
    mock(FakeInfo(userFunding=fx("userFunding"), userNonFundingLedgerUpdates=[]))
    events = HyperliquidConnector(ADDRESS).fetch_events_page().items
    first = events[0]
    assert first.venue_ref == "1790427600035:ETH"
    assert first.occurred_at == datetime(2026, 9, 26, 13, 0, 0, 35_000, tzinfo=UTC)
    # usdc -0.388806 -> -38_880_600 at 8 dp, instrument = the coin
    (leg,) = first.legs
    assert (leg.kind, leg.asset, leg.delta_atoms, leg.decimals, leg.instrument_symbol) == (
        LegKind.FUNDING,
        "USDC",
        -38_880_600,
        8,
        "ETH",
    )
    refs = [e.venue_ref for e in events]
    assert len(set(refs)) == len(refs) == 15  # 3 hours x 5 coins, zero hash shared


# ---- ledger updates -> legs --------------------------------------------------------


def _ledger_conn(address=LEDGER_B) -> HyperliquidConnector:
    conn = HyperliquidConnector(address)
    conn._spot = hl.SpotInfo.parse(fx("spotMeta"))
    return conn


def _variety():
    return {i: row for i, row in enumerate(fx("userNonFundingLedgerUpdates_variety"))}


def test_ledger_variety_rows_map_to_flows_and_fees():
    conn = _ledger_conn()
    rows = _variety()
    ev = {i: conn.to_ledger_event(r) for i, r in rows.items()}
    # deposit 40866.33 USDC -> 4_086_633_000_000 at 8 dp
    assert ev[0].kind is LegKind.DEPOSIT
    assert legs(ev[0]) == {("deposit", "USDC"): (4_086_633_000_000, 8)}
    # withdraw 9999.0 + fee 1.0 (a 10,000 request)
    assert legs(ev[1]) == {
        ("withdrawal", "USDC"): (-999_900_000_000, 8),
        ("fee", "USDC"): (-100_000_000, 8),
    }
    # send to self, perp -> spot: same account, no legs
    assert (ev[2].kind, ev[2].legs) == (LegKind.CONVERT, [])
    assert (ev[3].kind, ev[3].legs) == (LegKind.CONVERT, [])
    # spotTransfer 0.16128241 USDE in (weiDecimals 8)
    assert legs(ev[4]) == {("transfer_in", "USDE"): (16_128_241, 8)}
    # send 8000.0 USDC out / 14822.93 USDC in
    assert legs(ev[5]) == {("transfer_out", "USDC"): (-800_000_000_000, 8)}
    assert legs(ev[6]) == {("transfer_in", "USDC"): (1_482_293_000_000, 8)}
    # cStakingTransfer 10.025988 HYPE staked: leaves the spot balance
    assert legs(ev[8]) == {("transfer_out", "HYPE"): (-1_002_598_800, 8)}
    # MAX is not in the trimmed spotMeta: fallback 8 dp, 3824.3909 -> 382_439_090_000
    assert legs(ev[9]) == {("transfer_in", "MAX"): (382_439_090_000, 8)}
    # 10 USDC to the HyperEVM system address + 0.00002661 HYPE gas
    assert legs(ev[11]) == {
        ("transfer_out", "USDC"): (-1_000_000_000, 8),
        ("fee", "HYPE"): (-2_661, 8),
    }
    # 325.58777756 USDE out + 0.00002 HYPE gas
    assert legs(ev[12]) == {
        ("transfer_out", "USDE"): (-32_558_777_756, 8),
        ("fee", "HYPE"): (-2_000, 8),
    }
    # 1000 USDC out with fee 0.001402 USDC
    assert legs(ev[15]) == {
        ("transfer_out", "USDC"): (-100_000_000_000, 8),
        ("fee", "USDC"): (-140_200, 8),
    }
    # borrowLend: supply 19.986 HYPE leaves spot; borrow 300 USDC arrives
    assert legs(ev[16]) == {("transfer_out", "HYPE"): (-1_998_600_000, 8)}
    assert legs(ev[17]) == {("transfer_in", "USDC"): (30_000_000_000, 8)}
    # Refs: time:type:hash, unique across the rows
    assert ev[0].venue_ref == (
        "1757764284430:deposit:0x2440915fe806887e0855a9bd38b8f6f057e82b8659b60bf42ed68287f64466aa"
    )
    assert len({e.venue_ref for e in ev.values()}) == len(ev)


def test_docs_ledger_types_map_without_inventing_amounts():
    conn = _ledger_conn(ADDRESS)
    me, other, vault = ADDRESS, "0x" + "ab" * 20, "0x" + "cd" * 20

    def one(delta):
        return conn.to_ledger_event({"time": 1, "hash": "0x" + "12" * 32, "delta": delta})

    assert one({"type": "accountClassTransfer", "usdc": "5.0", "toPerp": True}).legs == []
    internal = one(
        {"type": "internalTransfer", "usdc": "12.5", "user": me, "destination": other, "fee": "1.0"}
    )
    assert legs(internal) == {
        ("transfer_out", "USDC"): (-1_250_000_000, 8),
        ("fee", "USDC"): (-100_000_000, 8),
    }
    sub = one({"type": "subAccountTransfer", "usdc": "3.0", "user": other, "destination": me})
    assert legs(sub) == {("transfer_in", "USDC"): (300_000_000, 8)}
    assert legs(one({"type": "vaultDeposit", "vault": vault, "usdc": "100.0"})) == {
        ("transfer_out", "USDC"): (-10_000_000_000, 8)
    }
    withdraw = {
        "type": "vaultWithdraw",
        "vault": vault,
        "user": me,
        "requestedUsd": "110.0",
        "commission": "1.5",
        "closingCost": "0.1",
        "basis": "100.0",
        "netWithdrawnUsd": "108.4",
    }
    assert legs(one(withdraw)) == {("transfer_in", "USDC"): (10_840_000_000, 8)}
    assert legs(one({"type": "vaultDistribution", "vault": vault, "usdc": "2.25"})) == {
        ("transfer_in", "USDC"): (225_000_000, 8)
    }
    assert legs(one({"type": "rewardsClaim", "amount": "0.75"})) == {
        ("transfer_in", "USDC"): (75_000_000, 8)
    }
    assert legs(one({"type": "spotGenesis", "token": "PURR", "amount": "12.5"})) == {
        ("transfer_in", "PURR"): (1_250_000, 5)  # PURR weiDecimals 5
    }
    unknown = one({"type": "somethingNew", "amount": "9"})
    assert (unknown.kind, unknown.legs) == (LegKind.ADJUSTMENT, [])
    assert "somethingNew" in unknown.description


def test_connected_vault_sees_deposits_as_inflows():
    vault = _ledger_conn("0x" + "cd" * 20)
    depositor = "0x" + "ab" * 20
    row = {"type": "vaultDeposit", "vault": vault.address, "usdc": "100.0", "user": depositor}
    event = vault.to_ledger_event({"time": 1, "hash": "0x" + "12" * 32, "delta": row})
    assert legs(event) == {("transfer_in", "USDC"): (10_000_000_000, 8)}
    wd = {
        "type": "vaultWithdraw",
        "vault": vault.address,
        "user": depositor,
        "requestedUsd": "110.0",
        "commission": "1.5",
        "closingCost": "0.1",
        "basis": "100.0",
        "netWithdrawnUsd": "108.4",
    }
    event = vault.to_ledger_event({"time": 2, "hash": "0x" + "34" * 32, "delta": wd})
    # 108.4 net + 1.5 commission = 109.9 leaves the vault
    assert legs(event) == {("transfer_out", "USDC"): (-10_990_000_000, 8)}


def test_zero_hash_rows_get_content_refs_and_exact_duplicates_an_ordinal():
    conn = _ledger_conn(ADDRESS)
    row = {"time": 7, "hash": hl.ZERO_HASH, "delta": {"type": "rewardsClaim", "amount": "1.0"}}
    other = {**row, "delta": {"type": "rewardsClaim", "amount": "2.0"}}
    refs = [e.venue_ref for e in conn._ledger_events([row, row, other])]
    assert refs[0].startswith("7:rewardsClaim:0x000") and len(refs[0].split(":")) == 4
    assert refs[1] == refs[0] + "#1"
    assert refs[2] != refs[0]


# ---- balances -------------------------------------------------------------------


@respx.mock
def test_unified_account_balances_come_from_spot_with_perp_positions():
    fake = mock(
        FakeInfo(
            userAbstraction=fx("userAbstraction"),  # "unifiedAccount"
            spotClearinghouseState=fx("spotClearinghouseState"),
            perpDexs=fx("perpDexs"),  # [first dex, "xyz"]
            clearinghouseState=fx("clearinghouseState"),
            **{"clearinghouseState:xyz": fx("clearinghouseState_xyz")},
        )
    )
    bal = HyperliquidConnector(ADDRESS).fetch_balances()
    got = {a.asset: (a.atoms, a.decimals, a.entry_price) for a in bal.assets}
    assert got == {
        # spot totals; zero-total tokens dropped
        "USDC": (10_140_118_075_769, 8, None),  # 101401.18075769
        "UXPL": (300_528_382_792, 6, None),  # 300528.382792
        "ETH-PERP": (115_669, 4, "2593.2"),  # szi 11.5669
        "LDO-PERP": (720_668, 1, "0.4618"),  # 72066.8
        "LINK-PERP": (21_496, 1, "13.22142"),  # 2149.6
        "ONDO-PERP": (405_960, 0, "0.410841"),
        "PUMP-PERP": (6_672_597, 0, "0.004493"),
    }
    # Perp dex accountValue is "not meaningful" in unified mode; equity is
    # spot USDC + unrealized P&L: 1145.1231 + 1198.470884 + 1873.536 +
    # 50780.97787 - 836.883566 = 54161.224288; 101401.18075769 +
    # 54161.224288 = 155562.40504569 -> 155_562_405_045.69 micros -> half-even
    # 155_562_405_046.
    assert bal.equity_usd_micros == 155_562_405_046
    assert bal.as_of == datetime(2026, 9, 26, 23, 5, 30, 949_000, tzinfo=UTC)
    # xyz has no positions, so its meta is never fetched
    assert [b.get("dex", "") for b in fake.of("meta")] == [""]


@respx.mock
def test_standard_mode_cash_is_account_value_minus_unrealized_plus_spot():
    # Docs examples: clearinghouseState (ETH 0.0335 isolated, uPnL -0.0134,
    # accountValue 13109.482328) and spotClearinghouseState (14.625485 USDC,
    # 2000 PURR).
    perp = {
        "assetPositions": [
            {
                "position": {
                    "coin": "ETH",
                    "entryPx": "2986.3",
                    "szi": "0.0335",
                    "unrealizedPnl": "-0.0134",
                    "positionValue": "100.02765",
                },
                "type": "oneWay",
            }
        ],
        "marginSummary": {"accountValue": "13109.482328"},
        "time": 1708622398623,
    }
    spot = {
        "balances": [
            {"coin": "USDC", "token": 0, "hold": "0.0", "total": "14.625485", "entryNtl": "0.0"},
            {"coin": "PURR", "token": 1, "hold": "0", "total": "2000", "entryNtl": "1234.56"},
        ]
    }
    empty = {"assetPositions": [], "marginSummary": {"accountValue": "0.0"}, "time": 1}
    mock(
        FakeInfo(
            userAbstraction="default",
            spotClearinghouseState=spot,
            perpDexs=fx("perpDexs"),
            clearinghouseState=perp,
            **{"clearinghouseState:xyz": empty},
        )
    )
    conn = HyperliquidConnector(ADDRESS)
    bal = conn.fetch_balances()
    got = {a.asset: (a.atoms, a.decimals) for a in bal.assets}
    # USDC = (13109.482328 - (-0.0134)) + 14.625485 = 13124.121213
    assert got == {
        "USDC": (1_312_412_121_300, 8),
        "PURR": (200_000_000, 5),  # 2000 at weiDecimals 5
        "ETH-PERP": (335, 4),
    }
    # equity = 13124.121213 + (-0.0134) = 13124.107813 = accountValue + spot USDC
    assert bal.equity_usd_micros == 13_124_107_813
    assert conn.fetch_balance().balance_micros == 13_124_107_813


# ---- connection, read-only proof, ownership ------------------------------------------------


def test_check_connection_rejects_unknown_and_agent_addresses():
    with respx.mock:
        mock(FakeInfo(userRole={"role": "missing"}))
        with pytest.raises(ValueError, match="no account"):
            HyperliquidConnector(ADDRESS).check_connection()
    with respx.mock:
        mock(FakeInfo(userRole={"role": "agent", "data": {"user": LEDGER_B}}))
        with pytest.raises(ValueError, match=LEDGER_B):
            HyperliquidConnector(ADDRESS).check_connection()


@respx.mock
def test_prove_read_only_holds_nothing_and_records_the_role():
    mock(FakeInfo(userRole=fx("userRole_user")))
    conn = HyperliquidConnector(ADDRESS)
    ev = conn.prove_read_only()
    assert ev.method is ReadOnlyProof.NONE
    assert ev.read_only is True
    assert ev.details["role"] == "user"
    assert ev.details["can_trade"] is False
    assert conn.check_write_scope() is False
    assert not any("secret" in str(v).lower() for v in ev.details.values())


def test_prove_read_only_when_the_role_cannot_be_read_or_is_an_agent():
    with respx.mock:
        mock(FakeInfo(userRole=httpx.Response(500)))
        ev = HyperliquidConnector(ADDRESS).prove_read_only()
    assert ev.read_only is True  # still nothing held
    assert ev.details["role"] is None
    assert ev.details["role_error"] == "HTTPStatusError"
    with respx.mock:
        mock(FakeInfo(userRole={"role": "agent", "data": {"user": LEDGER_B}}))
        ev = HyperliquidConnector(ADDRESS).prove_read_only()
    assert (ev.details["role"], ev.details["agent_of"]) == ("agent", LEDGER_B)


def test_eip191_published_vector_recovers_its_signer():
    assert recover_signer("I♥SF", VECTOR_SIGNATURE) == VECTOR_SIGNER
    assert recover_signer("I♥SF!", VECTOR_SIGNATURE) != VECTOR_SIGNER
    assert recover_signer("I♥SF", "0x1234") is None


def test_claim_message_names_address_and_code():
    text = claim_message(VECTOR_SIGNER.upper().replace("0X", "0x"), CODE)
    assert f"Address: {VECTOR_SIGNER}\n" in text
    assert f"Code: {CODE}\n" in text
    assert "not a transaction" in text


@respx.mock
def test_verify_claim_accepts_the_owners_signature_only():
    fake = mock(FakeInfo(userRole=fx("userRole_user")))
    conn = HyperliquidConnector(VECTOR_SIGNER)
    assert conn.verify_claim(encode_claim_proof(CODE, SIG_OWN)) is True
    assert fake.bodies == []  # a direct match needs no venue call
    # Signature over another code recovers some other address: rejected.
    assert conn.verify_claim(encode_claim_proof("koine-other", SIG_OWN)) is False
    assert conn.verify_claim(CODE) is False  # no signature yet
    assert conn.verify_claim(encode_claim_proof(CODE, "0xdeadbeef")) is False


def test_verify_claim_for_another_address_checks_master_and_vault_leader():
    # SIG_FOR_ADDRESS is the vector key signing the claim for ADDRESS.
    proof = encode_claim_proof(CODE, SIG_FOR_ADDRESS)
    with respx.mock:
        mock(FakeInfo(userRole=fx("userRole_user")))
        assert HyperliquidConnector(ADDRESS).verify_claim(proof) is False
    with respx.mock:
        mock(FakeInfo(userRole={"role": "subAccount", "data": {"master": VECTOR_SIGNER}}))
        assert HyperliquidConnector(ADDRESS).verify_claim(proof) is True
    with respx.mock:
        vault = {"leader": VECTOR_SIGNER.upper().replace("0X", "0x")}
        fake = mock(FakeInfo(userRole=fx("userRole_vault"), vaultDetails=vault))
        assert HyperliquidConnector(ADDRESS).verify_claim(proof) is True
        assert fake.of("vaultDetails")[0]["vaultAddress"] == ADDRESS
    with respx.mock:
        mock(FakeInfo(userRole=httpx.Response(502)))
        assert HyperliquidConnector(ADDRESS).verify_claim(proof) is None


# ---- market data -----------------------------------------------------------------------


@respx.mock
def test_instruments_cover_perps_and_spot_pairs_and_price_spot_tokens():
    mock(FakeInfo())
    md = HyperliquidMarketData()
    inst = {i.symbol: i for i in md.fetch_instruments()}
    btc = inst["BTC"]
    assert (btc.type, btc.base_asset, btc.quote_asset, btc.qty_decimals) == (
        InstrumentType.PERP,
        "BTC-PERP",
        "USDC",
        5,
    )
    hype = inst["@107"]
    assert (hype.type, hype.base_asset, hype.quote_asset, hype.qty_decimals) == (
        InstrumentType.SPOT,
        "HYPE",
        "USDC",
        2,
    )
    assert inst["PURR/USDC"].base_asset == "PURR"
    listed = list(inst.values())
    assert md.pricing_symbol("HYPE", listed) == "@107"
    assert md.pricing_symbol("UXPL", listed) == "@210"
    assert md.pricing_symbol("USDC", listed) is None
    assert len(respx.calls) == 2  # meta + spotMeta


@respx.mock
def test_daily_closes_drop_the_open_candle(monkeypatch):
    monkeypatch.setattr(hl, "_now_ms", lambda: CAPTURED_MS)
    fake = mock(FakeInfo(candleSnapshot=fx("candleSnapshot_BTC_1d")))
    md = HyperliquidMarketData()
    rec = Recorder()
    md.set_throttle(rec)
    closes = md.fetch_daily_closes("BTC", date(2026, 9, 20), date(2026, 9, 26))
    req = fake.of("candleSnapshot")[0]["req"]
    # 2026-09-20T00:00Z = 1789862400000; end = 2026-09-27T00:00Z - 1 ms.
    assert req == {
        "coin": "BTC",
        "interval": "1d",
        "startTime": 1_789_862_400_000,
        "endTime": 1_790_467_199_999,
    }
    # Seven candles; 09-26 closes at 1790467199999 > capture time: dropped.
    assert [(c.day.isoformat(), c.close) for c in closes] == [
        ("2026-09-20", "81221.0"),
        ("2026-09-21", "86662.0"),
        ("2026-09-22", "86237.0"),
        ("2026-09-23", "84366.0"),
        ("2026-09-24", "84385.0"),
        ("2026-09-25", "84066.0"),
    ]
    assert rec.seen == ["candleSnapshot"]  # 7 // 60 = 0 extra


def test_quote_is_top_of_book_with_mid_fallback():
    book = fx("l2Book_BTC")
    with respx.mock:
        mock(FakeInfo(l2Book=book))
        q = HyperliquidMarketData().fetch_quote("BTC")
    assert (q.bid, q.ask) == (book["levels"][0][0]["px"], book["levels"][1][0]["px"])
    assert q.bid == "84284.0"
    assert q.as_of == hl._ms_to_dt(book["time"])
    empty = {"coin": "@107", "time": 1, "levels": [[], []]}
    with respx.mock:
        mock(FakeInfo(l2Book=empty, allMids=fx("allMids")))
        q = HyperliquidMarketData().fetch_quote("@107")
    assert (q.bid, q.ask, q.last) == (None, None, "91.9955")


@respx.mock
def test_funding_history_pages_until_a_short_page(monkeypatch):
    monkeypatch.setattr(hl, "TIME_RANGE_PAGE_LIMIT", 3)
    rows = fx("fundingHistory_BTC")
    fake = mock(FakeInfo(fundingHistory=[Page(rows[:3]), Page(rows[3:]), Page([])]))
    start = datetime(2026, 9, 26, tzinfo=UTC)
    end = datetime(2026, 9, 26, 6, tzinfo=UTC)
    rates = HyperliquidMarketData().fetch_funding_history("BTC", start, end)
    assert [r.rate for r in rates] == [r["fundingRate"] for r in rows]
    assert rates[0].at == datetime(2026, 9, 26, 0, 0, 0, 2_000, tzinfo=UTC)
    reqs = fake.of("fundingHistory")
    # second page starts 1 ms after the third row (1790388000051 + 1)
    assert [r["startTime"] for r in reqs] == [
        1_790_380_800_000,
        1_790_388_000_052,
        1_790_398_800_054,
    ]
    assert {r["endTime"] for r in reqs} == {1_790_402_400_000}


# ---- registry ---------------------------------------------------------------------------


def test_registry_profile_and_builders():
    profile = get_profile("hyperliquid")
    assert profile.readonly_proof is ReadOnlyProof.NONE
    assert not profile.needs_secret
    assert profile.history_depth == "10k_fills"
    assert profile.quote_asset == "USDC"
    assert profile.transfers_observable and profile.balance_observable
    assert profile.rate_budgets == (hl.RATE_BUDGET,)
    assert profile.paper_fee_model.value == "bps_of_notional"
    assert profile.fee_rate_bps == 5  # 4.5 bps taker rounded up
    assert profile.instrument.value == "asset"
    assert not profile.requires_partner_approval
    tos = Path(__file__).parent.parent / profile.tos_doc
    assert "Status: **ok**" in tos.read_text()
    conn = build_connector("hyperliquid", "prod", ADDRESS.upper().replace("0X", "0x"))
    assert isinstance(conn, HyperliquidConnector) and conn.address == ADDRESS
    assert conn.info_url == INFO_URL
    assert isinstance(build_market_data("hyperliquid", "prod"), HyperliquidMarketData)
    with pytest.raises(ValueError):
        build_connector("hyperliquid", "prod", "not-an-address")
