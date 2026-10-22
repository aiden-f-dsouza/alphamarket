"""Lighter connector against public captures of 2026-10-22
(tests/fixtures/lighter/, provenance and anonymisation in its README), never
live calls (invariant 6). Expected values are hand-computed in comments.

Two accounts are replayed:
- index 714710 (synthetic owner 0x..a1): a large market maker; one explorer
  page of 100 trades, the flow rows of each type, and its account state.
- index 760300 (synthetic owner 0x..b2): a small trader whose WHOLE history
  is 145 explorer rows (one deposit, 138 trades, leverage changes) plus its
  account state minutes later, so the full replay must reconcile with it."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx

from alphamarket.connectors import lighter
from alphamarket.connectors.base import (
    Action,
    LegKind,
    RateLimited,
    RateScope,
    ReadOnlyProof,
    StreamMode,
    StreamProtocolError,
    StreamUnavailable,
)
from alphamarket.connectors.lighter import (
    API_BUDGET,
    API_URL,
    EXPLORER_BUDGET,
    WS_URL,
    LighterConnector,
    LighterError,
    claim_message,
    encode_claim_proof,
)
from alphamarket.connectors.registry import build_connector, build_market_data, get_profile
from alphamarket.money import from_atoms

FIXTURES = Path(__file__).parent / "fixtures" / "lighter"
DAY = "2026-10-22"
ADDR_A = "0x00000000000000000000000000000000000000a1"  # owns index 714710
ADDR_B = "0x00000000000000000000000000000000000000b2"  # owns index 760300
UNKNOWN = "0x00000000000000000000000000000000000000ff"
# The 760300 account was read at transaction_time 1792694009 (18:33:29 UTC).
CAPTURED = datetime(2026, 10, 22, 18, 33, 29, tzinfo=UTC)


def fx_text(name: str) -> str:
    return (FIXTURES / f"{name}_{DAY}.json").read_text()


def fx(name: str):
    return json.loads(fx_text(name), parse_float=Decimal)


def ts(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


class FakeLighter:
    """respx side effect for both hosts. Explorer logs are served from a
    newest-first list per index, sliced by offset/limit like the real API;
    `on_logs` can mutate the lists between requests (rows arriving during a
    walk). fundings answer the `count_back` rows ending at end_timestamp, as
    observed live. Records every (path, params)."""

    def __init__(self) -> None:
        self.logs: dict[int, list[dict]] = {}
        self.owners: dict[str, str] = {ADDR_A: "714710", ADDR_B: "760300"}
        self.accounts: dict[int, str] = {714710: "714710", 760300: "760300"}
        self.fundings: dict[int, list[dict]] = {}
        self.boards: dict[tuple[str, str], list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.on_logs: Callable[[int, int], None] | None = None
        self.status: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        params = {k: v[0] for k, v in parse_qs(url.query).items()}
        path = url.path
        self.calls.append((path, params))
        for key, code in self.status.items():
            if path.endswith(key):
                return httpx.Response(code, headers={"Retry-After": "7"}, text="{}")
        if path.startswith("/api/accounts/"):
            index = int(path.split("/")[3])
            n = sum(1 for p, _ in self.calls if p == path)
            if self.on_logs is not None:
                self.on_logs(index, n)
            rows = self.logs.get(index, [])
            offset, limit = int(params["offset"]), int(params["limit"])
            return httpx.Response(200, text=json.dumps(rows[offset : offset + limit]))
        endpoint = path.rsplit("/", 1)[-1]
        if endpoint == "accountsByL1Address":
            owner = self.owners.get(params["l1_address"])
            if owner is None:
                return httpx.Response(400, json={"code": 21100, "message": "account not found"})
            return httpx.Response(200, text=fx_text(f"accountsByL1Address_{owner}"))
        if endpoint == "account":
            return httpx.Response(
                200, text=fx_text(f"account_{self.accounts[int(params['value'])]}")
            )
        if endpoint == "orderBookDetails":
            return httpx.Response(200, text=fx_text("orderBookDetails"))
        if endpoint == "fundings":
            rows = self.fundings[int(params["market_id"])]
            end, back = int(params["end_timestamp"]), int(params["count_back"])
            served = [r for r in rows if int(r["timestamp"]) <= end][-back:]
            return httpx.Response(200, json={"code": 200, "resolution": "1h", "fundings": served})
        raise AssertionError(f"unexpected request {path} {params}")


def raw_rows(name: str) -> list[dict]:
    return json.loads(fx_text(name))


def fake_760300() -> FakeLighter:
    fake = FakeLighter()
    fake.logs[760300] = raw_rows("explorer_logs_760300_offset0") + raw_rows(
        "explorer_logs_760300_offset100"
    )
    for market in (1, 7, 186):
        fake.fundings[market] = json.loads(fx_text(f"fundings_{market}"))["fundings"]
    return fake


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(lighter, "_now", lambda: CAPTURED)


def run_pages(fetch, cursor=None, limit=200):
    """Follow next_cursor to the end of a pass: (items, pages, resume)."""
    items, pages = [], 0
    while True:
        page = fetch(cursor)
        pages += 1
        items.extend(page.items)
        if page.next_cursor is None:
            return items, pages, page.resume_cursor
        assert pages < limit
        cursor = page.next_cursor


def connector(address: str = ADDR_B) -> LighterConnector:
    return LighterConnector(address, client=httpx.Client())


def usdc_total(legs) -> Decimal:
    return sum(
        (from_atoms(leg.delta_atoms, leg.decimals) for leg in legs if leg.asset == "USDC"),
        Decimal(0),
    )


# ---- profile and registry --------------------------------------------------------------------


def test_profile_is_watch_only_with_two_ip_budgets():
    p = get_profile("lighter")
    assert p.display_name == "Lighter" and p.envs == ("prod",)
    assert p.credential.value == "address" and p.secret_label is None
    assert p.key_label == "Wallet address" and "sign" in p.key_help
    assert p.readonly_proof is ReadOnlyProof.NONE and p.ownership.value == "signed_message"
    assert p.balance_observable and p.transfers_observable and p.history_depth == "all"
    assert (p.tick_micros, p.contract_step_micros, p.fee_rounding_micros) == (1, 1, 1)
    assert p.instrument.value == "asset" and p.paper_fee_model.value == "bps_of_notional"
    assert p.asset_classes == ("crypto",) and p.quote_asset == "USDC"
    assert p.requires_partner_approval and p.tos_doc == "docs/venues/lighter.md"
    assert p.fee_rate_bps == 3  # Premium taker 2.8 bps, rounded up
    assert p.leaderboard_periods == ("day", "week", "month", "all")
    assert p.stream_mode is StreamMode.NUDGE and p.stream_users_per_ip == 200
    assert p.address_kind.value == "evm"
    api, explorer = p.rate_budgets
    assert api is API_BUDGET and explorer is EXPLORER_BUDGET
    assert api.scope is RateScope.IP and explorer.scope is RateScope.IP
    assert (api.capacity, api.refill_per_minute) == (60, 60)
    assert (explorer.capacity, explorer.refill_per_minute) == (90, 90)
    # Each host is charged only to its own bucket (cost 0 elsewhere).
    assert api.cost("explorer_logs") == 0 and api.cost("pnlLeaderboard") == 1
    assert explorer.cost("explorer_logs") == 2 and explorer.cost("fundings") == 0


def test_registry_builds_with_a_lowercased_address_and_refuses_junk():
    conn = build_connector("lighter", "prod", "0x00000000000000000000000000000000000000B2")
    assert isinstance(conn, LighterConnector) and conn.address == ADDR_B
    assert isinstance(build_market_data("lighter", "prod"), lighter.LighterMarketData)
    with pytest.raises(ValueError):
        build_connector("lighter", "prod", "not-an-address")
    with pytest.raises(ValueError):
        build_connector("lighter", "testnet", ADDR_B)


def test_only_the_read_only_websocket_url_exists():
    assert WS_URL == "wss://mainnet.zklighter.elliot.ai/stream?readonly=true"
    assert connector().stream_source().url() == WS_URL


# ---- transport -------------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("status", [429, 405])
def test_rate_limit_statuses_raise_rate_limited(status):
    fake = FakeLighter()
    fake.status["accountsByL1Address"] = status
    respx.route().mock(side_effect=fake)
    with pytest.raises(RateLimited) as exc:
        connector().account_indexes()
    assert exc.value.retry_after_seconds == 7.0 and exc.value.endpoint == "accountsByL1Address"


@respx.mock
def test_throttle_names_each_host_endpoint_before_the_request():
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    seen = []

    class Throttle:
        def before(self, endpoint):
            seen.append((endpoint, len(fake.calls)))

        def after(self, endpoint, extra_cost=0, used=None):
            pass

    conn = connector()
    conn.set_throttle(Throttle())
    page = conn.fetch_asset_fills_page(None)
    conn.fetch_asset_fills_page(page.next_cursor)
    assert seen == [("accountsByL1Address", 0), ("explorer_logs", 1)]


# ---- identity, proofs ------------------------------------------------------------------------


@respx.mock
def test_unknown_address_has_no_indexes_and_fails_check_connection():
    respx.route().mock(side_effect=FakeLighter())
    conn = connector(UNKNOWN)
    assert conn.account_indexes() == []
    with pytest.raises(LighterError):
        conn.check_connection()


def test_prove_read_only_holds_nothing():
    evidence = connector().prove_read_only()
    assert evidence.method is ReadOnlyProof.NONE and evidence.read_only is True
    assert evidence.details["can_trade"] is False and evidence.details["address"] == ADDR_B
    assert connector().check_write_scope() is False


def test_claim_is_an_eip191_signature_by_the_address_itself():
    from eth_account import Account
    from eth_account.messages import encode_defunct

    owner = Account.create("koine lighter test")  # generated here; nothing stored
    other = Account.create("someone else")
    conn = LighterConnector(owner.address)
    code = "koine-1a2b3c4d"
    text = conn.claim_text(code)
    assert text == claim_message(owner.address.lower(), code)
    assert "Venue: Lighter" in text and f"Address: {owner.address.lower()}" in text
    good = owner.sign_message(encode_defunct(text=text)).signature.hex()
    bad = other.sign_message(encode_defunct(text=text)).signature.hex()
    assert conn.verify_claim(conn.claim_payload(code, good)) is True
    assert conn.verify_claim(encode_claim_proof(code, bad)) is False
    assert conn.verify_claim(encode_claim_proof("another-code", good)) is False
    assert conn.verify_claim(code) is False  # no signature yet
    assert conn.verify_claim(encode_claim_proof(code, "0xdeadbeef")) is False


# ---- fills: the explorer page of 714710 ------------------------------------------------------


@respx.mock
def test_every_trade_row_is_a_fill_whatever_its_status():
    """explorer_logs_714710: 100 Trade rows, 43 of them "nothing_to_execute";
    each one matched a REST trade field for field (README), so none is
    filtered. Served as the index's whole history: a full first page, so the
    walk locates the end (offset 100 is empty, 50 holds the last 50 rows)."""
    fake = FakeLighter()
    fake.logs[714710] = raw_rows("explorer_logs_714710")
    respx.route().mock(side_effect=fake)
    fills, _, resume = run_pages(connector(ADDR_A).fetch_asset_fills_page)
    statuses = {r["status"] for r in fake.logs[714710]}
    assert statuses == {"executed", "nothing_to_execute"}
    assert len(fills) == 100 and resume is not None
    assert len({f.venue_fill_id for f in fills}) == 100
    assert all(f.venue_fill_id.startswith("714710:") for f in fills)
    assert [f.filled_at for f in fills] == sorted(f.filled_at for f in fills)


def _seeded_lit_book() -> dict:
    """Index 714710's LIT-perp book just before the page's first perp trade:
    the `*_position_size_before` / `*_entry_quote_before` of the REST row with
    the same tx hash (the explorer page starts mid-history)."""
    rest = {r["tx_hash"]: r for r in fx("trades_714710_perps")}
    rows = list(reversed(raw_rows("explorer_logs_714710")))
    first = next(r for r in rows if r["pubdata"]["trade_pubdata"]["market_index"] == 120)
    before = rest[first["hash"]]
    role = "taker" if (before["ask_account_id"] == 714710) != before["is_maker_ask"] else "maker"
    pos = int(Decimal(before[f"{role}_position_size_before"]) * 100)
    entry = int(Decimal(before[f"{role}_entry_quote_before"]) * 1_000_000)
    return {120: (pos, entry)}


@respx.mock
def test_derived_pnl_on_explorer_fills_matches_rest_on_overlapping_rows():
    """The page's 15 LIT-perp trades (the overlap with the REST capture)
    replayed from the REST state before the first one: each REALIZED_PNL leg
    equals Lighter's own *_account_pnl, to the micro."""
    respx.route().mock(side_effect=FakeLighter())
    conn = connector(ADDR_A)
    rest = {r["tx_hash"]: r for r in fx("trades_714710_perps")}
    book = _seeded_lit_book()
    rows = list(reversed(raw_rows("explorer_logs_714710")))
    checked = 0
    for ref, row in zip(lighter.row_refs(714710, rows), rows, strict=True):
        fill = conn._fill(714710, ref, row, book)
        if fill.instrument.symbol != "LIT":
            continue
        r = rest[row["hash"]]
        venue = r.get("ask_account_pnl" if r["ask_account_id"] == 714710 else "bid_account_pnl")
        pnl = sum(leg.delta_atoms for leg in fill.legs if leg.kind is LegKind.REALIZED_PNL)
        assert pnl == int(Decimal(venue) * 1_000_000), row["hash"]
        checked += 1
    assert checked == 15


@respx.mock
def test_perp_fill_legs_and_exact_fee():
    """First LIT-perp row of the page: 714710 is the maker on the bid
    (taker 644481 sells 13.82 @ 3.6179), maker fee 28 per million.

      notional = 13.82 x 3.6179 = 49.999378
      fee      = 49.999378 x 28 / 1e6 = 0.001399982584 (exact at 12 dp)
      FILL_BASE +1,382 LIT-PERP atoms (2 dp); REALIZED_PNL from the replay."""
    respx.route().mock(side_effect=FakeLighter())
    conn = connector(ADDR_A)
    rows = list(reversed(raw_rows("explorer_logs_714710")))
    row = next(r for r in rows if r["pubdata"]["trade_pubdata"]["market_index"] == 120)
    fill = conn._fill(714710, "714710:" + row["hash"], row, _seeded_lit_book())
    assert fill.action is Action.BUY and fill.liquidity == "maker"
    assert fill.price == "3.6179" and fill.qty_atoms == 1_382
    assert fill.instrument.base_asset == "LIT-PERP" and fill.instrument.copyable
    legs = {leg.kind: leg for leg in fill.legs}
    assert (legs[LegKind.FILL_BASE].delta_atoms, legs[LegKind.FILL_BASE].decimals) == (1_382, 2)
    assert (legs[LegKind.FEE].asset, legs[LegKind.FEE].delta_atoms) == ("USDC", -1_399_982_584)
    assert legs[LegKind.FEE].decimals == 12
    assert legs[LegKind.REALIZED_PNL].asset == "USDC" and legs[LegKind.REALIZED_PNL].decimals == 6
    assert LegKind.FILL_QUOTE not in legs


@respx.mock
def test_spot_sell_pays_quote_and_fee_in_usdc():
    """A LIT/USDC row where 714710 is the taker selling (is_taker_ask 1),
    taker fee 196: FILL_BASE -size LIT, FILL_QUOTE +notional USDC, FEE
    -notional x 196 at 12 dp in USDC (what the sale delivers)."""
    respx.route().mock(side_effect=FakeLighter())
    conn = connector(ADDR_A)
    rows = list(reversed(raw_rows("explorer_logs_714710")))
    row = next(
        r
        for r in rows
        if r["pubdata"]["trade_pubdata"]["market_index"] == 2049
        and r["pubdata"]["trade_pubdata"]["taker_account_index"] == "714710"
        and r["pubdata"]["trade_pubdata"]["is_taker_ask"] == 1
        and r["pubdata"]["trade_pubdata"]["taker_fee"]
    )
    pd = row["pubdata"]["trade_pubdata"]
    fill = conn._fill(714710, "714710:" + row["hash"], row, {})
    qty = int(Decimal(pd["size"]) * 100)
    notional = int(Decimal(pd["size"]) * Decimal(pd["price"]) * 1_000_000)
    legs = {leg.kind: leg for leg in fill.legs}
    assert fill.action is Action.SELL and fill.liquidity == "taker"
    assert fill.instrument.type.value == "spot" and fill.instrument.base_asset == "LIT"
    assert (legs[LegKind.FILL_BASE].asset, legs[LegKind.FILL_BASE].delta_atoms) == ("LIT", -qty)
    assert (legs[LegKind.FILL_QUOTE].asset, legs[LegKind.FILL_QUOTE].delta_atoms) == (
        "USDC",
        notional,
    )
    assert legs[LegKind.FEE].asset == "USDC"
    assert legs[LegKind.FEE].delta_atoms == -notional * pd["taker_fee"]
    assert legs[LegKind.FEE].decimals == 12


@respx.mock
def test_spot_buy_pays_its_fee_in_the_base_asset():
    """Synthetic row (no captured spot buy by 714710 carried a fee): buys 2.50
    LIT/USDC @ 3.6000 as taker, fee 196.

      notional = 9.000000 USDC; fee in LIT = 2.50 x 196 / 1e6 = 0.00049 LIT
      = 250 x 196 = 49,000 atoms at 2 + 6 = 8 dp."""
    respx.route().mock(side_effect=FakeLighter())
    row = {
        "hash": "synthetic-spot-buy",
        "time": "2026-10-22T12:00:00Z",
        "pubdata_type": "Trade",
        "status": "executed",
        "pubdata": {
            "trade_pubdata": {
                "trade_type": 0,
                "market_index": 2049,
                "is_taker_ask": 0,
                "maker_fee": 28,
                "taker_fee": 196,
                "taker_account_index": "714710",
                "maker_account_index": "42",
                "price": "3.6000",
                "size": "2.50",
            }
        },
    }
    fill = connector(ADDR_A)._fill(714710, "714710:synthetic-spot-buy", row, {})
    legs = {leg.kind: leg for leg in fill.legs}
    assert legs[LegKind.FILL_BASE].delta_atoms == 250
    assert legs[LegKind.FILL_QUOTE].delta_atoms == -9_000_000
    assert (legs[LegKind.FEE].asset, legs[LegKind.FEE].delta_atoms) == ("LIT", -49_000)
    assert legs[LegKind.FEE].decimals == 8


@respx.mock
def test_liquidation_trades_are_fills_with_the_liquidation_fee():
    """LiquidationTrade rows are fills. 714710 is the maker (fee 28). The
    liquidated side pays the 1% liquidation fee (taker_fee 10000): seen from
    the taker index 281474976531464, selling 0.1441 ETH @ 2477.12:
      notional 356.952992; fee 356.952992 x 10000 / 1e6 = 3.56952992."""
    respx.route().mock(side_effect=FakeLighter())
    rows = raw_rows("explorer_logs_LiquidationTrade")
    maker_fill = connector(ADDR_A)._fill(714710, "714710:" + rows[0]["hash"], rows[0], {})
    assert maker_fill.liquidity == "maker" and maker_fill.action is Action.BUY
    taker = 281474976531464
    fill = connector(ADDR_A)._fill(taker, f"{taker}:" + rows[0]["hash"], rows[0], {})
    fee = next(leg for leg in fill.legs if leg.kind is LegKind.FEE)
    assert fill.action is Action.SELL and fill.liquidity == "taker"
    assert from_atoms(fee.delta_atoms, fee.decimals) == Decimal("-3.56952992")


@respx.mock
def test_non_crypto_markets_are_not_copyable_and_binary_markets_are_flagged():
    respx.route().mock(side_effect=FakeLighter())
    conn = connector(ADDR_A)
    row = {
        "hash": "synthetic",
        "time": "2026-10-22T12:00:00Z",
        "pubdata_type": "Trade",
        "pubdata": {
            "trade_pubdata": {
                "market_index": 113,  # AAPL
                "is_taker_ask": 0,
                "maker_fee": 0,
                "taker_fee": 0,
                "taker_account_index": "714710",
                "maker_account_index": "1",
                "price": "250.000",
                "size": "0.100",
            }
        },
    }
    fill = conn._fill(714710, "714710:synthetic", row, {})
    assert fill.instrument.symbol == "AAPL" and fill.instrument.copyable is False
    row["pubdata"]["trade_pubdata"]["market_index"] = 9999  # not in orderBookDetails
    assert conn._fill(714710, "714710:x", row, {}) is None
    flags = conn.drain_unsupported()
    assert [f.venue_ref for f in flags] == ["714710:x"] and conn.drain_unsupported() == []


# ---- the complete history of 760300 ----------------------------------------------------------


@respx.mock
def test_full_history_walk_locates_genesis_then_walks_forward(frozen):
    """145 rows newest first. Requests: the index list; offset 0 (full, so
    not the whole history); locate offset 100 (45 rows: the oldest page,
    applied); forward offset 10 (anchor at row 90; 90 new rows); offset 0
    (anchor at row 10; 10 new rows; caught up). Every row applied once."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    fills, pages, resume = run_pages(connector().fetch_asset_fills_page)
    offsets = [int(p["offset"]) for path, p in fake.calls if path.startswith("/api/accounts")]
    assert offsets == [0, 100, 10, 0] and pages == 5
    trades = [r for r in fake.logs[760300] if lighter.is_trade(r)]
    assert len(fills) == len(trades) == 138
    assert {f.venue_fill_id for f in fills} == {f"760300:{r['hash']}" for r in trades}
    state = json.loads(resume)
    assert state["p"] == "discover" and state["w"]["760300"]["a"] == fake.logs[760300][0]["hash"]
    # The replayed book is the venue's: BTC long 0.06000 with entry quote
    # 4,927.398000 (= 0.06 x 82,123.3, the account's avg_entry_price), XRP 835
    # (entry 1,088.894635 = 835 x 1.304065), RKLB 60.0000 (entry 3,827.78).
    assert state["r"]["760300"] == {
        "1": [6000, 4_927_398_000],
        "7": [835, 1_088_894_635],
        "186": [600_000, 3_827_780_000],
    }


@respx.mock
def test_rows_arriving_during_a_walk_leave_no_gap_and_no_duplicate(frozen):
    """Three new rows land after the locate request and twelve more after
    the next one: offsets shift under the walk. The overlap absorbs the
    first; the second pushes the anchor out of the page, so the walk asks
    for the page past it and finds it there. Every row is applied exactly
    once (tests/test_lighter_walk.py drives the walk with rows arriving
    before every request)."""
    fake = fake_760300()
    base = fake.logs[760300]
    template = next(r for r in base if lighter.is_trade(r))

    def new_rows(tag: str, n: int) -> list[dict]:
        out = []
        for i in range(n):
            row = json.loads(json.dumps(template))
            row["hash"] = f"{tag}{i:02d}"
            row["time"] = f"2026-10-22T19:{i:02d}:00Z"
            out.append(row)
        return list(reversed(out))  # newest first

    def arrive(index: int, n: int) -> None:
        if n == 3:
            fake.logs[index] = new_rows("a", 3) + fake.logs[index]
        if n == 4:
            fake.logs[index] = new_rows("b", 12) + fake.logs[index]

    fake.on_logs = arrive
    respx.route().mock(side_effect=fake)
    fills, _, _ = run_pages(connector().fetch_asset_fills_page)
    ids = [f.venue_fill_id for f in fills]
    assert len(ids) == len(set(ids)) == 138 + 15
    want = {f"760300:{r['hash']}" for r in fake.logs[760300] if lighter.is_trade(r)}
    assert set(ids) == want


@respx.mock
def test_next_sync_resumes_from_the_anchor_and_seeks_past_many_new_rows(frozen):
    """After a full pass, 150 new rows arrive (more than a page): the next
    pass starts at offset 0, does not find its anchor (the page is newer),
    asks for the next page down (offset 100) where it is, then walks
    forward. Only the new rows come back."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    conn = connector()
    first, _, resume = run_pages(conn.fetch_asset_fills_page)
    template = next(r for r in fake.logs[760300] if lighter.is_trade(r))
    fresh = []
    for i in range(150):
        row = json.loads(json.dumps(template))
        row["hash"] = f"n{i:03d}"
        row["time"] = f"2026-10-23T{i // 60:02d}:{i % 60:02d}:00Z"
        fresh.append(row)
    fake.logs[760300] = list(reversed(fresh)) + fake.logs[760300]
    second, _, _ = run_pages(conn.fetch_asset_fills_page, resume)
    assert [f.venue_fill_id for f in second] == [f"760300:n{i:03d}" for i in range(150)]


@respx.mock
def test_a_restarted_backfill_reproduces_identical_legs(frozen):
    """The replay state rides in the cursor, so a None cursor replays from
    genesis: two independent passes (and one interrupted after three pages
    and resumed by a fresh connector) give the same fills and legs."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    a, _, _ = run_pages(connector().fetch_asset_fills_page)
    b, _, _ = run_pages(connector().fetch_asset_fills_page)
    conn = connector()
    head = []
    cursor = None
    for _ in range(3):
        page = conn.fetch_asset_fills_page(cursor)
        head.extend(page.items)
        cursor = page.next_cursor
    tail, _, _ = run_pages(connector().fetch_asset_fills_page, cursor)
    dump = [f.model_dump() for f in a]
    assert dump == [f.model_dump() for f in b] == [f.model_dump() for f in head + tail]
    ev_a, _, _ = run_pages(connector().fetch_events_page)
    ev_b, _, _ = run_pages(connector().fetch_events_page)
    assert [e.model_dump() for e in ev_a] == [e.model_dump() for e in ev_b]


@respx.mock
def test_full_replay_reconciles_with_the_account(frozen):
    """Fills plus events of 760300's whole history against /account read at
    18:33:29 UTC:

    - positions: BTC-PERP +0.06000, XRP-PERP +835, RKLB-PERP +60.0000, exact;
    - USDC: deposit 2,281.600000 + realized PnL 259.363885 (fees are 0 on a
      Standard account) + derived funding -17.552068 = 2,523.411817 against
      the venue's cash 2,031.137522667860 (cross margin) + 492.274292
      (isolated margin) = 2,523.411814667860. The 0.000003 gap is the
      funding values' 8-dp rounding: at most 0.5e-8 x |position| per hour,
      which over this history bounds the gap by the sum computed below."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    fills, _, _ = run_pages(connector().fetch_asset_fills_page)
    events, _, _ = run_pages(connector().fetch_events_page)
    legs = [leg for f in fills for leg in f.legs] + [leg for e in events for leg in e.legs]
    funding = [e for e in events if e.kind is LegKind.FUNDING]
    deposits = [e for e in events if e.kind is LegKind.DEPOSIT]
    assert [usdc_total(e.legs) for e in deposits] == [Decimal("2281.600000")]
    pnl = sum(
        (
            from_atoms(leg.delta_atoms, leg.decimals)
            for leg in legs
            if leg.kind is LegKind.REALIZED_PNL
        ),
        Decimal(0),
    )
    assert pnl == Decimal("259.363885")
    assert not [leg for leg in legs if leg.kind is LegKind.FEE]
    assert usdc_total([leg for e in funding for leg in e.legs]) == Decimal("-17.552067884540")

    venue = connector().fetch_balances()
    vector = {a.asset: from_atoms(a.atoms, a.decimals) for a in venue.assets}
    assert vector["USDC"] == Decimal("2523.41181466786")
    held: dict[str, Decimal] = {}
    for leg in legs:
        if leg.kind is LegKind.FILL_BASE:
            held[leg.asset] = held.get(leg.asset, Decimal(0)) + from_atoms(
                leg.delta_atoms, leg.decimals
            )
    assert (
        {k: v for k, v in held.items() if v}
        == {k: v for k, v in vector.items() if k.endswith("-PERP")}
        == {"BTC-PERP": Decimal("0.06"), "XRP-PERP": Decimal(835), "RKLB-PERP": Decimal(60)}
    )
    bound = Decimal(0)
    for e in funding:
        market = int(e.venue_ref.split(":")[2])
        size = {1: Decimal("0.06"), 7: Decimal(835), 186: Decimal(60)}[market]
        bound += Decimal("0.000000005") * size * 2  # |position| never exceeded 2x the final
    gap = usdc_total(legs) - vector["USDC"]
    assert gap == Decimal("0.0000024476")
    assert abs(gap) <= bound


@respx.mock
def test_funding_cross_check_against_total_funding_paid_out(frozen):
    """The venue's total_funding_paid_out on the open RKLB position is
    -2.790256. RKLB was never flat after it first opened, so every derived
    RKLB hour counts: 249 hours summing to -2.79035156, a 0.00009556 gap.
    The 8-dp rounding of the hourly values bounds our side at 249 x 60 x
    0.5e-8 = 0.0000747 (the position never exceeded 60), so about 0.00002 is
    Lighter's own rounding of that running total [VERIFY]. The account's cash
    is closer still (the full replay test: 0.0000024 over all markets), so
    the ledger follows the cash. Signs agree: negative is money paid."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    events, _, _ = run_pages(connector().fetch_events_page)
    trades = [r for r in reversed(fake.logs[760300]) if lighter.is_trade(r)]
    rklb = [r for r in trades if r["pubdata"][next(iter(r["pubdata"]))]["market_index"] == 186]
    position, opened = 0, None
    for r in rklb:
        pd = r["pubdata"][next(iter(r["pubdata"]))]
        taker = int(pd["taker_account_index"]) == 760300
        sells = bool(pd["is_taker_ask"]) if taker else not bool(pd["is_taker_ask"])
        qty = Decimal(pd["size"])
        if position == 0:
            opened = ts(r["time"])
        position += -qty if sells else qty
    assert position == 60
    since_open = [
        e
        for e in events
        if e.venue_ref.startswith("funding:760300:186:") and int(e.venue_ref.split(":")[3]) > opened
    ]
    derived = usdc_total([leg for e in since_open for leg in e.legs])
    assert len(since_open) == 249
    assert derived == Decimal("-2.79035156")
    assert abs(derived - Decimal("-2.790256")) <= Decimal("0.0001")


@respx.mock
def test_funding_hours_follow_the_position_held_before_each_hour(frozen):
    """Synthetic BTC history on index 760300 (size 5 dp):
      10:30 buy 0.10000; 12:00:00.000 sell 0.04000 (at the hour: counts after
      it); 13:15 sell 0.06000 (flat). Hourly values, all "long" (longs pay):
      11:00 1.00000000, 12:00 2.00000000, 13:00 0.50000000.
    Funding: 11:00 -0.1 x 1 = -0.1; 12:00 -0.1 x 2 = -0.2 (the 12:00 trade
    is after the hour); 13:00 -0.06 x 0.5 = -0.03; nothing after 13:15."""
    fake = FakeLighter()

    def trade(i: int, when: str, sells: bool, size: str) -> dict:
        return {
            "hash": f"t{i}",
            "time": when,
            "pubdata_type": "Trade",
            "status": None,
            "pubdata": {
                "trade_pubdata": {
                    "trade_type": 0,
                    "market_index": 1,
                    "is_taker_ask": 1 if sells else 0,
                    "maker_fee": 0,
                    "taker_fee": 0,
                    "taker_account_index": "760300",
                    "maker_account_index": "1",
                    "price": "80000.0",
                    "size": size,
                }
            },
        }

    rows = [
        trade(1, "2026-10-22T10:30:00Z", False, "0.10000"),
        trade(2, "2026-10-22T12:00:00.000Z", True, "0.04000"),
        trade(3, "2026-10-22T13:15:00Z", True, "0.06000"),
    ]
    fake.logs[760300] = list(reversed(rows))
    hour = ts("2026-10-22T11:00:00Z")
    fake.fundings[1] = [
        {"timestamp": hour + 3600 * k, "value": v, "rate": "0.0010", "direction": "long"}
        for k, v in enumerate(["1.00000000", "2.00000000", "0.50000000", "9.00000000"])
    ]
    respx.route().mock(side_effect=fake)
    events, _, _ = run_pages(connector().fetch_events_page)
    funding = {e.venue_ref: usdc_total(e.legs) for e in events}
    assert funding == {
        f"funding:760300:1:{hour}": Decimal("-0.1"),
        f"funding:760300:1:{hour + 3600}": Decimal("-0.2"),
        f"funding:760300:1:{hour + 7200}": Decimal("-0.03"),
    }
    assert all(e.legs[0].instrument_symbol == "BTC" for e in events)


@respx.mock
def test_an_open_position_is_funded_up_to_the_last_safe_hour(frozen):
    """A position still open at the end of the walk gets its hours up to
    floor_hour(now - 5 minutes) = 18:00 (now is 18:33:29), and the next pass
    carries on from there without booking an hour twice."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    conn = connector()
    events, _, resume = run_pages(conn.fetch_events_page)
    last = max(int(e.venue_ref.split(":")[3]) for e in events if e.kind is LegKind.FUNDING)
    assert last == ts("2026-10-22T18:00:00Z")
    again, _, _ = run_pages(conn.fetch_events_page, resume)
    assert again == []


# ---- flows -----------------------------------------------------------------------------------


@respx.mock
def test_deposits_withdrawals_and_transfers_map_to_external_flows():
    """714710's flow rows, whatever their status:
    - L1DepositV2: 5 deposits, DEPOSIT legs at 6 dp (999,999.005166 first)
    - WithdrawV2: 10,000,000 and 1,000 out (WITHDRAWAL)
    - L2TransferV2: 3.2567 LIT in from index 742190 (TRANSFER_IN, 8 dp);
      3,000,000 and 1,200 USDC out to index 731033 (TRANSFER_OUT); two moves
      between 714710's own spot and perps routes change nothing."""
    fake = FakeLighter()
    rows = (
        raw_rows("explorer_logs_L1DepositV2")
        + raw_rows("explorer_logs_WithdrawV2")
        + raw_rows("explorer_logs_L2TransferV2")
    )
    fake.logs[714710] = sorted(rows, key=lambda r: r["time"], reverse=True)
    respx.route().mock(side_effect=fake)
    events, _, _ = run_pages(connector(ADDR_A).fetch_events_page)
    by_kind: dict[LegKind, list] = {}
    for e in events:
        by_kind.setdefault(e.kind, []).append(e)
    deposits = sorted(usdc_total(e.legs) for e in by_kind[LegKind.DEPOSIT])
    assert deposits == sorted(
        Decimal(v) for v in ("999999.005166", "10000000", "3999999.8", "3000000.000001", "5000100")
    )
    assert sorted(usdc_total(e.legs) for e in by_kind[LegKind.WITHDRAWAL]) == [
        Decimal(-10_000_000),
        Decimal(-1000),
    ]
    (lit_in,) = by_kind[LegKind.TRANSFER_IN]
    assert [(leg.asset, leg.delta_atoms, leg.decimals) for leg in lit_in.legs] == [
        ("LIT", 325_670_000, 8)
    ]
    assert sorted(usdc_total(e.legs) for e in by_kind[LegKind.TRANSFER_OUT]) == [
        Decimal(-3_000_000),
        Decimal(-1200),
    ]
    assert len(events) == 5 + 2 + 3  # the two same-index route moves book nothing
    assert all(e.venue_ref.startswith("714710:") for e in events)


@respx.mock
def test_transfers_between_the_addresses_own_indexes_are_internal():
    """With 731033 also an index of the address, the two transfers to it
    move nothing (and carry no fee)."""
    fake = FakeLighter()
    fake.logs[714710] = raw_rows("explorer_logs_L2TransferV2")
    respx.route().mock(side_effect=fake)
    conn = connector(ADDR_A)
    own = {714710, 731033}
    rows = list(reversed(fake.logs[714710]))
    events = [
        conn._flow(714710, ref, row, own)
        for ref, row in zip(lighter.row_refs(714710, rows), rows, strict=True)
    ]
    kinds = [e.kind for e in events if e is not None]
    assert kinds == [LegKind.TRANSFER_IN]  # only the LIT from index 742190


@respx.mock
def test_pool_and_staking_moves_are_transfers_and_flag_the_account():
    """L2MintShares (1,721.474978 USDC into the LLP) and L2StakeAssets (72.82022676
    LIT staked) leave the trading account: TRANSFER_OUT, plus a pool_share flag."""
    fake = FakeLighter()
    rows = raw_rows("explorer_logs_pools")
    for row in rows:  # re-home the captured rows on index 760300
        row["pubdata"][next(iter(row["pubdata"]))]["account_index"] = "760300"
    fake.logs[760300] = rows
    respx.route().mock(side_effect=fake)
    conn = connector()
    events, _, _ = run_pages(conn.fetch_events_page)
    assert sorted(
        (e.legs[0].asset, from_atoms(e.legs[0].delta_atoms, e.legs[0].decimals)) for e in events
    ) == [
        ("LIT", Decimal("-72.82022676")),
        ("USDC", Decimal("-1721.474978")),
    ]
    assert all(e.kind is LegKind.TRANSFER_OUT for e in events)
    assert {f.reason for f in conn.drain_unsupported()} == {"pool_share"}


@respx.mock
def test_unknown_explorer_types_are_flagged_and_noops_are_silent():
    fake = FakeLighter()
    fake.logs[760300] = [
        {
            "hash": "u1",
            "time": "2026-10-22T10:00:00Z",
            "pubdata_type": "L2SomethingNew",
            "pubdata": {"something_pubdata": {"account_index": "760300", "amount": "5"}},
        },
        {
            "hash": "u2",
            "time": "2026-10-22T09:00:00Z",
            "pubdata_type": "L2UpdateLeverage",
            "pubdata": {"l2_update_leverage_pubdata": {"account_index": "760300"}},
        },
    ]
    respx.route().mock(side_effect=fake)
    conn = connector()
    events, _, _ = run_pages(conn.fetch_events_page)
    assert events == []
    flags = conn.drain_unsupported()
    assert [(f.venue_ref, f.activity_type) for f in flags] == [("760300:u1", "L2SomethingNew")]


# ---- balances --------------------------------------------------------------------------------


@respx.mock
def test_balances_cash_excludes_unrealized_and_adds_isolated_margin():
    """760300: USDC margin_balance 2,031.13752266786 (12 dp) + the BTC
    position's isolated allocated_margin 492.274292 = 2,523.41181466786.
    Equity = total_asset_value 1,678.303963 (+ 0 spot USDC)."""
    respx.route().mock(side_effect=FakeLighter())
    balances = connector().fetch_balances()
    vector = {a.asset: a for a in balances.assets}
    assert from_atoms(vector["USDC"].atoms, vector["USDC"].decimals) == Decimal("2523.41181466786")
    assert (vector["BTC-PERP"].atoms, vector["BTC-PERP"].decimals) == (6000, 5)
    assert vector["BTC-PERP"].entry_price == "82123.3"
    assert (vector["RKLB-PERP"].atoms, vector["RKLB-PERP"].decimals) == (600_000, 4)
    assert (vector["XRP-PERP"].atoms, vector["XRP-PERP"].decimals) == (835, 0)
    assert balances.equity_usd_micros == 1_678_303_963


@respx.mock
def test_balances_of_a_maker_with_spot_and_shorts():
    """714710: USDC = spot 1,764,951.214390430404 + margin 17,743,905.004747396184
    = 19,508,856.219137826588; LIT spot 1,096,943.73308452; shorts signed
    negative (BTC -201.72972); no equity figure (Lighter values no LIT)."""
    respx.route().mock(side_effect=FakeLighter())
    balances = connector(ADDR_A).fetch_balances()
    vector = {a.asset: a for a in balances.assets}
    assert from_atoms(vector["USDC"].atoms, vector["USDC"].decimals) == Decimal(
        "19508856.219137826588"
    )
    assert from_atoms(vector["LIT"].atoms, vector["LIT"].decimals) == Decimal("1096943.73308452")
    assert (vector["BTC-PERP"].atoms, vector["BTC-PERP"].decimals) == (-20_172_972, 5)
    assert vector["SOL-PERP"].atoms == 1_689_103 and vector["LIT-PERP"].atoms == -69_222_678
    assert sum(1 for a in balances.assets if a.asset.endswith("-PERP")) == 10
    assert balances.equity_usd_micros is None


@respx.mock
def test_composite_address_sums_its_indexes():
    """An address with two indexes (the 714710 and 760300 captures served as
    master and sub-account): USDC adds up, and a perp both hold (BTC) nets
    with no single entry price."""
    fake = FakeLighter()
    combined = {
        "code": 200,
        "l1_address": "0x00000000000000000000000000000000000000A1",
        "sub_accounts": [
            {"account_type": 0, "index": 714710},
            {"account_type": 1, "index": 760300},
            {"account_type": 2, "index": 281474976710654},  # a public pool: left out
        ],
    }
    respx.get(f"{API_URL}/accountsByL1Address").mock(
        return_value=httpx.Response(200, json=combined)
    )
    respx.route().mock(side_effect=fake)
    conn = connector(ADDR_A)
    assert conn.account_indexes() == [714710, 760300]
    vector = {a.asset: a for a in conn.fetch_balances().assets}
    assert from_atoms(vector["USDC"].atoms, vector["USDC"].decimals) == Decimal(
        "19508856.219137826588"
    ) + Decimal("2523.41181466786")
    assert vector["BTC-PERP"].atoms == -20_172_972 + 6000 and vector["BTC-PERP"].entry_price is None


# ---- streaming (NUDGE) -----------------------------------------------------------------------


@respx.mock
def test_stream_subscribes_each_index_and_nudges_on_updates_only():
    respx.route().mock(side_effect=FakeLighter())
    source = connector(ADDR_A).stream_source()
    source.prepare()
    assert source.url() == WS_URL
    assert [json.loads(f) for f in source.subscribe_frames()] == [
        {"type": "subscribe", "channel": "account_all_trades/714710"}
    ]
    assert json.loads(source.ping_frame()) == {"type": "ping"}
    frames = json.loads(fx_text("ws_frames"))
    assert source.parse(json.dumps(frames["connected"])) == []
    assert source.parse(json.dumps(frames["pong"])) == []
    snapshot = frames["subscribed/account_all_trades"]
    assert source.parse(json.dumps(snapshot)) == []  # history: the poller's job
    update = {**snapshot, "type": "update/account_all_trades"}
    (event,) = source.parse(json.dumps(update))
    assert event.nudge and event.fills == [] and event.ref == "account_all_trades:714710"
    assert source.parse(json.dumps({**update, "trades": {}})) == []
    other = {**update, "channel": "account_all_trades:5"}
    assert source.parse(json.dumps(other)) == []
    with pytest.raises(StreamUnavailable):
        source.parse(json.dumps({"code": 20558, "error": {"code": 20558, "message": "restricted"}}))
    with pytest.raises(StreamProtocolError):
        source.parse("not json")
    with pytest.raises(StreamProtocolError):
        source.parse(json.dumps({"error": {"code": 30001, "message": "bad channel"}}))


@respx.mock
def test_stream_for_an_unknown_address_is_unavailable():
    respx.route().mock(side_effect=FakeLighter())
    with pytest.raises(StreamUnavailable):
        connector(UNKNOWN).stream_source().prepare()


# ---- anchoring: what watchlist._track does ---------------------------------------------------


@respx.mock
def test_anchor_check_costs_one_page_and_sees_no_fills(frozen):
    """take_asset_anchor asks for one fills page with since = now - 5 s: on
    Lighter that page is the index list (no fills), so the anchor is taken
    at once; the backfill starts from genesis on its own."""
    from alphamarket.reconcile import take_asset_anchor

    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    anchor = take_asset_anchor(connector(), settle_seconds=0)
    assert anchor.balance_micros == 2_523_411_814  # USDC toward zero, micros
    assert sum(1 for p, _ in fake.calls if p.endswith("accountsByL1Address")) == 2


# ---- funding hours Lighter has not published yet ---------------------------------------------

LAST_HOUR = ts("2026-10-22T18:00:00Z")  # floor_hour(18:33:29 - 5 min)


def _funding_refs(events) -> set[str]:
    return {e.venue_ref for e in events if e.kind is LegKind.FUNDING}


@respx.mock
def test_an_hour_not_yet_published_waits_for_the_next_pass(frozen):
    """The fundings answers end at 17:00, before the 18:00 cutoff: the pass
    books up to 17:00 and keeps 18:00 pending ("fw") instead of dropping it
    (`through` has already moved past it). Once Lighter publishes 18:00 the
    next pass books exactly that hour for the three open positions, and a
    third pass books nothing. The total equals one pass over the full
    answers, so the ledger does not depend on when the sync ran."""
    full = fake_760300()
    respx.route().mock(side_effect=full)
    whole, _, _ = run_pages(connector().fetch_events_page)

    respx.reset()
    fake = fake_760300()
    late = dict(fake.fundings)
    fake.fundings = {m: [r for r in rows if r["timestamp"] < LAST_HOUR] for m, rows in late.items()}
    respx.route().mock(side_effect=fake)
    conn = connector()
    first, _, resume = run_pages(conn.fetch_events_page)
    assert max(int(r.split(":")[3]) for r in _funding_refs(first)) == LAST_HOUR - 3600
    assert {s[3] for s in json.loads(resume)["fw"]} == {LAST_HOUR}

    fake.fundings = late  # 18:00 is out
    second, _, resume = run_pages(conn.fetch_events_page, resume)
    # BTC and RKLB were open just before 18:00 (XRP opened after it)
    assert _funding_refs(second) == {f"funding:760300:{m}:{LAST_HOUR}" for m in (1, 186)}
    third, _, _ = run_pages(conn.fetch_events_page, resume)
    assert third == []
    key = sorted  # the same events, whatever the order of the funding pages
    assert key(e.venue_ref for e in first + second) == key(e.venue_ref for e in whole)
    assert usdc_total([leg for e in first + second for leg in e.legs]) == usdc_total(
        [leg for e in whole for leg in e.legs]
    )


@respx.mock
def test_a_hole_inside_the_published_hours_is_skipped_and_logged(frozen, caplog):
    """An hour missing between published values is an explicit decision: no
    funding is booked for it (Lighter's public record shows none) and a
    warning is logged; reconcile shows any charge Lighter did make."""
    fake = fake_760300()
    respx.route().mock(side_effect=fake)
    whole = _funding_refs(run_pages(connector().fetch_events_page)[0])
    hole = ts("2026-10-20T07:00:00Z")
    assert f"funding:760300:1:{hole}" in whole  # BTC was open then
    fake.fundings[1] = [r for r in fake.fundings[1] if r["timestamp"] != hole]
    with caplog.at_level("WARNING", logger="alphamarket.connectors.lighter"):
        events, _, resume = run_pages(connector().fetch_events_page)
    assert _funding_refs(events) == whole - {f"funding:760300:1:{hole}"}
    assert "unpublished hours" in caplog.text
    assert not json.loads(resume).get("fw")  # nothing waits: the hour is skipped


def test_back_to_back_segments_of_one_position_merge():
    h = 3600
    segments = [
        [1, 7, 5, 3 * h, 3 * h],
        [1, 7, 5, h, 2 * h],  # joins the one above: 1h..3h
        [1, 7, 6, 4 * h, 4 * h],  # another position
        [2, 7, 5, 4 * h, 5 * h],  # another index
    ]
    assert lighter.merge_segments(segments) == [
        [1, 7, 5, h, 3 * h],
        [1, 7, 6, 4 * h, 4 * h],
        [2, 7, 5, 4 * h, 5 * h],
    ]


# ---- statuses, shapes and hashes the captures do not cover -----------------------------------


def _trade_row(hash_: str, status, market: int = 1) -> dict:
    return {
        "hash": hash_,
        "time": "2026-10-22T10:00:00Z",
        "pubdata_type": "Trade",
        "status": status,
        "pubdata": {
            "trade_pubdata": {
                "market_index": market,
                "is_taker_ask": 0,
                "maker_fee": 0,
                "taker_fee": 0,
                "taker_account_index": "760300",
                "maker_account_index": "1",
                "price": "80000.0",
                "size": "0.01000",
            }
        },
    }


@respx.mock
def test_only_known_statuses_are_booked(frozen):
    """executed, nothing_to_execute, committed, verified and null are in the
    account's state (README); an unknown status ("failed") is flagged and
    neither stream books it."""
    fake = FakeLighter()
    fake.fundings[1] = []  # nothing published: the open BTC hours wait
    statuses = ["executed", "nothing_to_execute", "committed", "verified", None, "failed"]
    fake.logs[760300] = [_trade_row(f"s{i}", s) for i, s in enumerate(statuses)]
    respx.route().mock(side_effect=fake)
    conn = connector()
    fills, _, _ = run_pages(conn.fetch_asset_fills_page)
    assert [f.venue_fill_id for f in fills] == [f"760300:s{i}" for i in reversed(range(5))]
    flags = conn.drain_unsupported()
    assert [(f.venue_ref, f.description) for f in flags] == [
        ("760300:s5", "explorer status 'failed'")
    ]
    run_pages(conn.fetch_events_page)
    assert [f.venue_ref for f in conn.drain_unsupported()] == ["760300:s5"]


@respx.mock
def test_a_trade_like_row_in_another_shape_is_flagged_by_the_fills_stream():
    """A Deleverage (not captured) without trade pubdata would move the
    position: the fills stream flags it instead of skipping it silently."""
    fake = FakeLighter()
    fake.logs[760300] = [
        {
            "hash": "d1",
            "time": "2026-10-22T10:00:00Z",
            "pubdata_type": "Deleverage",
            "status": "executed",
            "pubdata": {"deleverage_pubdata": {"market_index": 1, "size": "0.01000"}},
        }
    ]
    respx.route().mock(side_effect=fake)
    conn = connector()
    fills, _, _ = run_pages(conn.fetch_asset_fills_page)
    assert fills == []
    assert [(f.venue_ref, f.activity_type) for f in conn.drain_unsupported()] == [
        ("760300:d1", "Deleverage")
    ]


@respx.mock
def test_a_market_finer_than_six_dp_is_flagged_not_raised():
    """size_decimals + price_decimals > 6 would make price x size finer than
    a micro: every trade of that market is flagged and the sync goes on."""
    import dataclasses

    respx.route().mock(side_effect=FakeLighter())
    conn = connector()
    btc = conn.markets()[1]
    conn.markets()[1] = dataclasses.replace(btc, price_decimals=btc.price_decimals + 1)
    assert conn._fill(760300, "760300:f", _trade_row("f", "executed"), {}) is None
    (flag,) = conn.drain_unsupported()
    assert flag.venue_ref == "760300:f" and "finer than 6 dp" in flag.description


@respx.mock
def test_unstake_and_burn_rows_are_flagged_with_no_legs():
    """The way back from a pool or staking is not captured, and one unstake
    may log a request row and an executed-unlock row: booking both would
    count the money twice. So these are flagged only [VERIFY]."""
    fake = FakeLighter()
    kinds = ["L2UnstakeAssets", "ExecutedPendingUnlock", "BurnedShares", "L2BurnShares"]
    fake.logs[760300] = [
        {
            "hash": f"p{i}",
            "time": f"2026-10-22T1{i}:00:00Z",
            "pubdata_type": kind,
            "status": "executed",
            "pubdata": {"x_pubdata": {"account_index": "760300", "lit_amount": "5.0"}},
        }
        for i, kind in enumerate(kinds)
    ]
    respx.route().mock(side_effect=fake)
    conn = connector()
    events, _, _ = run_pages(conn.fetch_events_page)
    assert events == []
    flags = conn.drain_unsupported()
    assert sorted(f.activity_type for f in flags) == sorted(kinds)
    assert {f.reason for f in flags} == {"pool_share"}


@respx.mock
def test_a_sub_account_created_during_the_pass_is_internal():
    """The pass lists [714710]; by the time the walk meets the transfers to
    731033, it is the address's new sub-account. The first unknown index
    re-lists the address once: the transfers to 731033 move nothing, the
    LIT from 742190 (someone else) still comes in, and 731033 is walked in
    the same pass. One re-list per pass, not one per transfer."""
    fake = FakeLighter()
    fake.logs[714710] = raw_rows("explorer_logs_L2TransferV2")
    fake.logs[731033] = []
    master = json.loads(fx_text("accountsByL1Address_714710"))
    grown = json.loads(json.dumps(master))
    grown["sub_accounts"].append({"account_type": 1, "index": 731033})
    route = respx.get(f"{API_URL}/accountsByL1Address").mock(
        side_effect=[httpx.Response(200, json=master), httpx.Response(200, json=grown)]
    )
    respx.route().mock(side_effect=fake)
    events, _, resume = run_pages(connector(ADDR_A).fetch_events_page)
    assert [(e.kind, e.legs[0].asset) for e in events] == [(LegKind.TRANSFER_IN, "LIT")]
    assert route.call_count == 2
    assert [p for p, _ in fake.calls if p.startswith("/api/accounts/")] == [
        "/api/accounts/714710/logs",
        "/api/accounts/731033/logs",
    ]
    assert json.loads(resume)["ix"] == [714710, 731033]


@respx.mock
def test_a_frame_with_a_null_error_is_not_an_error():
    respx.route().mock(side_effect=FakeLighter())
    source = connector(ADDR_A).stream_source()
    source.prepare()
    snapshot = json.loads(fx_text("ws_frames"))["subscribed/account_all_trades"]
    update = {**snapshot, "type": "update/account_all_trades", "error": None}
    (event,) = source.parse(json.dumps(update))
    assert event.nudge
