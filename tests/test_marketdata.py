"""Public market data (M6): connector mapping against captured payloads,
mark rules, storage and the open-ticker sweep. No live calls (invariant #6)."""

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx

from alphamarket import marketdata
from alphamarket.connectors.base import NormalizedMarket
from alphamarket.connectors.kalshi import KalshiMarketData
from alphamarket.models import (
    Allocation,
    Fill,
    MirrorFill,
    Settlement,
    Strategy,
    User,
    VenueAccount,
)

BASE = "https://external-api.kalshi.com/trade-api/v2"
FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@respx.mock
def test_market_mapping_from_live_payload():
    route = respx.get(f"{BASE}/markets/KXBTCD-26SEP0614-T79699.99").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_market.json"))
    )
    m = KalshiMarketData(BASE).fetch_market("KXBTCD-26SEP0614-T79699.99")
    assert route.called
    # No auth headers on a public endpoint: nothing to leak.
    assert "KALSHI-ACCESS-KEY" not in route.calls[0].request.headers
    assert m.status == "active" and m.result == "" and not m.settled
    assert m.yes_bid_micros == 580_000 and m.yes_ask_micros == 590_000
    assert m.last_price_micros == 580_000
    assert m.yes_mark_micros == 585_000  # midpoint
    assert m.close_time == datetime(2026, 9, 6, 18, 0, tzinfo=UTC)


@respx.mock
def test_settled_market_marks_at_result():
    respx.get(f"{BASE}/markets/KXBTCD-26SEP0613-T88799.99").mock(
        return_value=httpx.Response(200, json=_fixture("kalshi_market_settled.json"))
    )
    m = KalshiMarketData(BASE).fetch_market("KXBTCD-26SEP0613-T88799.99")
    assert m.status == "finalized" and m.result == "no" and m.settled
    assert m.yes_mark_micros == 0


@respx.mock
def test_unknown_market_is_none():
    respx.get(f"{BASE}/markets/NOPE").mock(
        return_value=httpx.Response(404, json={"error": {"code": "not_found"}})
    )
    assert KalshiMarketData(BASE).fetch_market("NOPE") is None


def test_mark_rules():
    def mk(**kw):
        base = dict(
            ticker="T",
            status="active",
            yes_bid_micros=0,
            yes_ask_micros=0,
            last_price_micros=0,
            as_of=T0,
        )
        return NormalizedMarket(**{**base, **kw})

    assert mk(result="yes").yes_mark_micros == 1_000_000
    assert mk(yes_bid_micros=400_000, yes_ask_micros=440_000).yes_mark_micros == 420_000
    assert mk(yes_bid_micros=400_000, last_price_micros=390_000).yes_mark_micros == 390_000
    assert mk().yes_mark_micros is None


class StubSource:
    venue = "kalshi"

    def __init__(self, markets: dict[str, NormalizedMarket]):
        self.markets = markets
        self.requested: list[str] = []

    def fetch_market(self, ticker):
        self.requested.append(ticker)
        return self.markets.get(ticker)


def test_refresh_stores_and_latest_wins(session_factory):
    with session_factory() as db:
        src = StubSource(
            {
                "A": NormalizedMarket(
                    ticker="A",
                    status="active",
                    yes_bid_micros=100_000,
                    yes_ask_micros=120_000,
                    last_price_micros=110_000,
                    as_of=T0,
                )
            }
        )
        assert marketdata.refresh_prices(db, "kalshi", "prod", ["A", "B"], source=src) == 1
        assert src.requested == ["A", "B"]
        assert marketdata.latest_marks(db, "kalshi", "prod", ["A", "B"]) == {"A": 110_000}
        src.markets["A"] = NormalizedMarket(
            ticker="A",
            status="finalized",
            yes_bid_micros=0,
            yes_ask_micros=0,
            last_price_micros=0,
            result="yes",
            as_of=T0,
        )
        marketdata.refresh_prices(db, "kalshi", "prod", ["A"], source=src)
        assert marketdata.latest_marks(db, "kalshi", "prod", ["A"]) == {"A": 1_000_000}
        # Different environment: separate marks.
        assert marketdata.latest_marks(db, "kalshi", "demo", ["A"]) == {}


def test_open_tickers_by_venue(session_factory):
    with session_factory() as db:
        u = User(email="a@x", password_hash="x", display_name="A")
        db.add(u)
        db.flush()
        acct = VenueAccount(user_id=u.id, venue="kalshi", venue_env="prod", key_id="k")
        db.add(acct)
        db.flush()
        s = Strategy(user_id=u.id, venue_account_id=acct.id, name="S", slug="s")
        db.add(s)
        db.flush()
        for ticker in ("OPEN", "DONE"):
            db.add(
                Fill(
                    strategy_id=s.id,
                    venue_fill_id=ticker,
                    ticker=ticker,
                    side="yes",
                    action="buy",
                    count_micros=1,
                    price_micros=1,
                    filled_at=T0,
                )
            )
        db.add(
            Settlement(
                strategy_id=s.id,
                venue_settlement_id="DONE:1",
                ticker="DONE",
                market_result="yes",
                revenue_micros=0,
                settled_at=T0,
            )
        )
        f = User(email="f@x", password_hash="x", display_name="F")
        db.add(f)
        db.flush()
        alloc = Allocation(
            user_id=f.id,
            strategy_id=s.id,
            allocation_micros=1,
            drawdown_limit_bps=1000,
            high_water_micros=1,
        )
        db.add(alloc)
        db.flush()
        # The creator settled DONE but this paper book has not yet: still open.
        db.add(
            MirrorFill(
                allocation_id=alloc.id,
                signal_id=1,
                mode="paper",
                ticker="DONE",
                side="yes",
                action="buy",
                count_micros=1,
                price_micros=1,
                filled_at=T0,
            )
        )
        db.commit()
        assert marketdata.open_tickers_by_venue(db) == {("kalshi", "prod"): {"OPEN", "DONE"}}
