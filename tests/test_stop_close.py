"""A stop closes the paper book (decisions 8 and 9; paper_lifecycle.close_stopped,
methodology "When a tail stops").

Every stop of a crypto/stock tail sells what it holds: spot and shares at the
bid, a long perp at the bid, a short perp at the ask, from the freshest quote
no older than quote_max_age_seconds (15 min), with the fee a normal paper fill
pays. With no fresh quote the instrument waits (the tail shows "Closing");
it never closes at a stale mark or a last price. Prediction-market tails run
to settlement.

Expected values are worked by hand in the comments (USD; M = 1,000,000
micros)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from alphamarket import paper, paper_book, prices, signals
from alphamarket.connectors.base import Action, InstrumentType, LegKind, RegulatoryFees
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import aware
from alphamarket.mirror import process_allocation
from alphamarket.models import (
    Allocation,
    AllocationSnapshot,
    AllocationStatus,
    AssetQuote,
    Fill,
    Instrument,
    MirrorAssetFill,
    MirrorFill,
    MirrorLedgerEntry,
    PaperBookCheckpoint,
    Strategy,
    User,
    VenueAccount,
    utcnow,
)
from alphamarket.money import to_micros
from alphamarket.paper_book import LegSpec, cash_leg
from alphamarket.paper_lifecycle import (
    apply_lifecycle,
    close_now,
    close_stopped,
    is_closing,
    stop_ref,
)
from alphamarket.portfolio import allocation_view, snapshot_allocation, stop_allocation
from alphamarket.tails import stop_by_user
from test_paper_engine import World

M = 1_000_000
D = date(2026, 9, 10)  # a Thursday
BTC_TIERS = [{"lower_bound": "0", "max_leverage": 40}]


def at(day: date, hour: int = 12, minute: int = 0, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=UTC)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


# ---- seeding ---------------------------------------------------------------------------------


def _allocation(db, venue: str, dollars: str, tag: str = "", dd_bps: int = 9000) -> Allocation:
    name = f"{venue}{tag}"
    creator = User(email=f"c-{name}@example.com", password_hash="x", display_name="C")
    db.add(creator)
    db.flush()
    account = VenueAccount(user_id=creator.id, venue=venue, venue_env="prod", key_id=f"k-{name}")
    db.add(account)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=account.id, name=name, slug=name)
    db.add(strategy)
    db.flush()
    follower = User(email=f"f-{name}@example.com", password_hash="x", display_name="F")
    db.add(follower)
    db.flush()
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=to_micros(dollars),
        drawdown_limit_bps=dd_bps,
        high_water_micros=to_micros(dollars),
        started_at=at(D, 0),
    )
    db.add(alloc)
    db.commit()
    return alloc


def _instrument(db, venue, symbol, base, type_=InstrumentType.SPOT, decimals=8, tiers=None):
    inst = Instrument(
        venue=venue,
        symbol=symbol,
        type=type_,
        base_asset=base,
        quote_asset="USD",
        qty_decimals=decimals,
        margin_tiers=tiers,
    )
    db.add(inst)
    db.flush()
    return inst


def _seed_fill(db, alloc, inst, action, qty_atoms, price, notional, fee, when, ref):
    """A paper fill booked as the engine books it (paper._legs' shape): spot
    pays/receives the notional, a perp only pays the fee (no P&L on an open)."""
    sign = 1 if action == "buy" else -1
    db.add(
        MirrorAssetFill(
            allocation_id=alloc.id,
            reason="signal",
            mode="paper",
            instrument_id=inst.id,
            action=action,
            qty_atoms=qty_atoms,
            qty_decimals=inst.qty_decimals,
            price=price,
            notional_micros=notional,
            fee_micros=fee,
            price_basis="ask" if action == "buy" else "bid",
            filled_at=when,
        )
    )
    legs = [
        LegSpec(inst.base_asset, sign * qty_atoms, inst.qty_decimals, LegKind.FILL_BASE, inst.id)
    ]
    if inst.type != InstrumentType.PERP:
        legs.append(cash_leg(-sign * notional, LegKind.FILL_QUOTE))
    legs.append(cash_leg(-fee, LegKind.FEE))
    paper_book.append_group(db, alloc, ref=ref, legs=legs, occurred_at=when)
    db.commit()


def _quote(db, inst, when, bid=None, ask=None, last=None):
    db.add(AssetQuote(instrument_id=inst.id, bid=bid, ask=ask, last=last, as_of=when, source="t"))
    db.commit()


def _stop(db, alloc, when, reason="user"):
    stop_allocation(db, alloc, reason, commit=False)
    alloc.stopped_at = when
    db.commit()


def _cash(db, alloc) -> int:
    return paper_book.load_book(db, alloc).cash_micros


def _legs(db, alloc, ref) -> dict[str, int]:
    rows = db.scalars(
        select(MirrorLedgerEntry).where(
            MirrorLedgerEntry.allocation_id == alloc.id, MirrorLedgerEntry.ref == ref
        )
    ).all()
    return {f"{r.kind}:{r.asset}": r.delta_atoms for r in rows}


def _closes(db, alloc) -> list[MirrorAssetFill]:
    return list(
        db.scalars(
            select(MirrorAssetFill)
            .where(
                MirrorAssetFill.allocation_id == alloc.id, MirrorAssetFill.reason == "stop_close"
            )
            .order_by(MirrorAssetFill.id)
        ).all()
    )


def _n_legs(db, alloc) -> int:
    return db.scalar(
        select(func.count(MirrorLedgerEntry.id)).where(MirrorLedgerEntry.allocation_id == alloc.id)
    )


def _kraken_btc_and_eth(db, tag=""):
    """$2,500 on Kraken (0.80% taker, fees to $0.0001): 0.01 BTC bought at
    60,030 (cost 600.30, fee 4.8024) and 0.5 ETH at 3,001 (cost 1,500.50,
    fee 12.0040). Cash 2,500 - 600.30 - 4.8024 - 1,500.50 - 12.004 = 382.3936."""
    alloc = _allocation(db, "kraken", "2500", tag)
    btc = _instrument(db, "kraken", f"XXBTZUSD{tag}", "BTC")
    eth = _instrument(db, "kraken", f"XETHZUSD{tag}", "ETH")
    _seed_fill(db, alloc, btc, "buy", 1_000_000, "60030", 600_300_000, 4_802_400, at(D), "sig:1")
    _seed_fill(db, alloc, eth, "buy", 50_000_000, "3001", 1_500_500_000, 12_004_000, at(D), "sig:2")
    assert _cash(db, alloc) == to_micros("382.3936")
    return alloc, btc, eth


# ---- spot -----------------------------------------------------------------------------------


def test_spot_closes_at_the_bid_after_fees(db):
    """The engine's own buy (methodology worked example): a $1,000 follower
    buys 0.01 BTC at the ask 60,030, cost 600.30, fee 4.8024, cash 394.8976.
    Stopped, it sells the 0.01 at the bid 60,990.5: proceeds 609.905 (rounded
    down: exact), fee 609.905 x 0.8% = 4.87924 -> 4.8792 (half up to $0.0001).
    Cash 394.8976 + 609.905 - 4.8792 = 999.9234."""
    kraken = World(db, "kraken")
    btc = kraken.instrument("XXBTZUSD", "BTC", min_qty_atoms=10_000, min_notional="0.5")
    alloc = kraken.allocation("1000")
    kraken.signal(btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    process_allocation(db, alloc)
    assert _cash(db, alloc) == to_micros("394.8976")

    now = at(D, 14)
    _stop(db, alloc, now)
    # Fresh at 13:59: the bid 60,990.5. A newer row with only a last price is
    # not a bid (never a last or a mid for a close), and an older, stale row
    # with a better bid is never used.
    _quote(db, btc, at(D, 13, 40), bid="70000", ask="70010")  # 20 min old: stale
    _quote(db, btc, at(D, 13, 59), bid="60990.5", ask="61000")
    _quote(db, btc, at(D, 13, 59, 30), last="65000")
    assert is_closing(db, alloc)

    assert apply_lifecycle(db, alloc, now) == 1
    db.commit()
    ref = stop_ref(btc.id, alloc.id)
    assert ref == f"stop:{btc.id}:{alloc.id}"
    assert _legs(db, alloc, ref) == {
        "fill_base:BTC": -1_000_000,
        "fill_quote:USD": 609_905_000,
        "fee:USD": -4_879_200,
    }
    (fill,) = _closes(db, alloc)
    assert (fill.action, fill.qty_atoms, fill.qty_decimals, fill.price, fill.price_basis) == (
        "sell",
        1_000_000,
        8,
        "60990.5",
        "bid",
    )
    assert fill.signal_id is None and fill.penalty_bps is None and fill.mode == "paper"
    assert fill.notional_micros == 609_905_000 and fill.fee_micros == 4_879_200
    # The very fee a copied sell of the same size pays (one formula).
    profile = get_profile("kraken")
    assert fill.fee_micros == paper.asset_fee_micros(profile, Action.SELL, 1_000_000, 8, "60990.5")
    assert aware(fill.filled_at) == now and aware(fill.quote_at) == at(D, 13, 59)
    assert _cash(db, alloc) == to_micros("999.9234")
    assert paper_book.load_book(db, alloc).holdings == {}
    assert not is_closing(db, alloc)
    # The stop close is booked like a sale: the cost basis is gone with the position.
    assert paper.cost_basis_micros(db, alloc).get("BTC", 0) == 0


def test_an_instrument_without_a_fresh_quote_waits_while_the_others_close(db):
    alloc, btc, eth = _kraken_btc_and_eth(db)
    t1 = at(D, 14)
    _stop(db, alloc, t1)
    _quote(db, btc, at(D, 13, 59), bid="60990.5", ask="61000")
    # ETH: a stale bid (16 min) and a fresh last-only row. Neither prices a close.
    _quote(db, eth, at(D, 13, 44), bid="3100", ask="3101")
    _quote(db, eth, at(D, 13, 59), last="3050")

    # BTC: 0.01 x 60,990.5 = 609.905, fee 4.8792: cash 382.3936 + 609.905 - 4.8792 = 987.4194.
    assert apply_lifecycle(db, alloc, t1) == 1
    db.commit()
    assert _cash(db, alloc) == to_micros("987.4194")
    held = paper_book.load_book(db, alloc).holdings
    assert set(held) == {"ETH"} and held["ETH"].atoms == 50_000_000
    assert is_closing(db, alloc)
    view = allocation_view(db, alloc)
    assert view["closing"] is True and view["status"] == "stopped"

    # Later passes with no fresh ETH bid add nothing.
    assert apply_lifecycle(db, alloc, at(D, 14, 30)) == 0

    # The first fresh ETH bid, 2,999.5: 0.5 x 2,999.5 = 1,499.75, fee 11.998 -> 11.9980.
    # Cash 987.4194 + 1,499.75 - 11.998 = 2,475.1714.
    t2 = at(D, 15)
    _quote(db, eth, at(D, 14, 58), bid="2999.5", ask="3000.5")
    assert apply_lifecycle(db, alloc, t2) == 1
    db.commit()
    assert _legs(db, alloc, stop_ref(eth.id, alloc.id)) == {
        "fill_base:ETH": -50_000_000,
        "fill_quote:USD": 1_499_750_000,
        "fee:USD": -11_998_000,
    }
    assert _cash(db, alloc) == to_micros("2475.1714")
    assert [(f.instrument_id, aware(f.filled_at)) for f in _closes(db, alloc)] == [
        (btc.id, t1),
        (eth.id, t2),
    ]
    assert not is_closing(db, alloc)
    assert allocation_view(db, alloc)["closing"] is False


def test_rerunning_the_close_adds_nothing(db):
    alloc, btc, eth = _kraken_btc_and_eth(db)
    now = at(D, 14)
    # An ACTIVE allocation is never closed, quotes or not.
    _quote(db, btc, at(D, 13, 59), bid="60990.5", ask="61000")
    _quote(db, eth, at(D, 13, 59), bid="2999.5", ask="3000.5")
    assert close_stopped(db, alloc, now) == 0
    _stop(db, alloc, now)
    assert apply_lifecycle(db, alloc, now) == 2
    db.commit()
    n, cash = _n_legs(db, alloc), _cash(db, alloc)
    # 382.3936 + 609.905 - 4.8792 + 1,499.75 - 11.998 = 2,475.1714 (as above).
    assert cash == to_micros("2475.1714")
    # Again, through every entry point: the book is flat and each ref is booked.
    assert apply_lifecycle(db, alloc, now) == 0
    assert apply_lifecycle(db, alloc, at(D, 14, 5)) == 0
    assert close_stopped(db, alloc, at(D, 14, 5)) == 0
    assert close_now(db, alloc) == 0
    assert process_allocation(db, alloc) == (0, 0)
    assert (_n_legs(db, alloc), _cash(db, alloc)) == (n, cash)
    assert len(_closes(db, alloc)) == 2
    assert all(paper_book.has_group(db, alloc, stop_ref(i.id, alloc.id)) for i in (btc, eth))


# ---- stocks ---------------------------------------------------------------------------------


def test_a_stock_after_hours_waits_then_closes_at_the_first_fresh_quote(db, monkeypatch):
    """$10,000 on Alpaca: 20 shares (9 dp) bought at 80, commission-free:
    cash 8,400. Stopped Thursday 20:30 UTC, after the 20:00 close: the last
    quote (19:59:50) is 40 minutes old, so nothing closes overnight. Friday's
    first quote (13:30:02) has the bid 81.25: proceeds 20 x 81.25 = 1,625.00;
    with an SEC rate of $27.80 per million, SEC = 1,625 x 27.80 / 1e6 =
    0.045175 -> 0.05 and FINRA TAF = 20 x 0.000166 = 0.00332 -> 0.01, each up
    to the cent: fee 0.06. Cash 8,400 + 1,625 - 0.06 = 10,024.94."""
    fees = RegulatoryFees(
        sec_fee_per_million="27.80", taf_per_share="0.000166", taf_max="8.30", effective="t"
    )
    profile = replace(get_profile("alpaca"), regulatory_fees=fees)
    monkeypatch.setattr(paper, "_profile_of", lambda db, allocation: profile)
    alloc = _allocation(db, "alpaca", "10000")
    aapl = _instrument(db, "alpaca", "AAPL", "AAPL", InstrumentType.EQUITY, decimals=9)
    _seed_fill(db, alloc, aapl, "buy", 20 * 10**9, "80", 1_600 * M, 0, at(D, 15), "sig:1")
    assert _cash(db, alloc) == 8_400 * M
    _quote(db, aapl, at(D, 19, 59, 50), bid="80.50", ask="80.52", last="80.51")

    stopped_at = at(D, 20, 30)
    _stop(db, alloc, stopped_at)
    assert process_allocation(db, alloc) == (0, 0)  # (the clock is past every quote)
    assert apply_lifecycle(db, alloc, stopped_at) == 0
    assert apply_lifecycle(db, alloc, at(D + timedelta(days=1), 13, 0)) == 0
    db.commit()
    assert _closes(db, alloc) == [] and is_closing(db, alloc)
    assert allocation_view(db, alloc)["closing"] is True
    assert _cash(db, alloc) == 8_400 * M

    friday = D + timedelta(days=1)
    _quote(db, aapl, at(friday, 13, 30, 2), bid="81.25", ask="81.27")
    now = at(friday, 13, 31)
    assert apply_lifecycle(db, alloc, now) == 1
    db.commit()
    assert _legs(db, alloc, stop_ref(aapl.id, alloc.id)) == {
        "fill_base:AAPL": -20 * 10**9,
        "fill_quote:USD": 1_625 * M,
        "fee:USD": -60_000,
    }
    (fill,) = _closes(db, alloc)
    assert (fill.price, fill.price_basis, fill.fee_micros) == ("81.25", "bid", 60_000)
    assert fill.fee_micros == paper.regulatory_fee_micros(fees, Action.SELL, 20 * 10**9, 9, "81.25")
    assert aware(fill.filled_at) == now and aware(fill.quote_at) == at(friday, 13, 30, 2)
    assert _cash(db, alloc) == to_micros("10024.94")
    assert not is_closing(db, alloc)


# ---- perps ----------------------------------------------------------------------------------


def test_a_short_perp_closes_at_the_ask_with_realized_pnl(db):
    """Hyperliquid (0.05% to the micro). $1,000 opened 0.19943 BTC short at
    60,990: notional 12,163.2357, fee 6.08161785 -> 6.081618, cash
    993.918382. Stopped, it buys 0.19943 back at the ask 60,500.5 (the bid
    60,499.5 is the wrong side for a short):
      realized = 0.19943 x (60,500.5 - 60,990) x (-1) = 0.19943 x 489.5 = 97.620985
      fee      = 0.19943 x 60,500.5 = 12,065.614715 x 0.05% = 6.0328073575 -> 6.032807
      cash     = 993.918382 + 97.620985 - 6.032807 = 1,085.50656"""
    alloc = _allocation(db, "hyperliquid", "1000")
    perp = _instrument(db, "hyperliquid", "BTC", "BTC-PERP", InstrumentType.PERP, 5, BTC_TIERS)
    _seed_fill(db, alloc, perp, "sell", 19_943, "60990", 12_163_235_700, 6_081_618, at(D), "sig:1")
    assert _cash(db, alloc) == to_micros("993.918382")

    now = at(D, 14)
    _stop(db, alloc, now)
    _quote(db, perp, at(D, 13, 59), bid="60499.5", ask="60500.5")
    assert apply_lifecycle(db, alloc, now) == 1  # (no liquidation: the short is in profit)
    db.commit()
    assert _legs(db, alloc, stop_ref(perp.id, alloc.id)) == {
        "fill_base:BTC-PERP": 19_943,
        "realized_pnl:USD": 97_620_985,
        "fee:USD": -6_032_807,
    }
    (fill,) = _closes(db, alloc)
    assert (fill.action, fill.qty_atoms, fill.qty_decimals, fill.price, fill.price_basis) == (
        "buy",
        19_943,
        5,
        "60500.5",
        "ask",
    )
    assert fill.notional_micros == 12_065_614_715 and fill.fee_micros == 6_032_807
    book = paper_book.load_book(db, alloc)
    assert book.perps == {} and book.holdings == {}
    assert book.cash_micros == to_micros("1085.50656")
    assert apply_lifecycle(db, alloc, at(D, 14, 5)) == 0


def _hl_long(db, tag):
    """$1,000 on Hyperliquid: 0.09998 BTC-PERP long at 60,010, fee 2.9999,
    cash 997.0001 (tests/test_paper_lifecycle.py's book)."""
    alloc = _allocation(db, "hyperliquid", "1000", tag)
    perp = _instrument(
        db, "hyperliquid", f"BTC{tag}", "BTC-PERP", InstrumentType.PERP, 5, BTC_TIERS
    )
    _seed_fill(db, alloc, perp, "buy", 9_998, "60010", 5_999_799_800, 2_999_900, at(D), "sig:1")
    assert _cash(db, alloc) == to_micros("997.0001")
    return alloc, perp


def test_liquidation_then_a_stop_does_not_close_twice(db):
    alloc, perp = _hl_long(db, "-liq")
    # Mark 50,500 (mid of 50,499 / 50,501): AV = 997.0001 + 0.09998 x (50,500 - 60,010)
    # = 46.1903 < MM = 5,048.99 / 80 = 63.112375: liquidated at the mark, -950.8098.
    _quote(db, perp, at(D, 15, 10), bid="50499", ask="50501")
    now = at(D, 15, 12)
    assert apply_lifecycle(db, alloc, now) == 1  # the liquidation; the stop close finds it flat
    db.commit()
    assert alloc.status == AllocationStatus.STOPPED and alloc.stop_reason == "liquidated"
    assert _cash(db, alloc) == to_micros("46.1903")
    assert _closes(db, alloc) == []
    assert not is_closing(db, alloc)
    # Every later stop path adds nothing.
    n = _n_legs(db, alloc)
    stop_by_user(db, alloc)  # already stopped: stays as it is
    assert close_now(db, alloc) == 0
    assert apply_lifecycle(db, alloc, at(D, 15, 20)) == 0
    assert _n_legs(db, alloc) == n and _closes(db, alloc) == []
    assert alloc.stop_reason == "liquidated"


def test_a_stop_close_then_a_crash_does_not_liquidate(db):
    alloc, perp = _hl_long(db, "-stop")
    now = at(D, 15, 12)
    _stop(db, alloc, now)
    # Healthy at 59,995 (AV 995.5004 >> MM), so no liquidation; the stop sells at the bid:
    #   realized = 0.09998 x (59,990 - 60,010) = -1.9996
    #   fee      = 0.09998 x 59,990 = 5,997.8002 x 0.05% = 2.9989001 -> 2.998900
    #   cash     = 997.0001 - 1.9996 - 2.9989 = 992.0016
    _quote(db, perp, at(D, 15, 10), bid="59990", ask="60000")
    assert apply_lifecycle(db, alloc, now) == 1
    db.commit()
    assert _legs(db, alloc, stop_ref(perp.id, alloc.id)) == {
        "fill_base:BTC-PERP": -9_998,
        "realized_pnl:USD": -1_999_600,
        "fee:USD": -2_998_900,
    }
    assert _cash(db, alloc) == to_micros("992.0016")
    # A crash after the close finds nothing to liquidate.
    _quote(db, perp, at(D, 15, 20), bid="40000", ask="40001")
    assert apply_lifecycle(db, alloc, at(D, 15, 21)) == 0
    assert alloc.stop_reason == "user"
    assert db.scalar(select(func.count()).where(MirrorLedgerEntry.ref.like("liq:%"))) == 0


# ---- prediction markets ---------------------------------------------------------------------


def test_a_binary_tail_is_untouched_by_a_stop(db):
    """A Kalshi tail keeps its contracts after Stop: they run to settlement."""
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    creator = User(email="c-kalshi@example.com", password_hash="x", display_name="C")
    db.add(creator)
    db.flush()
    account = VenueAccount(
        user_id=creator.id,
        venue="kalshi",
        venue_env="prod",
        key_id="k",
        anchor_balance_micros=to_micros("1000.00"),
        anchor_at=t0,
        claimed_at=t0,
    )
    db.add(account)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=account.id, name="K", slug="k")
    db.add(strategy)
    db.flush()
    db.add(
        Fill(
            strategy_id=strategy.id,
            venue_fill_id="a",
            ticker="KXFED-26SEP-C",
            side="yes",
            action="buy",
            count_micros=to_micros("10"),
            price_micros=to_micros("0.40"),
            fee_micros=to_micros("0.168"),
            filled_at=t0 + timedelta(hours=1),
        )
    )
    follower = User(email="f-kalshi@example.com", password_hash="x", display_name="F")
    db.add(follower)
    db.flush()
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=to_micros("100.00"),
        drawdown_limit_bps=1500,
        high_water_micros=to_micros("100.00"),
        started_at=t0,
    )
    db.add(alloc)
    db.commit()
    signals.emit_signals(db, strategy)
    process_allocation(db, alloc)
    fills_before = db.scalars(select(MirrorFill).where(MirrorFill.allocation_id == alloc.id)).all()
    assert len(fills_before) == 1
    positions = allocation_view(db, alloc)["positions"]
    assert positions

    stop_by_user(db, alloc)
    assert alloc.status == AllocationStatus.STOPPED
    assert close_now(db, alloc) == 0 and not is_closing(db, alloc)
    process_allocation(db, alloc)
    view = allocation_view(db, alloc)
    assert view["closing"] is False and view["positions"] == positions
    fills_after = db.scalars(select(MirrorFill).where(MirrorFill.allocation_id == alloc.id)).all()
    assert [f.id for f in fills_after] == [f.id for f in fills_before]
    assert db.scalar(select(func.count(MirrorAssetFill.id))) == 0
    assert db.scalar(select(func.count(MirrorLedgerEntry.id))) == 0


# ---- the stop paths ---------------------------------------------------------------------------


def _kraken_follower_with_btc(db, dd_bps=1500):
    """The engine's buy: $1,000, 0.01 BTC at the ask 60,030, cash 394.8976."""
    kraken = World(db, "kraken")
    btc = kraken.instrument("XXBTZUSD", "BTC", min_qty_atoms=10_000, min_notional="0.5")
    alloc = kraken.allocation("1000", drawdown_limit_bps=dd_bps)
    kraken.signal(btc, "buy", "0.1", "60000", before="0", after="0.1", ask="60030")
    process_allocation(db, alloc)
    assert _cash(db, alloc) == to_micros("394.8976")
    assert db.get(PaperBookCheckpoint, alloc.id) is not None  # written by that pass
    return alloc, btc


def test_a_manual_stop_closes_inline_and_keeps_the_checkpoint_true(db):
    alloc, btc = _kraken_follower_with_btc(db)
    _quote(db, btc, utcnow() - timedelta(seconds=60), bid="60990.5", ask="61000")
    stop_by_user(db, alloc)  # database only: no venue is called
    assert alloc.status == AllocationStatus.STOPPED and alloc.stop_reason == "user"
    # Closed before the snapshot: 394.8976 + 609.905 - 4.8792 = 999.9234 (as above).
    assert _cash(db, alloc) == to_micros("999.9234")
    (fill,) = _closes(db, alloc)
    assert (fill.price, fill.price_basis, fill.fee_micros) == ("60990.5", "bid", 4_879_200)
    snap = db.scalars(
        select(AllocationSnapshot)
        .where(AllocationSnapshot.allocation_id == alloc.id)
        .order_by(AllocationSnapshot.id.desc())
    ).first()
    assert snap.equity_micros == to_micros("999.9234") and snap.open_value_micros == 0
    # The checkpoint was brought up to the close under the row lock, and is right.
    cp = db.get(PaperBookCheckpoint, alloc.id)
    db.refresh(cp)
    top = db.scalar(
        select(func.max(MirrorLedgerEntry.id)).where(MirrorLedgerEntry.allocation_id == alloc.id)
    )
    assert cp.through_entry_id == top
    assert paper_book.verify_checkpoint(db, alloc) is True
    full = paper_book.load_book(db, alloc)
    state = paper_book.book_state(db, alloc)
    assert (state.cash_micros, state.holdings, state.perps) == (
        full.cash_micros,
        full.holdings,
        full.perps,
    )
    assert allocation_view(db, alloc)["closing"] is False
    assert process_allocation(db, alloc) == (0, 0)


def test_a_manual_stop_with_no_fresh_quote_closes_on_a_later_pass(db):
    alloc, btc = _kraken_follower_with_btc(db)
    _quote(db, btc, utcnow() - timedelta(minutes=20), bid="60990.5", ask="61000")  # stale
    stop_by_user(db, alloc)
    assert alloc.status == AllocationStatus.STOPPED
    assert _closes(db, alloc) == [] and is_closing(db, alloc)
    assert paper_book.verify_checkpoint(db, alloc) is True
    _quote(db, btc, utcnow() - timedelta(seconds=5), bid="60990.5", ask="61000")
    assert process_allocation(db, alloc) == (0, 1)  # the mirror pass closes it
    assert _cash(db, alloc) == to_micros("999.9234")
    assert not is_closing(db, alloc)
    assert paper_book.verify_checkpoint(db, alloc) is True


def test_the_drawdown_stop_records_the_pre_close_equity_then_closes(db):
    """0.01 BTC and cash 394.8976. Quote 40,000 / 40,010 (mid 40,005): equity
    394.8976 + 400.05 = 794.9476, a 20.5% drawdown past the 15% limit. The
    snapshot keeps 794.9476; the close sells at the bid 40,000: proceeds 400,
    fee 3.2000, cash 394.8976 + 400 - 3.2 = 791.6976, the next snapshot."""
    alloc, btc = _kraken_follower_with_btc(db, dd_bps=1500)
    _quote(db, btc, utcnow() - timedelta(seconds=30), bid="40000", ask="40010")
    snap = snapshot_allocation(db, alloc)
    assert snap.equity_micros == to_micros("794.9476")
    assert alloc.status == AllocationStatus.STOPPED and alloc.stop_reason == "drawdown_limit"
    assert _cash(db, alloc) == to_micros("791.6976")
    (fill,) = _closes(db, alloc)
    assert (fill.price, fill.fee_micros) == ("40000", 3_200_000)
    assert snapshot_allocation(db, alloc).equity_micros == to_micros("791.6976")
    assert paper_book.verify_checkpoint(db, alloc) is True


# ---- quoting a closing tail -----------------------------------------------------------------


def _held(db, alloc, inst, atoms, when, ref):
    paper_book.append_group(
        db,
        alloc,
        ref=ref,
        legs=[LegSpec(inst.base_asset, atoms, inst.qty_decimals, LegKind.FILL_BASE, inst.id)],
        occurred_at=when,
    )
    db.add(
        MirrorAssetFill(
            allocation_id=alloc.id,
            reason="signal",
            mode="paper",
            instrument_id=inst.id,
            action="buy",
            qty_atoms=abs(atoms),
            qty_decimals=inst.qty_decimals,
            price="1",
            notional_micros=1,
            price_basis="ask",
            filled_at=when,
        )
    )
    db.flush()


def test_open_instruments_include_a_stopped_tail_until_it_is_flat(db):
    now = at(D, 14)
    btc = _instrument(db, "kraken", "XXBTZUSD", "BTC")
    eth = _instrument(db, "kraken", "XETHZUSD", "ETH")
    sol = _instrument(db, "kraken", "SOLUSD", "SOL")
    closing = _allocation(db, "kraken", "1000", "-closing")
    _held(db, closing, btc, 1_000_000, at(D), "sig:1")
    flat = _allocation(db, "kraken", "1000", "-flat")
    _held(db, flat, eth, 5_000_000, at(D), "sig:1")
    _held(db, flat, eth, -5_000_000, at(D, 13), "stop:x")
    old = _allocation(db, "kraken", "1000", "-old")
    _held(db, old, sol, 7, at(D) - timedelta(days=40), "sig:1")
    _stop(db, closing, now - timedelta(days=1))
    _stop(db, flat, at(D, 13))
    _stop(db, old, now - prices.STOPPED_QUOTE_WINDOW - timedelta(seconds=1))

    # The closing tail's BTC is quoted; the flat one drops out; one stopped
    # longer than the window is not scanned at all.
    assert [i.id for i in prices._open_instruments(db, now)] == [btc.id]
    # Once its close is booked, the stopped tail is flat and drops out too.
    _held(db, closing, btc, -1_000_000, at(D, 13, 30), "stop:y")
    db.commit()
    assert prices._open_instruments(db, now) == []


def test_fresh_side_is_the_freshest_row_with_that_side_never_stale(db):
    btc = _instrument(db, "kraken", "XXBTZUSD", "BTC")
    now = at(D, 14)
    assert prices.fresh_side(db, btc.id, now, "bid") is None
    _quote(db, btc, now - timedelta(seconds=901), bid="1", ask="2")  # 15 min 1 s: stale
    assert prices.fresh_side(db, btc.id, now, "bid") is None
    _quote(db, btc, now - timedelta(seconds=900), bid="10", ask="11")  # exactly 15 min: fresh
    _quote(db, btc, now - timedelta(seconds=30), ask="12")  # ask only
    _quote(db, btc, now - timedelta(seconds=10), last="13")  # last only
    bid = prices.fresh_side(db, btc.id, now, "bid")
    ask = prices.fresh_side(db, btc.id, now, "ask")
    assert (bid.price, bid.side, bid.as_of) == ("10", "bid", now - timedelta(seconds=900))
    assert (ask.price, ask.as_of) == ("12", now - timedelta(seconds=30))
    with pytest.raises(ValueError):
        prices.fresh_side(db, btc.id, now, "last")


# ---- read side --------------------------------------------------------------------------------


def test_the_closing_label_shows_until_the_book_is_flat(client, session_factory):
    from test_pages_v2 import _stats, _strategy, v2_extras
    from test_tail_ui import _allocations, _signup, _start

    with session_factory() as db:
        s = _strategy(db, "kraken", "eth-swing", "ETH Swing")
        _stats(db, s, v2_extras())
        eth = _instrument(db, "kraken", "XETHZUSD", "ETH")
        db.commit()
        eth_id = eth.id
    _signup(client)
    assert _start(client).status_code == 303
    (row,) = _allocations(session_factory)
    with session_factory() as db:
        alloc = db.get(Allocation, row.id)
        eth = db.get(Instrument, eth_id)
        _seed_fill(
            db, alloc, eth, "buy", 50_000_000, "3001", 1_500_500_000, 12_004_000, at(D), "s:1"
        )
        # Only a stale quote: the stop cannot close ETH yet.
        _quote(db, eth, utcnow() - timedelta(minutes=30), bid="3000", ask="3001")

    resp = client.post(f"/portfolio/allocations/{row.id}/stop", follow_redirects=False)
    assert resp.status_code == 303
    html = client.get("/portfolio?notice=stopped").text
    assert "stopped (user) · closing" in html
    assert "Closing: this stopped tail sells its remaining positions" in html
    assert "Crypto and stock positions are sold at the current quote" in html  # the notice
    tail = client.get("/s/eth-swing/tail").text
    assert "Closing: your stopped paper tail of this strategy" in tail
    api = client.get(f"/allocations/{row.id}").json()
    assert api["status"] == "stopped" and api["closing"] is True

    with session_factory() as db:
        _quote(db, db.get(Instrument, eth_id), utcnow() - timedelta(seconds=5), bid="3000")
        assert process_allocation(db, db.get(Allocation, row.id))[1] == 1
    html = client.get("/portfolio").text
    assert "· closing" not in html and "Closing:" not in html
    assert "Closing:" not in client.get("/s/eth-swing/tail").text
    assert client.get(f"/allocations/{row.id}").json()["closing"] is False


# ---- Postgres ---------------------------------------------------------------------------------


@pytest.mark.pg
def test_the_stop_close_on_postgres_is_append_only(pg_session_factory):
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    with pg_session_factory() as db:
        alloc, btc, eth = _kraken_btc_and_eth(db, "-stop-pg")
        _quote(db, btc, utcnow() - timedelta(seconds=60), bid="60990.5", ask="61000")
        _quote(db, eth, utcnow() - timedelta(seconds=60), bid="2999.5", ask="3000.5")
        _stop(db, alloc, utcnow())
        # The inline close takes the row lock (FOR UPDATE) and commits.
        assert close_now(db, alloc) == 2
        assert close_now(db, alloc) == 0
        assert _cash(db, alloc) == to_micros("2475.1714")
        assert paper_book.verify_checkpoint(db, alloc) is True
        ref = stop_ref(btc.id, alloc.id)
        for statement in (
            "UPDATE mirror_ledger_entries SET delta_atoms = 0 WHERE ref = :r",
            "DELETE FROM mirror_ledger_entries WHERE ref = :r",
        ):
            with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
                db.execute(text(statement), {"r": ref})
        with pytest.raises(DBAPIError, match="append-only"), db.begin_nested():
            db.execute(
                text("UPDATE mirror_asset_fills SET fee_micros = 0 WHERE reason = 'stop_close'")
            )
        db.rollback()
        assert apply_lifecycle(db, alloc, utcnow()) == 0
