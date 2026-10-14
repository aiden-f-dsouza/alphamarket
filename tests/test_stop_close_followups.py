"""Round-2 follow-ups of the stop close (docs/copy_strategies_implementation.md;
methodology "When a tail stops").

1. A tail still closing names the positions that wait for a fresh quote
   (paper_lifecycle.closing_waits), and the nightly housekeeping logs one
   that has waited longer than stop_close_alert_days. Quoting it never
   stops (tests/test_stop_close.py pins prices._open_instruments).
2. A split that arrives after the stop close but is dated before it is
   closed at the stop close's own price, so the book ends exactly where
   "split, then close" would have left it (paper_lifecycle.correct_late_splits).

Expected values are worked by hand in the docstrings (USD; M = 1,000,000
micros). Stock fees: SEC $27.80 per million of a sell's notional and FINRA
TAF $0.000166 per share sold (max $8.30), each rounded UP to the cent per
fill; buys pay nothing.
"""

import logging
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import select

from alphamarket import housekeeping, paper, paper_book
from alphamarket.connectors.base import CorporateActionKind, InstrumentType, RegulatoryFees
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import aware
from alphamarket.models import Allocation, CorporateActionRow, MirrorLedgerEntry, utcnow
from alphamarket.money import to_micros
from alphamarket.paper_book import LegSpec
from alphamarket.paper_lifecycle import (
    CloseWait,
    apply_lifecycle,
    closing_waits,
    correct_late_splits,
    is_closing,
    stop_ref,
)
from test_stop_close import (
    BTC_TIERS,
    D,
    M,
    _allocation,
    _cash,
    _closes,
    _instrument,
    _kraken_btc_and_eth,
    _legs,
    _quote,
    _seed_fill,
    _stop,
    at,
)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


SHARE = 10**9  # AAPL at 9 decimals
FEES = RegulatoryFees(
    sec_fee_per_million="27.80", taf_per_share="0.000166", taf_max="8.30", effective="t"
)
FRI = D + timedelta(days=1)  # D is a Thursday
MON = D + timedelta(days=4)
TUE = D + timedelta(days=5)


# ---- 1. what a closing tail waits for ------------------------------------------------------


def test_closing_waits_names_the_position_without_a_fresh_quote(db):
    alloc, btc, eth = _kraken_btc_and_eth(db)
    now = at(D, 14)
    _stop(db, alloc, now)
    _quote(db, btc, at(D, 13, 59), bid="60990.5", ask="61000")
    _quote(db, eth, at(D, 13, 44), bid="3100", ask="3101")  # 16 min: stale
    _quote(db, eth, at(D, 13, 59), last="3050")  # fresh, but no bid
    # Before the pass: BTC has a fresh bid (it closes next pass), ETH waits.
    # The last bid ETH had is the stale 13:44 row (a last price is not a bid).
    assert closing_waits(db, alloc, now) == [CloseWait("ETH", "ETH/USD", "bid", at(D, 13, 44))]
    assert apply_lifecycle(db, alloc, now) == 1
    db.commit()
    assert closing_waits(db, alloc, now) == [CloseWait("ETH", "ETH/USD", "bid", at(D, 13, 44))]
    _quote(db, eth, at(D, 14, 58), bid="2999.5", ask="3000.5")
    assert closing_waits(db, alloc, at(D, 15)) == []  # closes on the next pass
    assert apply_lifecycle(db, alloc, at(D, 15)) == 1
    db.commit()
    assert closing_waits(db, alloc, at(D, 15)) == []  # flat


def test_a_short_perp_waits_on_the_ask_and_an_active_tail_waits_on_nothing(db):
    alloc = _allocation(db, "hyperliquid", "1000")
    perp = _instrument(db, "hyperliquid", "BTC", "BTC-PERP", InstrumentType.PERP, 5, BTC_TIERS)
    _seed_fill(db, alloc, perp, "sell", 19_943, "60990", 12_163_235_700, 6_081_618, at(D), "sig:1")
    now = at(D, 14)
    _quote(db, perp, at(D, 13, 59), bid="60499.5")  # a fresh bid is the wrong side
    assert closing_waits(db, alloc, now) == []  # ACTIVE: not closing
    _stop(db, alloc, now)
    assert closing_waits(db, alloc, now) == [CloseWait("BTC-PERP", "BTC-PERP", "ask", None)]


def test_housekeeping_logs_a_tail_closing_for_longer_than_the_alert_age(db, caplog):
    """Settings default: 7 days. One tail stopped 8 days ago still holds ETH
    with no fresh bid since then: logged. One stopped 2 days ago: not yet."""
    now = at(D + timedelta(days=8), 3)
    old, btc, eth = _kraken_btc_and_eth(db, "-hk-old")
    _quote(db, eth, at(D, 13, 44), bid="3100", ask="3101")
    _stop(db, old, at(D, 14))
    young, _, _ = _kraken_btc_and_eth(db, "-hk-young")
    _stop(db, young, now - timedelta(days=2))
    # The old tail's BTC closed at the stop; only its ETH is left.
    _quote(db, btc, at(D, 13, 59), bid="60990.5")
    assert apply_lifecycle(db, old, at(D, 14)) == 1
    db.commit()

    caplog.set_level(logging.WARNING, logger="alphamarket.housekeeping")
    assert housekeeping.report_stuck_closes(db, now, timedelta(days=7)) == 1
    (record,) = (r for r in caplog.records if "still closing" in r.getMessage())
    message = record.getMessage()
    assert f"allocation {old.id} stopped 2026-09-10T14:00:00+00:00 still closing" in message
    assert "ETH/USD has no fresh bid (last 2026-09-10T13:44:00+00:00)" in message
    # Nothing is written.
    assert is_closing(db, old) and is_closing(db, young)


# ---- 2. a split after the stop close ---------------------------------------------------------


@pytest.fixture()
def alpaca(monkeypatch):
    profile = replace(get_profile("alpaca"), regulatory_fees=FEES)
    monkeypatch.setattr(paper, "_profile_of", lambda db, allocation: profile)
    return profile


def _aapl_tail(db, tag=""):
    """$10,000 on Alpaca: 20 AAPL bought at 80 on Thursday, commission-free:
    cash 10,000 - 1,600 = 8,400."""
    alloc = _allocation(db, "alpaca", "10000", tag)
    aapl = _instrument(db, "alpaca", f"AAPL{tag}", "AAPL", InstrumentType.EQUITY, decimals=9)
    _seed_fill(db, alloc, aapl, "buy", 20 * SHARE, "80", 1_600 * M, 0, at(D, 15), "sig:1")
    assert _cash(db, alloc) == 8_400 * M
    return alloc, aapl


def _split(db, ex, ratio_new, ratio_old, symbol="AAPL"):
    row = CorporateActionRow(
        source="alpaca",
        symbol=symbol,
        kind=CorporateActionKind.SPLIT,
        ex_date=ex,
        ratio_new=ratio_new,
        ratio_old=ratio_old,
    )
    db.add(row)
    db.commit()
    return row


def _stop_monday(db, alloc, aapl, bid, ask):
    stopped = at(MON, 14)
    _stop(db, alloc, stopped)
    _quote(db, aapl, at(MON, 13, 59), bid=bid, ask=ask)
    assert apply_lifecycle(db, alloc, stopped) == 1
    db.commit()
    return stopped


def test_a_late_split_closes_at_the_stop_price_as_if_split_then_closed(db, alpaca):
    """A 2:1 split, ex-date Friday, that Koine hears of only on Tuesday.

    Monday 14:00 the tail stops; the bid is 40.60 (the market is post-split)
    and the book still holds the pre-split 20 shares. The close sells 20 at
    40.60: proceeds 812.00; SEC 812 x 27.80 / 1e6 = 0.0225736 -> 0.03, TAF
    20 x 0.000166 = 0.00332 -> 0.01: fee 0.04. Cash 8,400 + 812 - 0.04 =
    9,211.96, and the book is flat.

    Tuesday the split arrives: +20 shares at Friday 00:00 (the 20 held then).
    Split, then close, would have sold 40 at 40.60: proceeds 1,624.00; SEC
    1,624 x 27.80 / 1e6 = 0.0451472 -> 0.05, TAF 40 x 0.000166 = 0.00664 ->
    0.01: fee 0.06; cash 8,400 + 1,624 - 0.06 = 10,023.94.

    The correction sells the 20 new shares at the same 40.60, dated Monday
    14:00: cash +1,624.00 - 812.00 = +812.00, fee 0.06 - 0.04 = 0.02 (not
    the 0.04 a separate 20-share sale would pay). Cash 9,211.96 + 812 - 0.02
    = 10,023.94."""
    alloc, aapl = _aapl_tail(db)
    stopped = _stop_monday(db, alloc, aapl, "40.60", "40.62")
    close_ref = stop_ref(aapl.id, alloc.id)
    assert _legs(db, alloc, close_ref) == {
        "fill_base:AAPL": -20 * SHARE,
        "fill_quote:USD": 812 * M,
        "fee:USD": -40_000,
    }
    assert _cash(db, alloc) == to_micros("9211.96") and not is_closing(db, alloc)

    split = _split(db, FRI, 2, 1)
    # One pass: the split (1) and its correction (1).
    assert apply_lifecycle(db, alloc, at(TUE, 9)) == 2
    db.commit()
    assert _legs(db, alloc, f"ca:{split.id}") == {"split:AAPL": 20 * SHARE}
    fix_ref = f"{close_ref}:ca:{split.id}"
    assert _legs(db, alloc, fix_ref) == {
        "fill_base:AAPL": -20 * SHARE,
        "fill_quote:USD": 812 * M,
        "fee:USD": -20_000,
    }
    first, fix = _closes(db, alloc)
    assert (fix.action, fix.qty_atoms, fix.qty_decimals, fix.price, fix.price_basis) == (
        "sell",
        20 * SHARE,
        9,
        "40.60",
        "bid",
    )
    assert (fix.notional_micros, fix.fee_micros, fix.signal_id) == (812 * M, 20_000, None)
    assert aware(fix.filled_at) == stopped and aware(fix.quote_at) == at(MON, 13, 59)
    assert aware(first.filled_at) == stopped
    rows = db.scalars(
        select(MirrorLedgerEntry).where(
            MirrorLedgerEntry.allocation_id == alloc.id, MirrorLedgerEntry.ref == fix_ref
        )
    ).all()
    assert {aware(r.occurred_at) for r in rows} == {stopped}
    assert _cash(db, alloc) == to_micros("10023.94")
    assert paper_book.load_book(db, alloc).holdings == {}
    assert not is_closing(db, alloc)
    assert paper.cost_basis_micros(db, alloc).get("AAPL", 0) == 0
    # Idempotent: the split and its correction are booked once.
    assert apply_lifecycle(db, alloc, at(TUE, 10)) == 0
    assert correct_late_splits(db, alloc, aapl) == 0
    assert len(_closes(db, alloc)) == 2


def test_the_correction_equals_a_split_known_before_the_close(db, alpaca):
    """The same tail with the split on file before the stop: the close sells
    40 at 40.60 in one fill (proceeds 1,624.00, fee 0.06), cash 10,023.94,
    the very cash the late split's correction ends on, and nothing to
    correct."""
    alloc, aapl = _aapl_tail(db, "-known")
    split = _split(db, FRI, 2, 1, symbol="AAPL")
    assert apply_lifecycle(db, alloc, at(FRI, 9)) == 1  # the split, while ACTIVE
    db.commit()
    _stop_monday(db, alloc, aapl, "40.60", "40.62")
    assert _legs(db, alloc, stop_ref(aapl.id, alloc.id)) == {
        "fill_base:AAPL": -40 * SHARE,
        "fill_quote:USD": 1_624 * M,
        "fee:USD": -60_000,
    }
    assert _cash(db, alloc) == to_micros("10023.94")
    assert correct_late_splits(db, alloc, aapl) == 0
    assert _legs(db, alloc, f"{stop_ref(aapl.id, alloc.id)}:ca:{split.id}") == {}


def test_a_late_reverse_split_buys_back_at_the_stop_price_and_refunds_fee(db, alpaca):
    """A 1:2 reverse split, ex Friday, heard of Tuesday. Monday's close sold
    the pre-split 20 at the post-split bid 162.40: proceeds 3,248.00; SEC
    3,248 x 27.80 / 1e6 = 0.0902944 -> 0.10, TAF 0.00332 -> 0.01: fee 0.11.
    Cash 8,400 + 3,248 - 0.11 = 11,647.89.

    The split takes 10 shares (20 x 1 / 2 - 20 = -10). Split, then close:
    sell 10 at 162.40, proceeds 1,624.00; SEC 1,624 x 27.80 / 1e6 =
    0.0451472 -> 0.05, TAF 10 x 0.000166 = 0.00166 -> 0.01: fee 0.06; cash
    8,400 + 1,624 - 0.06 = 10,023.94. The correction buys 10 back at 162.40:
    cash 1,624.00 - 3,248.00 = -1,624.00, fee 0.06 - 0.11 = -0.05 (a
    refund). Cash 11,647.89 - 1,624 + 0.05 = 10,023.94."""
    alloc, aapl = _aapl_tail(db, "-rev")
    _stop_monday(db, alloc, aapl, "162.40", "162.45")
    assert _cash(db, alloc) == to_micros("11647.89")
    split = _split(db, FRI, 1, 2, symbol="AAPL")
    assert apply_lifecycle(db, alloc, at(TUE, 9)) == 2
    db.commit()
    assert _legs(db, alloc, f"ca:{split.id}") == {"split:AAPL": -10 * SHARE}
    assert _legs(db, alloc, f"{stop_ref(aapl.id, alloc.id)}:ca:{split.id}") == {
        "fill_base:AAPL": 10 * SHARE,
        "fill_quote:USD": -1_624 * M,
        "fee:USD": 50_000,
    }
    fix = _closes(db, alloc)[-1]
    assert (fix.action, fix.price, fix.price_basis, fix.notional_micros, fix.fee_micros) == (
        "buy",
        "162.40",
        "bid",
        1_624 * M,
        -50_000,
    )
    assert _cash(db, alloc) == to_micros("10023.94")
    assert paper_book.load_book(db, alloc).holdings == {}


def test_a_split_dated_after_the_close_changes_nothing(db, alpaca):
    """Ex-date Wednesday, after Monday's close: the book held nothing then,
    so the split adds nothing and nothing is corrected (cash 9,211.96)."""
    alloc, aapl = _aapl_tail(db, "-after")
    _stop_monday(db, alloc, aapl, "40.60", "40.62")
    _split(db, MON + timedelta(days=2), 2, 1)
    assert apply_lifecycle(db, alloc, at(MON + timedelta(days=3), 9)) == 0
    assert _cash(db, alloc) == to_micros("9211.96")
    assert len(_closes(db, alloc)) == 1


def test_a_dividend_after_the_close_is_not_paid_on_the_late_split_shares(db, alpaca):
    """The late split and a $0.50 dividend (ex and paid Wednesday, after the
    close) arrive in the same pass. The split's shares are closed first, so
    the holding at Wednesday's record instant is 0: no dividend (paid on the
    20 phantom shares it would have been 20 x 0.50 = 10.00). Cash 10,023.94."""
    alloc, aapl = _aapl_tail(db, "-div")
    _stop_monday(db, alloc, aapl, "40.60", "40.62")
    _split(db, FRI, 2, 1)
    wed = MON + timedelta(days=2)
    db.add(
        CorporateActionRow(
            source="alpaca",
            symbol="AAPL",
            kind=CorporateActionKind.CASH_DIVIDEND,
            ex_date=wed,
            pay_date=wed,
            cash_per_share="0.50",
        )
    )
    db.commit()
    assert apply_lifecycle(db, alloc, at(wed + timedelta(days=1), 9)) == 2
    db.commit()
    kinds = db.scalars(
        select(MirrorLedgerEntry.kind).where(MirrorLedgerEntry.allocation_id == alloc.id)
    ).all()
    assert "dividend" not in kinds
    assert _cash(db, alloc) == to_micros("10023.94")


def test_a_split_booked_before_this_fix_is_corrected_by_the_close_stage(db, alpaca):
    """A tail left "Closing" by a late split booked earlier (the split's ref is
    applied, so the corporate-action stage skips it): the stop-close stage
    finds the stop booked and the shares held, and corrects it the same way
    (cash 10,023.94 as above)."""
    alloc, aapl = _aapl_tail(db, "-legacy")
    _stop_monday(db, alloc, aapl, "40.60", "40.62")
    paper_book.append_group(
        db,
        alloc,
        ref="ca:77",
        legs=[LegSpec("AAPL", 20 * SHARE, 9, "split", aapl.id)],
        occurred_at=at(FRI, 0),
    )
    db.commit()
    assert is_closing(db, alloc)
    assert apply_lifecycle(db, alloc, at(TUE, 9)) == 1
    db.commit()
    assert _legs(db, alloc, f"{stop_ref(aapl.id, alloc.id)}:ca:77") == {
        "fill_base:AAPL": -20 * SHARE,
        "fill_quote:USD": 812 * M,
        "fee:USD": -20_000,
    }
    assert _cash(db, alloc) == to_micros("10023.94")
    assert not is_closing(db, alloc)


def test_a_late_split_on_a_tail_that_is_not_stopped_is_left_alone(db, alpaca):
    alloc, aapl = _aapl_tail(db, "-active")
    assert correct_late_splits(db, alloc, aapl) == 0
    assert db.get(Allocation, alloc.id).status == "active"


def test_the_tail_page_names_the_instrument_a_closing_tail_waits_for(client, session_factory):
    from alphamarket.models import Instrument
    from test_pages_v2 import _stats, _strategy, v2_extras
    from test_tail_ui import _allocations, _signup, _start

    with session_factory() as db:
        s = _strategy(db, "kraken", "eth-wait", "ETH Wait")
        _stats(db, s, v2_extras())
        eth = _instrument(db, "kraken", "XETHZUSD", "ETH")
        db.commit()
        eth_id = eth.id
    _signup(client)
    assert _start(client, "eth-wait").status_code == 303
    (row,) = _allocations(session_factory)
    stale = (utcnow() - timedelta(minutes=30)).replace(second=0, microsecond=0)
    with session_factory() as db:
        alloc = db.get(Allocation, row.id)
        eth = db.get(Instrument, eth_id)
        _seed_fill(
            db, alloc, eth, "buy", 50_000_000, "3001", 1_500_500_000, 12_004_000, at(D), "s:1"
        )
        _quote(db, eth, stale, bid="3000", ask="3001")

    assert client.post(f"/portfolio/allocations/{row.id}/stop").status_code in (200, 303)
    tail = client.get("/s/eth-wait/tail").text
    assert "Closing: your stopped paper tail" in tail
    when = stale.strftime("%Y-%m-%d %H:%M UTC")
    assert f"Waiting for a fresh quote: ETH/USD (the bid; last quoted {when})" in tail
