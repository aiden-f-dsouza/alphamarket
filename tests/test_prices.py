"""Asset market data persistence (wave2 w2-market-data): daily closes with the
USD value rule, fallback pricing, rerun cost, budget handling, quotes and
marks, funding rates, corporate actions and Hyperliquid margin tiers.

No live calls (invariant 6): respx mocks the venues' public endpoints with
the recorded fixtures where they exist and hand-built bodies otherwise.
Dates: NOW is 2026-09-24 04:15 UTC (the nightly tick), so "yesterday" is
2026-09-23; a creator anchored 2026-09-21 12:00 needs closes from 2026-09-20
(the day before the anchor, for equity at the first fill)."""

import json
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.orm import Session

from alphamarket import prices, valuation_db
from alphamarket.config import get_settings
from alphamarket.connectors.base import BudgetExhausted, InstrumentType, MarginTier
from alphamarket.connectors.hyperliquid import HyperliquidMarketData
from alphamarket.jobs import Defer
from alphamarket.models import (
    Allocation,
    AnchorBalance,
    AssetFill,
    AssetQuote,
    CorporateActionRow,
    FundingRateRow,
    Instrument,
    MarketDataCoverage,
    MirrorAssetFill,
    MirrorLedgerEntry,
    PriceDaily,
    Strategy,
    User,
    VenueAccount,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 24, 4, 15, tzinfo=UTC)
ANCHOR = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
D20, D21, D22, D23 = (date(2026, 9, 2 + n) for n in (18, 19, 20, 21))

KRAKEN = "https://api.kraken.com/0/public"
HL_INFO = "https://api.hyperliquid.xyz/info"
ALPACA_TRADING = "https://paper-api.alpaca.markets"
ALPACA_DATA = "https://data.alpaca.markets"
CG = "https://api.coingecko.com/api/v3"


def fixture(path: str):
    return json.loads((FIXTURES / path).read_text())


def epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=UTC).timestamp())


# ---- doubles ----------------------------------------------------------------------------------


class StubLimiter:
    """The slice of VenueLimiter prices.py uses, recording what happened."""

    def __init__(self) -> None:
        self.throttled: list[str] = []
        self.limited: list[tuple] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.exhausted = False
        self.breaker = None

    def breaker_open_until(self, venue):
        return self.breaker

    def throttle(self, venue, credential_id):
        self.throttled.append(venue)
        owner = self

        class _Throttle:
            def before(self, endpoint):
                if owner.exhausted:
                    raise BudgetExhausted(7.0, endpoint)

            def after(self, endpoint, extra_cost=0, used=None):
                pass

        return _Throttle()

    def on_rate_limited(self, venue, credential_id, retry_after_seconds, endpoint=""):
        self.limited.append((venue, credential_id, retry_after_seconds, endpoint))

    def record_success(self, venue):
        self.successes.append(venue)

    def record_failure(self, venue):
        self.failures.append(venue)


def ctx(db, venue, now=NOW, limiter=None):
    return SimpleNamespace(
        db=db, job=SimpleNamespace(payload={"venue": venue}), limiter=limiter, clock=lambda: now
    )


# ---- seeding ----------------------------------------------------------------------------------


def make_user(db) -> User:
    user = User(email=f"{uuid.uuid4().hex}@example.com", password_hash="x", display_name="U")
    db.add(user)
    db.flush()
    return user


def make_account(db, venue, anchors, env="prod", anchor_at=ANCHOR, key="k") -> VenueAccount:
    """A creator account with one strategy and an anchor vector
    {asset: (atoms, decimals)}."""
    user = make_user(db)
    account = VenueAccount(
        user_id=user.id, venue=venue, venue_env=env, key_id=f"{key}-{user.id}", anchor_at=anchor_at
    )
    db.add(account)
    db.flush()
    db.add(Strategy(user_id=user.id, venue_account_id=account.id, name=venue, slug=f"s-{user.id}"))
    for asset, (atoms, decimals) in anchors.items():
        db.add(
            AnchorBalance(
                venue_account_id=account.id,
                asset=asset,
                atoms=atoms,
                decimals=decimals,
                as_of=anchor_at,
            )
        )
    db.flush()
    return account


def make_allocation(db, account, started_at, held: dict[str, int]) -> Allocation:
    """A follower of the account's strategy holding {asset: atoms} in its mirror ledger."""
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    follower = make_user(db)
    allocation = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=1_000_000_000,
        drawdown_limit_bps=1500,
        high_water_micros=1_000_000_000,
        started_at=started_at,
    )
    db.add(allocation)
    db.flush()
    for n, (asset, atoms) in enumerate(held.items()):
        db.add(
            MirrorLedgerEntry(
                allocation_id=allocation.id,
                asset=asset,
                delta_atoms=atoms,
                decimals=8,
                kind="fill_base",
                ref=f"sig:{n}",
                group_ref=f"g{n}",
                occurred_at=started_at,
            )
        )
    db.flush()
    return allocation


def make_instrument(db, venue, symbol, base, type_=InstrumentType.SPOT, quote="USD"):
    inst = Instrument(
        venue=venue,
        symbol=symbol,
        type=type_,
        base_asset=base,
        quote_asset=quote,
        qty_decimals=8,
    )
    db.add(inst)
    db.flush()
    return inst


def add_fill(db, allocation, inst, filled_at):
    db.add(
        MirrorAssetFill(
            allocation_id=allocation.id,
            reason="signal",
            mode="paper",
            instrument_id=inst.id,
            action="buy",
            qty_atoms=1,
            qty_decimals=8,
            price="1",
            notional_micros=1,
            price_basis="ask",
            filled_at=filled_at,
        )
    )
    db.flush()


# ---- venue mocks ------------------------------------------------------------------------------


def kraken_ohlc(closes_by_pair: dict[str, dict[date, str]]):
    """One OHLC handler for several pairs: bars at each UTC midnight; `last`
    is today's midnight, so every bar before it is committed."""

    def handler(request: httpx.Request) -> httpx.Response:
        pair = request.url.params["pair"]
        bars = [
            [epoch(day), "1", "2", "1", close, "1", "1", 1]
            for day, close in sorted(closes_by_pair.get(pair, {}).items())
        ]
        return httpx.Response(
            200, json={"error": [], "result": {pair: bars, "last": epoch(NOW.date())}}
        )

    return handler


BTC_CLOSES = {D20: "60000", D21: "61234.5", D22: "61500.25", D23: "62000"}
ETH_CLOSES = {D20: "2900", D21: "3000", D22: "3100.5", D23: "3200"}


def mock_kraken(closes=None):
    respx.get(f"{KRAKEN}/AssetPairs").mock(
        return_value=httpx.Response(200, json=fixture("kraken/asset_pairs.json"))
    )
    return respx.get(f"{KRAKEN}/OHLC").mock(
        side_effect=kraken_ohlc(
            closes or {"XXBTZUSD": BTC_CLOSES, "XETHZUSD": ETH_CLOSES, "SOLUSD": {}}
        )
    )


def mock_hyperliquid(closes: dict[date, str] | None = None):
    candles = [
        {
            "t": epoch(day) * 1000,
            "T": epoch(day) * 1000 + 86_399_999,
            "s": "BTC",
            "i": "1d",
            "o": "1",
            "c": close,
            "h": "2",
            "l": "1",
            "v": "1",
            "n": 1,
        }
        for day, close in sorted((closes or {D20: "60000", D21: "61000"}).items())
    ]
    bodies = {
        "meta": fixture("hyperliquid/meta_2026-09-26.json"),
        "spotMeta": fixture("hyperliquid/spotMeta_2026-09-26.json"),
        "candleSnapshot": candles,
        "l2Book": fixture("hyperliquid/l2Book_BTC_2026-09-26.json"),
        "fundingHistory": fixture("hyperliquid/fundingHistory_BTC_2026-09-26.json"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=bodies[json.loads(request.content)["type"]])

    return respx.post(HL_INFO).mock(side_effect=handler)


def alpaca_bar(day: date, close: float) -> dict:
    return {"t": f"{day.isoformat()}T04:00:00Z", "c": close, "o": 1, "h": 2, "l": 1, "v": 1}


@pytest.fixture()
def alpaca_keys(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "alpaca_data_key_id", "data-key")
    monkeypatch.setattr(settings, "alpaca_data_secret_key", "data-secret")


def mock_alpaca(bars: list[dict] | None = None):
    respx.get(f"{ALPACA_TRADING}/v2/assets").mock(
        return_value=httpx.Response(200, json=fixture("alpaca/assets.json"))
    )
    bars = bars if bars is not None else [alpaca_bar(D21, 178.25), alpaca_bar(D22, 180.5)]
    return respx.get(f"{ALPACA_DATA}/v2/stocks/bars").mock(
        return_value=httpx.Response(200, json={"bars": {"AAPL": bars}, "next_page_token": None})
    )


def closes_of(db, venue, symbol) -> dict[date, tuple[str, int | None]]:
    rows = db.execute(
        select(PriceDaily.day, PriceDaily.close, PriceDaily.usd_micros_per_unit)
        .join(Instrument, Instrument.id == PriceDaily.instrument_id)
        .where(Instrument.venue == venue, Instrument.symbol == symbol)
    ).all()
    return {day: (close, usd) for day, close, usd in rows}


def instrument_of(db, venue, symbol) -> Instrument | None:
    return db.scalar(
        select(Instrument).where(Instrument.venue == venue, Instrument.symbol == symbol)
    )


# ---- usd_micros_per_unit ----------------------------------------------------------------------


def test_usd_value_of_a_close():
    # Kraken's 61234.5 USD x 1e6 = 61_234_500_000 micros.
    assert prices.usd_micros_per_unit("61234.5", "USD") == 61_234_500_000
    # A USDT (or USDC) quoted close is valued at par.
    assert prices.usd_micros_per_unit("100.5", "USDT") == 100_500_000
    assert prices.usd_micros_per_unit("0.9998", "USDC") == 999_800
    # A BTC-quoted close is not a USD value; neither is any other quote.
    assert prices.usd_micros_per_unit("0.052", "BTC") is None
    assert prices.usd_micros_per_unit("3000", "EUR") is None


def test_usd_value_is_exact_and_truncates_toward_zero():
    # 61234.56789123 x 1e6 = 61_234_567_891.23 -> 61_234_567_891 (never rounded up)
    assert prices.usd_micros_per_unit("61234.56789123", "USD") == 61_234_567_891
    assert prices.usd_micros_per_unit("1.9999999", "USD") == 1_999_999
    # below one micro-dollar: 4e-7 USD -> 0.4 micros -> 0
    assert prices.usd_micros_per_unit("0.0000004", "USD") == 0
    # scientific notation from a venue is still parsed exactly: 1.2345e-5 -> 12.345 -> 12
    assert prices.usd_micros_per_unit("1.2345e-5", "USD") == 12
    # 28+ significant digits do not pass through a float or a rounded Decimal context
    assert prices.usd_micros_per_unit("1234567890123456.123456789", "USD") == (
        1_234_567_890_123_456_123_456
    )
    with pytest.raises(ValueError):
        prices.usd_micros_per_unit("not-a-number", "USD")


# ---- run_daily: Kraken, fallback, rerun -------------------------------------------------------


@respx.mock
def test_kraken_closes_unknown_coin_and_coingecko_fallback(session_factory):
    mock_kraken()
    cg_route = respx.get(f"{CG}/coins/shiba-inu/market_chart/range").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "coingecko" / "market_chart_range_shiba_daily.json").read_text(),
        )
    )
    with session_factory() as db:
        make_account(
            db,
            "kraken",
            {
                "USD": (5_000_000_000, 6),  # cash: no price needed
                "USDT": (10_000_000, 6),  # stablecoin at par: no price needed
                "BTC": (100_000_000, 8),
                "SHIB": (1_000_000_000_000, 8),  # not on Kraken, in the curated map
                "NOPE": (5, 8),  # on neither: stays unpriced, no search
            },
        )
        db.commit()
        prices.run_daily(ctx(db, "kraken"))

        btc = closes_of(db, "kraken", "XXBTZUSD")
        # From the day before the anchor (09-20) to yesterday (09-23).
        assert sorted(btc) == [D20, D21, D22, D23]
        assert btc[D21] == ("61234.5", 61_234_500_000)  # the plan's exit number
        assert btc[D20] == ("60000", 60_000_000_000)
        inst = instrument_of(db, "kraken", "XXBTZUSD")
        assert (inst.base_asset, inst.quote_asset, inst.type) == ("BTC", "USD", "spot")

        # SHIB: CoinGecko's synthetic instrument, source "coingecko".
        shib = instrument_of(db, "coingecko", "shiba-inu")
        assert (shib.base_asset, shib.quote_asset) == ("SHIB", "USD")
        rows = db.execute(
            select(PriceDaily.day, PriceDaily.close, PriceDaily.source).where(
                PriceDaily.instrument_id == shib.id
            )
        ).all()
        assert {r.day: (r.close, r.source) for r in rows} == {
            D20: ("0.000012345678", "coingecko"),
            D21: ("0.000012", "coingecko"),
        }
        assert cg_route.call_count == 1

        # Unknown coin and stablecoin/cash: no instrument, no row, no CoinGecko search.
        assert instrument_of(db, "coingecko", "NOPE") is None
        assert db.scalar(select(Instrument).where(Instrument.base_asset == "NOPE")) is None
        assert (
            db.scalar(select(Instrument).where(Instrument.base_asset.in_(("USD", "USDT")))) is None
        )
        assert db.scalar(select(func.count(PriceDaily.id))) == 4 + 2

        # The valuation reader sees them: Kraken's close for BTC, no price for NOPE.
        account = db.scalar(select(VenueAccount))
        got = valuation_db.price_inputs(
            db, ["BTC", "SHIB", "NOPE"], D20, D23, valuation_db.pricing_venues(account)
        )
        assert {(p.asset, p.day): p.usd_micros_per_unit for p in got}[("BTC", D21)] == (
            61_234_500_000
        )
        assert {p.asset for p in got} == {"BTC", "SHIB"}


@respx.mock
def test_second_run_makes_no_http_calls_and_next_day_fetches_only_the_tail(session_factory):
    ohlc = mock_kraken()
    respx.get(f"{CG}/coins/shiba-inu/market_chart/range").mock(
        return_value=httpx.Response(
            200,
            text=(FIXTURES / "coingecko" / "market_chart_range_shiba_daily.json").read_text(),
        )
    )
    with session_factory() as db:
        make_account(db, "kraken", {"BTC": (100_000_000, 8), "SHIB": (10**12, 8), "NOPE": (5, 8)})
        db.commit()
        prices.run_daily(ctx(db, "kraken"))
        first_calls = respx.calls.call_count
        assert first_calls >= 3  # asset pairs, one OHLC, one CoinGecko range

        prices.run_daily(ctx(db, "kraken"))
        assert respx.calls.call_count == first_calls  # a rerun costs nothing

        # The next night: only the new day (09-24 closes at tomorrow's midnight) is missing.
        # Kraken now also serves the 09-24 bar; the connector is asked from 09-24 on.
        closes = {"XXBTZUSD": {**BTC_CLOSES, date(2026, 9, 24): "62500"}}
        ohlc.mock(side_effect=kraken_ohlc(closes))
        before = ohlc.call_count
        prices.run_daily(ctx(db, "kraken", now=NOW + timedelta(days=1)))
        assert ohlc.call_count == before + 1
        since = int(ohlc.calls.last.request.url.params["since"])
        assert since == epoch(date(2026, 9, 24)) - 1
        btc = closes_of(db, "kraken", "XXBTZUSD")
        assert sorted(btc) == [D20, D21, D22, D23, date(2026, 9, 24)]
        assert btc[date(2026, 9, 24)] == ("62500", 62_500_000_000)
        assert btc[D21] == ("61234.5", 61_234_500_000)  # existing rows never rewritten


@respx.mock
def test_followers_extend_demand_and_start_before_the_creator_anchor(session_factory):
    ohlc = mock_kraken()
    with session_factory() as db:
        account = make_account(db, "kraken", {"BTC": (100_000_000, 8)})
        # A follower started 2026-09-18 12:00 and holds ETH (creator never did).
        make_allocation(db, account, datetime(2026, 9, 18, 12, tzinfo=UTC), {"ETH": 5_000_000})
        db.commit()
        ohlc.mock(
            side_effect=kraken_ohlc(
                {
                    "XXBTZUSD": BTC_CLOSES,
                    "XETHZUSD": {date(2026, 9, 17) + timedelta(days=n): "3000" for n in range(7)},
                }
            )
        )
        prices.run_daily(ctx(db, "kraken"))
        eth = closes_of(db, "kraken", "XETHZUSD")
        assert min(eth) == date(2026, 9, 17)  # allocation start 09-18, minus one day
        assert max(eth) == D23
        assert min(closes_of(db, "kraken", "XXBTZUSD")) == D20  # creator anchor 09-21, minus one


@respx.mock
def test_job_for_another_venue_ignores_this_venues_accounts(session_factory):
    mock_kraken()
    with session_factory() as db:
        make_account(db, "kraken", {"BTC": (100_000_000, 8)})
        db.commit()
        prices.run_daily(ctx(db, "kalshi"))  # a binary venue prices itself
        prices.run_daily(ctx(db, "hyperliquid"))  # no account is priced by it
        assert respx.calls.call_count == 0
        with pytest.raises(ValueError):
            prices.run_daily(SimpleNamespace(db=db, job=SimpleNamespace(payload={}), limiter=None))


# ---- IBKR via Alpaca, weekends ----------------------------------------------------------------


@respx.mock
def test_ibkr_held_stock_is_priced_through_alpaca(session_factory, alpaca_keys):
    bars = mock_alpaca()
    with session_factory() as db:
        ibkr = make_account(db, "ibkr", {"USD": (5_000_000_000, 6), "AAPL": (10_000_000_000, 9)})
        db.commit()
        # The nightly tick enqueues one job per PRICING venue: alpaca, not ibkr.
        prices.run_daily(ctx(db, "alpaca"))

        aapl = closes_of(db, "alpaca", "AAPL")
        # Alpaca has no bar on Sunday 09-20 (and none yet for 09-23 in this mock).
        assert aapl == {D21: ("178.25", 178_250_000), D22: ("180.5", 180_500_000)}
        assert instrument_of(db, "alpaca", "AAPL").type == InstrumentType.EQUITY
        assert db.scalar(select(Instrument).where(Instrument.venue == "ibkr")) is None
        assert bars.call_count == 1
        sent = bars.calls[0].request
        assert sent.url.params["symbols"] == "AAPL"
        assert sent.url.params["start"] == "2026-09-20"  # day before the anchor

        # The reader the account's valuation uses (pricing venue alpaca) finds them.
        assert valuation_db.pricing_venues(ibkr) == ("alpaca",)
        priced = valuation_db.price_inputs(db, ["AAPL"], None, None, ("alpaca",))
        assert [(p.day, p.usd_micros_per_unit) for p in priced] == [
            (D21, 178_250_000),
            (D22, 180_500_000),
        ]

        # The weekend day without a bar is not asked for again, and neither is anything else.
        calls = respx.calls.call_count
        prices.run_daily(ctx(db, "alpaca"))
        assert respx.calls.call_count == calls


# ---- budget: Defer, partial progress, limiter hand-off -----------------------------------------


@respx.mock
def test_rate_limit_keeps_progress_defers_and_the_rerun_fetches_only_the_rest(session_factory):
    mock_kraken()
    state = {"n": 0}

    def ohlc(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 2:  # the second pair is told to wait
            return httpx.Response(429, headers={"Retry-After": "30"}, text="")
        return kraken_ohlc({"XXBTZUSD": BTC_CLOSES, "XETHZUSD": ETH_CLOSES})(request)

    route = respx.get(f"{KRAKEN}/OHLC").mock(side_effect=ohlc)
    limiter = StubLimiter()
    with session_factory() as db:
        make_account(db, "kraken", {"BTC": (10**8, 8), "ETH": (10**8, 8)})
        db.commit()
        with pytest.raises(Defer) as deferred:
            prices.run_daily(ctx(db, "kraken", limiter=limiter))
        assert deferred.value.retry_after_seconds == 30
        assert limiter.limited == [("kraken", None, 30.0, "OHLC")]  # hand-off to the limiter
        assert "kraken" in limiter.throttled
        # BTC (sorted first) was committed; ETH has its instrument but no closes yet.
        assert sorted(closes_of(db, "kraken", "XXBTZUSD")) == [D20, D21, D22, D23]
        assert closes_of(db, "kraken", "XETHZUSD") == {}

        before = route.call_count
        prices.run_daily(ctx(db, "kraken", limiter=limiter))
        assert route.call_count == before + 1  # only ETH's OHLC
        assert sorted(closes_of(db, "kraken", "XETHZUSD")) == [D20, D21, D22, D23]


@respx.mock
def test_exhausted_budget_and_open_circuit_defer_without_a_request(session_factory):
    mock_kraken()
    limiter = StubLimiter()
    limiter.exhausted = True
    with session_factory() as db:
        make_account(db, "kraken", {"BTC": (10**8, 8)})
        db.commit()
        with pytest.raises(Defer) as deferred:
            prices.run_daily(ctx(db, "kraken", limiter=limiter))
        assert deferred.value.retry_after_seconds == 7.0
        assert respx.calls.call_count == 0  # throttle refused before any HTTP

        limiter.exhausted = False
        limiter.breaker = NOW + timedelta(minutes=10)  # circuit open
        with pytest.raises(Defer):
            prices.run_daily(ctx(db, "kraken", limiter=limiter))
        assert respx.calls.call_count == 0


@respx.mock
def test_a_failing_asset_does_not_block_the_others_but_fails_the_job(session_factory):
    mock_kraken()
    respx.get(f"{KRAKEN}/OHLC").mock(
        side_effect=lambda request: (
            httpx.Response(200, json={"error": ["EGeneral:Internal error"], "result": {}})
            if request.url.params["pair"] == "XXBTZUSD"
            else kraken_ohlc({"XETHZUSD": ETH_CLOSES})(request)
        )
    )
    with session_factory() as db:
        make_account(db, "kraken", {"BTC": (10**8, 8), "ETH": (10**8, 8)})
        db.commit()
        with pytest.raises(prices.PricesDailyError):
            prices.run_daily(ctx(db, "kraken"))
        assert closes_of(db, "kraken", "XXBTZUSD") == {}
        assert sorted(closes_of(db, "kraken", "XETHZUSD")) == [D20, D21, D22, D23]


# ---- Hyperliquid perps and margin tiers ---------------------------------------------------------


@respx.mock
def test_perp_closes_under_its_own_asset_and_margin_tiers(session_factory):
    info = mock_hyperliquid()
    with session_factory() as db:
        make_account(
            db,
            "hyperliquid",
            {"USDC": (1_000_000_000, 8), "BTC-PERP": (1_000, 5)},
        )
        db.commit()
        prices.run_daily(ctx(db, "hyperliquid"))

        perp = instrument_of(db, "hyperliquid", "BTC")
        assert (perp.type, perp.base_asset, perp.quote_asset) == (
            InstrumentType.PERP,
            "BTC-PERP",
            "USDC",
        )
        # USDC-quoted: at par. Closes belong to "BTC-PERP", not to spot "BTC".
        assert closes_of(db, "hyperliquid", "BTC") == {
            D20: ("60000", 60_000_000_000),
            D21: ("61000", 61_000_000_000),
        }
        # Ascending tiers from the meta payload; the lowest allows 40x.
        assert perp.margin_tiers == [
            {"lower_bound": "0.0", "max_leverage": 40},
            {"lower_bound": "150000000.0", "max_leverage": 20},
        ]
        assert perp.margin_tiers_at is not None
        got = valuation_db.price_inputs(db, ["BTC-PERP", "BTC"], None, None, ("hyperliquid",))
        assert {p.asset for p in got} == {"BTC-PERP"}

        calls = info.call_count
        prices.run_daily(ctx(db, "hyperliquid"))
        assert info.call_count == calls  # tiers fresh today, closes covered: nothing asked

        # The next day the tiers are refreshed (and the new day's close fetched).
        later = NOW + timedelta(days=1)
        prices.run_daily(ctx(db, "hyperliquid", now=later))
        db.refresh(perp)
        assert perp.margin_tiers_at.replace(tzinfo=UTC) == later
        assert info.call_count > calls


@respx.mock
def test_margin_tiers_from_the_recorded_meta():
    mock_hyperliquid()
    source = HyperliquidMarketData()
    btc = source.fetch_margin_tiers("BTC")
    assert btc == [MarginTier("0.0", 40), MarginTier("150000000.0", 20)]
    assert btc[0].max_leverage == 40  # lowest tier
    assert source.fetch_margin_tiers("ETH") == [
        MarginTier("0.0", 25),
        MarginTier("100000000.0", 15),
    ]
    # LDO names table 5, which the response does not include: one tier from its maxLeverage.
    assert source.fetch_margin_tiers("LDO") == [MarginTier("0", 5)]
    assert source.fetch_margin_tiers("NOTACOIN") == []
    assert respx.calls.call_count == 1  # `meta` is fetched once and cached


@respx.mock
def test_margin_tiers_sorted_ascending_by_numeric_lower_bound():
    meta = {
        "universe": [{"name": "ABC", "szDecimals": 1, "maxLeverage": 10, "marginTableId": 7}],
        "marginTables": [
            [
                7,
                {
                    "description": "unsorted",
                    "marginTiers": [
                        {"lowerBound": "20000000.0", "maxLeverage": 5},
                        {"lowerBound": "9000000.0", "maxLeverage": 8},
                        {"lowerBound": "0.0", "maxLeverage": 10},
                    ],
                },
            ]
        ],
        "collateralToken": 0,
    }
    respx.post(HL_INFO).mock(return_value=httpx.Response(200, json=meta))
    tiers = HyperliquidMarketData().fetch_margin_tiers("ABC")
    # Numeric, not lexical: "9000000.0" sorts after "20000000.0" as a string.
    assert [t.lower_bound for t in tiers] == ["0.0", "9000000.0", "20000000.0"]
    assert [t.max_leverage for t in tiers] == [10, 8, 5]


@respx.mock
def test_hip3_perp_without_tables_gets_one_tier_from_its_max_leverage():
    respx.post(HL_INFO).mock(
        return_value=httpx.Response(200, json=fixture("hyperliquid/meta_xyz_2026-09-26.json"))
    )
    assert HyperliquidMarketData().fetch_margin_tiers("xyz:RKLB") == [MarginTier("0", 10)]


# ---- capture_quote ----------------------------------------------------------------------------


@respx.mock
def test_capture_quote_stores_the_top_of_book_through_the_limiter(session_factory):
    respx.get(f"{KRAKEN}/Ticker").mock(
        return_value=httpx.Response(200, json=fixture("kraken/ticker_xxbtzusd.json"))
    )
    limiter = StubLimiter()
    with session_factory() as db:
        inst = make_instrument(db, "kraken", "XXBTZUSD", "BTC")
        row = prices.capture_quote(db, "kraken", "prod", inst, limiter)
        assert row is not None and row.id is not None
        assert (row.bid, row.ask, row.last, row.source) == (
            "84261.30000",
            "84261.40000",
            "84261.60000",
            "kraken",
        )
        assert limiter.throttled == ["kraken"]  # throttled as (source venue, no credential)
        stored = db.scalars(select(AssetQuote)).all()
        assert [(q.instrument_id, q.bid) for q in stored] == [(inst.id, "84261.30000")]


@respx.mock
def test_capture_quote_returns_none_on_exhausted_budget_and_stores_nothing(session_factory):
    ticker = respx.get(f"{KRAKEN}/Ticker").mock(
        return_value=httpx.Response(200, json=fixture("kraken/ticker_xxbtzusd.json"))
    )
    limiter = StubLimiter()
    limiter.exhausted = True
    with session_factory() as db:
        inst = make_instrument(db, "kraken", "XXBTZUSD", "BTC")
        assert prices.capture_quote(db, "kraken", "prod", inst, limiter) is None
        assert ticker.call_count == 0
        assert db.scalar(select(func.count(AssetQuote.id))) == 0


@respx.mock
def test_capture_quote_none_on_rate_limit_open_circuit_no_quote_and_errors(session_factory):
    ticker = respx.get(f"{KRAKEN}/Ticker").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "12"})
    )
    limiter = StubLimiter()
    with session_factory() as db:
        inst = make_instrument(db, "kraken", "XXBTZUSD", "BTC")
        assert prices.capture_quote(db, "kraken", "prod", inst, limiter) is None
        assert limiter.limited == [("kraken", None, 12.0, "Ticker")]

        calls = ticker.call_count
        limiter.breaker = NOW  # any non-None value: circuit open
        assert prices.capture_quote(db, "kraken", "prod", inst, limiter) is None
        assert ticker.call_count == calls
        limiter.breaker = None

        ticker.mock(
            return_value=httpx.Response(
                200, json={"error": ["EQuery:Unknown asset pair"], "result": {}}
            )
        )
        assert prices.capture_quote(db, "kraken", "prod", inst, limiter) is None  # no quote

        ticker.mock(return_value=httpx.Response(503))
        assert prices.capture_quote(db, "kraken", "prod", inst, None) is None  # logged, not raised
        assert db.scalar(select(func.count(AssetQuote.id))) == 0


@respx.mock
def test_capture_quote_prices_an_ibkr_instrument_through_alpaca(session_factory, alpaca_keys):
    route = respx.get(f"{ALPACA_DATA}/v2/stocks/quotes/latest").mock(
        return_value=httpx.Response(200, json=fixture("alpaca/latest_quotes.json"))
    )
    limiter = StubLimiter()
    with session_factory() as db:
        inst = make_instrument(db, "ibkr", "AAPL", "AAPL", InstrumentType.EQUITY)
        row = prices.capture_quote(db, "ibkr", "prod", inst, limiter)
        assert row is not None
        assert (row.instrument_id, row.bid, row.ask, row.last, row.source) == (
            inst.id,
            "172.62",
            "172.7",
            None,
            "alpaca",
        )
        assert limiter.throttled == ["alpaca"]  # the budget spent is Alpaca's, not IBKR's
        assert route.calls[0].request.url.params["symbols"] == "AAPL"


# ---- latest_mark ------------------------------------------------------------------------------


def _quote(db, inst, as_of, bid=None, ask=None, last=None, source="kraken"):
    db.add(
        AssetQuote(instrument_id=inst.id, bid=bid, ask=ask, last=last, as_of=as_of, source=source)
    )
    db.flush()


def test_latest_mark_is_the_mid_else_last_within_the_freshness_window(session_factory):
    with session_factory() as db:
        inst = make_instrument(db, "kraken", "XXBTZUSD", "BTC")
        other = make_instrument(db, "kraken", "XETHZUSD", "ETH")
        assert prices.latest_mark(db, inst.id, NOW) is None  # nothing stored

        # Mid of bid 60030 and ask 60990.5 = 60510.25 exactly.
        _quote(db, inst, NOW - timedelta(seconds=60), "60030", "60990.5", "60500")
        mark = prices.latest_mark(db, inst.id, NOW)
        assert (mark.price, mark.basis, mark.source) == ("60510.25", "mid", "kraken")
        assert mark.instrument_id == inst.id and mark.as_of == NOW - timedelta(seconds=60)

        # A newer quote with only a last price wins on freshness, basis "last".
        _quote(db, inst, NOW - timedelta(seconds=10), last="60700.5", source="alpaca")
        mark = prices.latest_mark(db, inst.id, NOW)
        assert (mark.price, mark.basis, mark.source) == ("60700.5", "last", "alpaca")

        # A newer quote with neither side nor last is not a mark: the previous one stands.
        _quote(db, inst, NOW - timedelta(seconds=5), bid="60000")  # bid only, no last
        assert prices.latest_mark(db, inst.id, NOW).price == "60700.5"

        # Marks are per instrument.
        assert prices.latest_mark(db, other.id, NOW) is None


def test_latest_mark_respects_quote_max_age(session_factory):
    max_age = get_settings().quote_max_age_seconds
    assert max_age == 900
    with session_factory() as db:
        inst = make_instrument(db, "kraken", "XXBTZUSD", "BTC")
        _quote(db, inst, NOW - timedelta(seconds=max_age), "100", "102")
        assert prices.latest_mark(db, inst.id, NOW).price == "101"  # exactly at the limit: fresh
        assert prices.latest_mark(db, inst.id, NOW + timedelta(seconds=1)) is None  # 901 s: stale


# ---- refresh_asset_marks ----------------------------------------------------------------------


@respx.mock
def test_refresh_asset_marks_quotes_open_books_and_stores_funding(session_factory):
    hl = mock_hyperliquid()
    ticker = respx.get(f"{KRAKEN}/Ticker").mock(
        return_value=httpx.Response(
            200,
            json={
                "error": [],
                "result": {
                    "XETHZUSD": {
                        "a": ["3001", "1", "1"],
                        "b": ["3000", "1", "1"],
                        "c": ["3000.5", "1"],
                    }
                },
            },
        )
    )
    started = datetime(2026, 9, 24, 0, 0, tzinfo=UTC)
    with session_factory() as db:
        hl_account = make_account(db, "hyperliquid", {"USDC": (10**9, 8)}, key="hl")
        kr_account = make_account(db, "kraken", {"USD": (10**9, 6)}, key="kr")
        perp = make_instrument(db, "hyperliquid", "BTC", "BTC-PERP", InstrumentType.PERP, "USDC")
        eth = make_instrument(db, "kraken", "XETHZUSD", "ETH")
        btc_spot = make_instrument(db, "kraken", "XXBTZUSD", "BTC")
        open_perp = make_allocation(db, hl_account, started, {"BTC-PERP": 5_000})
        open_spot = make_allocation(db, kr_account, started, {"ETH": 40_000_000})
        # A closed position (net zero) and a stopped allocation are not refreshed.
        closed = make_allocation(db, kr_account, started, {"BTC": 1_000})
        db.add(
            MirrorLedgerEntry(
                allocation_id=closed.id,
                asset="BTC",
                delta_atoms=-1_000,
                decimals=8,
                kind="fill_base",
                ref="sig:9",
                group_ref="g9",
                occurred_at=started,
            )
        )
        stopped = make_allocation(db, kr_account, started, {"ETH": 7})
        stopped.status = "stopped"
        for allocation, inst in ((open_perp, perp), (open_spot, eth), (closed, btc_spot)):
            add_fill(db, allocation, inst, started + timedelta(hours=1))
        add_fill(db, stopped, eth, started)
        db.commit()

        written = prices.refresh_asset_marks(db, None)
        assert written == 2  # the BTC perp and ETH; not the closed BTC position
        quotes = {q.instrument_id: q for q in db.scalars(select(AssetQuote)).all()}
        assert set(quotes) == {perp.id, eth.id}
        assert (quotes[perp.id].bid, quotes[perp.id].ask) == ("84284.0", "84285.0")
        assert quotes[perp.id].source == "hyperliquid"
        assert (quotes[eth.id].bid, quotes[eth.id].ask, quotes[eth.id].last) == (
            "3000",
            "3001",
            "3000.5",
        )
        assert ticker.calls[0].request.url.params["pair"] == "XETHZUSD"
        assert prices.latest_mark(db, eth.id, quotes[eth.id].as_of).price == "3000.5"

        # Funding rows from the fixture (6 hourly rates), source = the venue.
        rates = db.scalars(
            select(FundingRateRow).where(FundingRateRow.instrument_id == perp.id)
        ).all()
        assert len(rates) == 6
        assert {r.source for r in rates} == {"hyperliquid"}
        first = min(rates, key=lambda r: r.at)
        assert (first.rate, first.at.replace(tzinfo=UTC)) == (
            "0.0000074391",
            datetime.fromtimestamp(1790380800002 / 1000, UTC),
        )
        # The first fetch starts at the earliest open fill of the instrument.
        fund_body = json.loads(
            [c for c in hl.calls if json.loads(c.request.content)["type"] == "fundingHistory"][
                0
            ].request.content
        )
        assert fund_body["startTime"] == int((started + timedelta(hours=1)).timestamp() * 1000)

        # A second pass adds quotes again but no duplicate funding rows, and the funding
        # request now starts one millisecond after the newest stored rate.
        assert prices.refresh_asset_marks(db, None) == 2
        assert db.scalar(select(func.count(FundingRateRow.id))) == 6
        last_body = json.loads(
            [c for c in hl.calls if json.loads(c.request.content)["type"] == "fundingHistory"][
                -1
            ].request.content
        )
        newest = max(r.at for r in rates)
        assert last_body["startTime"] == int(newest.replace(tzinfo=UTC).timestamp() * 1000) + 1


@respx.mock
def test_refresh_asset_marks_stops_a_blocked_source_and_is_a_noop_without_books(session_factory):
    ticker = respx.get(f"{KRAKEN}/Ticker").mock(return_value=httpx.Response(200, json={}))
    with session_factory() as db:
        assert prices.refresh_asset_marks(db, None) == 0  # no allocations at all
        kr_account = make_account(db, "kraken", {"USD": (10**9, 6)})
        started = datetime(2026, 9, 24, tzinfo=UTC)
        for symbol, base in (("XETHZUSD", "ETH"), ("XXBTZUSD", "BTC")):
            inst = make_instrument(db, "kraken", symbol, base)
            allocation = make_allocation(db, kr_account, started, {base: 10_000})
            add_fill(db, allocation, inst, started)
        db.commit()
        limiter = StubLimiter()
        limiter.exhausted = True
        assert prices.refresh_asset_marks(db, limiter) == 0
        assert ticker.call_count == 0
        # An open circuit also skips the venue without a request.
        limiter.exhausted = False
        limiter.breaker = NOW
        assert prices.refresh_asset_marks(db, limiter) == 0
        assert ticker.call_count == 0


# ---- corporate actions ------------------------------------------------------------------------


@respx.mock
def test_corporate_actions_for_follower_held_equities(session_factory, alpaca_keys):
    mock_alpaca()
    body = {
        "corporate_actions": {
            "forward_splits": [
                {
                    "symbol": "AAPL",
                    "ex_date": "2026-09-30",
                    "new_rate": 2,
                    "old_rate": 1,
                    "payable_date": "2026-09-29",
                },
                {"symbol": "MSFT", "ex_date": "2026-09-30", "new_rate": 3, "old_rate": 1},
            ],
            "cash_dividends": [
                {"symbol": "AAPL", "ex_date": "2026-09-28", "payable_date": None, "rate": 0.25}
            ],
        }
    }
    route = respx.get(f"{ALPACA_DATA}/v1/corporate-actions").mock(
        side_effect=lambda request: httpx.Response(200, json=body)
    )
    with session_factory() as db:
        creator = make_account(db, "alpaca", {"USD": (10**9, 6)}, env="paper")
        make_allocation(
            db, creator, datetime(2026, 9, 22, 10, tzinfo=UTC), {"AAPL": 10_000_000_000}
        )
        db.commit()
        prices.run_daily(ctx(db, "alpaca"))

        assert route.call_count == 1
        params = route.calls[0].request.url.params
        assert params["symbols"] == "AAPL"  # only what a follower holds
        assert params["start"] == "2026-09-21"  # allocation start 09-22, minus one day
        assert params["end"] == "2026-10-24"  # today + 30 days of lookahead
        rows = db.scalars(select(CorporateActionRow).order_by(CorporateActionRow.kind)).all()
        assert [(r.symbol, r.kind, r.ex_date.isoformat()) for r in rows] == [
            ("AAPL", "cash_dividend", "2026-09-28"),
            ("AAPL", "split", "2026-09-30"),
        ]  # MSFT, returned though not asked for, is not stored
        dividend, split = rows
        assert (dividend.source, dividend.cash_per_share, dividend.pay_date) == (
            "alpaca",
            "0.25",
            None,
        )
        assert (split.ratio_new, split.ratio_old, split.pay_date) == (2, 1, date(2026, 9, 29))
        # AAPL is also priced (it is a follower asset).
        assert sorted(closes_of(db, "alpaca", "AAPL")) == [D21, D22]

        calls = respx.calls.call_count
        prices.run_daily(ctx(db, "alpaca"))
        assert respx.calls.call_count == calls  # same UTC day: nothing asked again

        # The next night re-reads the last 14 days (the dividend's pay date is announced),
        # without duplicating rows.
        body["corporate_actions"]["cash_dividends"][0]["payable_date"] = "2026-10-05"
        prices.run_daily(ctx(db, "alpaca", now=NOW + timedelta(days=1)))
        assert route.call_count == 2
        assert route.calls[1].request.url.params["start"] == "2026-09-10"  # 09-24 - 14 days
        assert db.scalar(select(func.count(CorporateActionRow.id))) == 2
        db.refresh(dividend)
        assert dividend.pay_date == date(2026, 10, 5)


@respx.mock
def test_no_follower_no_corporate_action_request(session_factory, alpaca_keys):
    mock_alpaca()
    route = respx.get(f"{ALPACA_DATA}/v1/corporate-actions").mock(
        return_value=httpx.Response(200, json={"corporate_actions": {}})
    )
    with session_factory() as db:
        make_account(db, "alpaca", {"USD": (10**9, 6), "AAPL": (10**9, 9)}, env="paper")
        db.commit()
        prices.run_daily(ctx(db, "alpaca"))  # a creator alone: its own legs carry its actions
        assert route.call_count == 0


# ---- coverage table ---------------------------------------------------------------------------


@respx.mock
def test_coverage_rows_record_what_was_asked(session_factory):
    mock_kraken()
    with session_factory() as db:
        make_account(db, "kraken", {"BTC": (10**8, 8)})
        db.commit()
        prices.run_daily(ctx(db, "kraken"))
        row = db.scalar(select(MarketDataCoverage))
        assert (row.source, row.kind, row.scope) == ("kraken", "closes", "XXBTZUSD")
        assert (row.covered_from, row.covered_through) == (D20, D23)
        assert row.fetched_on == NOW.date()


# ---- Postgres (migration 0014 and the JSON column) ----------------------------------------------


@pytest.mark.pg
@respx.mock
def test_run_daily_on_postgres_matches_the_migrated_schema(pg_url):
    """The migrated schema (market_data_coverage, instruments.margin_tiers JSON, upsert via
    ON CONFLICT) behaves like the SQLite one. Runs in an outer transaction that is rolled
    back, so the shared test database is left untouched."""
    mock_kraken()
    info = mock_hyperliquid()
    engine = create_engine(pg_url)
    try:
        with engine.connect() as conn:
            outer = conn.begin()
            db = Session(
                bind=conn,
                join_transaction_mode="create_savepoint",
                expire_on_commit=False,
                autoflush=False,
            )
            try:
                # Other pg tests share this database and may have left fresh
                # margin tiers or coverage behind, which would make run_daily
                # skip the fetches under test. Start from none (rolled back).
                db.execute(delete(MarketDataCoverage))
                db.execute(update(Instrument).values(margin_tiers=None, margin_tiers_at=None))
                make_account(db, "kraken", {"BTC": (10**8, 8), "NOPE": (5, 8)}, key="pgk")
                make_account(db, "hyperliquid", {"BTC-PERP": (1_000, 5)}, key="pgh")
                db.commit()
                prices.run_daily(ctx(db, "kraken"))
                prices.run_daily(ctx(db, "hyperliquid"))
                assert closes_of(db, "kraken", "XXBTZUSD")[D21] == ("61234.5", 61_234_500_000)
                perp = instrument_of(db, "hyperliquid", "BTC")
                assert perp.margin_tiers[0] == {"lower_bound": "0.0", "max_leverage": 40}
                coverage = db.scalars(
                    select(MarketDataCoverage).where(MarketDataCoverage.kind == "closes")
                ).all()
                assert {c.scope for c in coverage} >= {"XXBTZUSD", "BTC"}

                calls = respx.calls.call_count
                prices.run_daily(ctx(db, "kraken"))
                prices.run_daily(ctx(db, "hyperliquid"))
                assert respx.calls.call_count == calls  # a rerun costs nothing here either
                assert info.call_count >= 1
            finally:
                db.close()
                outer.rollback()
    finally:
        engine.dispose()


# ---- review fixes: perps opened after the anchor, late closes, one source per pass ----------


@respx.mock
def test_perp_opened_after_the_anchor_is_priced(session_factory):
    """A perp position opened after the anchor is not a ledger leg (it lives in
    perp_fills), yet its "BTC-PERP" closes are what values it."""
    mock_hyperliquid()
    with session_factory() as db:
        account = make_account(db, "hyperliquid", {"USDC": (1_000_000_000, 8)})
        perp = make_instrument(db, "hyperliquid", "BTC", "BTC-PERP", InstrumentType.PERP, "USDC")
        db.add(
            AssetFill(
                venue_account_id=account.id,
                instrument_id=perp.id,
                venue_fill_id="f1",
                action="buy",
                qty_atoms=1_000,
                qty_decimals=5,
                price="60500",
                filled_at=ANCHOR + timedelta(hours=1),
            )
        )
        db.commit()
        prices.run_daily(ctx(db, "hyperliquid"))
        assert closes_of(db, "hyperliquid", "BTC") == {
            D20: ("60000", 60_000_000_000),
            D21: ("61000", 61_000_000_000),
        }


@respx.mock
def test_a_recent_close_the_source_has_not_published_is_asked_for_the_next_night(
    session_factory, alpaca_keys
):
    bars = mock_alpaca()  # 09-21 and 09-22 only: 09-23's bar is late
    with session_factory() as db:
        make_account(db, "ibkr", {"USD": (5_000_000_000, 6), "AAPL": (10_000_000_000, 9)})
        db.commit()
        prices.run_daily(ctx(db, "alpaca"))
        row = db.scalar(select(MarketDataCoverage).where(MarketDataCoverage.kind == "closes"))
        assert (row.covered_from, row.covered_through) == (D20, D22)  # 09-23 still missing

        prices.run_daily(ctx(db, "alpaca"))  # same day: nothing asked again
        assert bars.call_count == 1

        bars.mock(
            return_value=httpx.Response(
                200,
                json={
                    "bars": {"AAPL": [alpaca_bar(D23, 181), alpaca_bar(date(2026, 9, 24), 182)]},
                    "next_page_token": None,
                },
            )
        )
        prices.run_daily(ctx(db, "alpaca", now=NOW + timedelta(days=1)))
        assert bars.call_count == 2  # one request covers the late day and the new one
        assert bars.calls.last.request.url.params["start"] == "2026-09-23"
        aapl = closes_of(db, "alpaca", "AAPL")
        assert aapl[D23] == ("181", 181_000_000)
        assert aapl[date(2026, 9, 24)] == ("182", 182_000_000)


def test_fetch_window_skips_a_tail_already_asked_today():
    y = D23
    assert prices._fetch_window(None, D20, y) == (D20, y)
    assert prices._fetch_window((D20, D21), D20, y) == (D22, y)
    assert prices._fetch_window((D20, D21), D20, y, fetched_today=True) is None
    # A new, older first day is still fetched the same day (the head only).
    assert prices._fetch_window((D21, D21), D20, y, fetched_today=True) == (D20, D20)
    assert prices._fetch_window((D20, y), D20, y) is None
    assert prices._fetch_window(None, NOW.date(), y) is None  # first day after yesterday


@respx.mock
def test_refresh_asset_marks_builds_one_source_per_venue(session_factory):
    def ticker(request: httpx.Request) -> httpx.Response:
        pair = request.url.params["pair"]
        book = {"a": ["11", "1", "1"], "b": ["10", "1", "1"], "c": ["10.5", "1"]}
        return httpx.Response(200, json={"error": [], "result": {pair: book}})

    respx.get(f"{KRAKEN}/Ticker").mock(side_effect=ticker)
    limiter = StubLimiter()
    with session_factory() as db:
        kr_account = make_account(db, "kraken", {"USD": (10**9, 6)})
        started = datetime(2026, 9, 24, tzinfo=UTC)
        for symbol, base in (("XETHZUSD", "ETH"), ("XXBTZUSD", "BTC")):
            inst = make_instrument(db, "kraken", symbol, base)
            allocation = make_allocation(db, kr_account, started, {base: 10_000})
            add_fill(db, allocation, inst, started)
        db.commit()
        assert prices.refresh_asset_marks(db, limiter) == 2
        assert limiter.throttled == ["kraken"]  # one throttled source for both instruments
