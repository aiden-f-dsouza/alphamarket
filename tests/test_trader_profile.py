"""trader_profile.py: every metric against hand-computed values (the pure
module behind "How this trader trades"). 2026-09-07 is a Monday (UTC)."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from alphamarket import trader_profile as tp
from alphamarket.connectors.base import InstrumentType as T
from alphamarket.trader_profile import EquityIn, FillIn, FundingIn, SettlementIn, build_profile

DELAY = 300  # a follower polls every five minutes


def at(day: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    """September 2026, UTC."""
    return datetime(2026, 9, day, hh, mm, ss, tzinfo=UTC)


def fill(when, inst, kind, action, qty, price, *, decimals=6, order=None, outcome=None):
    q, p = Decimal(qty), Decimal(price)
    return FillIn(
        at=when,
        instrument=inst,
        kind=kind,
        action=action,
        qty_atoms=int(q.scaleb(decimals)),
        qty_decimals=decimals,
        price=price,
        notional_micros=int(q * p * 1_000_000),
        outcome=outcome,
        order_id=order,
    )


def perp(when, action, qty, price, inst="BTC-PERP", **kw):
    return fill(when, inst, T.PERP, action, qty, price, decimals=5, **kw)


def profile(fills, **kw):
    kw.setdefault("copy_delay_seconds", DELAY)
    return build_profile(fills, **kw)


# Five BTC-PERP fills: open 2, add 1, sell 2.5 (partial close), sell 2 on Tuesday
# (closes the last 0.5, flips short 1.5), buy 1.5 (closes the short).
PERP_FILLS = [
    perp(at(7, 10), "buy", "2", "100"),
    perp(at(7, 10, 30), "buy", "1", "110"),
    perp(at(7, 12), "sell", "2.5", "120"),
    perp(at(8, 10), "sell", "2", "130"),
    perp(at(8, 10, 30), "buy", "1.5", "125"),
]
EQUITY = [EquityIn(date(2026, 9, 6), 1_000_000_000), EquityIn(date(2026, 9, 7), 1_500_000_000)]


def test_fifo_round_trips_partial_close_and_flip():
    h = profile(PERP_FILLS).holding
    # Lot A (2 @ 10:00 Mon) closed by fill 3: 2h. Lot B (1 @ 10:30): 0.5 closed
    # by fill 3 (1.5h), 0.5 by fill 4 on Tuesday 10:00 (23.5h). Fill 4's other
    # 1.5 flips short; fill 5 closes it 30 min later. Pieces: 7200, 5400,
    # 84600, 1800 seconds.
    assert h.n_round_trips == 4
    assert dict(h.buckets) == {"<1h": 1, "1-24h": 3, "1-7d": 0, "7-30d": 0, ">30d": 0}
    assert h.median_seconds == (5400 + 7200) / 2  # sorted 1800, 5400, 7200, 84600


def test_holding_bucket_edges_are_lower_inclusive():
    base = at(7, 10)
    gaps = [
        timedelta(minutes=59, seconds=59),  # <1h
        timedelta(hours=1),  # 1-24h
        timedelta(hours=24),  # 1-7d
        timedelta(days=7),  # 7-30d
        timedelta(days=30),  # >30d
    ]
    fills = []
    for i, gap in enumerate(gaps):
        inst = f"C{i}-PERP"
        fills += [perp(base, "buy", "1", "10", inst), perp(base + gap, "sell", "1", "10", inst)]
    h = profile(fills).holding
    assert [n for _, n in h.buckets] == [1, 1, 1, 1, 1]
    assert h.median_seconds == 86_400  # the 24h piece is the middle one


def test_open_lots_are_not_round_trips_and_unmatched_spot_sells_are_ignored():
    fills = [
        fill(at(7, 9), "ETH/USDC", T.SPOT, "sell", "3", "2000"),  # inventory from before
        fill(at(7, 10), "ETH/USDC", T.SPOT, "buy", "1", "2000"),
    ]
    p = profile(fills)
    assert p.holding is None  # nothing closed
    assert p.sizing.n_entries == 1 and p.sizing.scale_ins == 0  # the sell opened no short
    # A spot sell larger than the inventory closes it; the excess is ignored.
    fills.append(fill(at(7, 11), "ETH/USDC", T.SPOT, "sell", "5", "2100"))
    p = profile(fills)
    assert p.holding.n_round_trips == 1 and p.holding.median_seconds == 3600
    assert p.sizing.n_entries == 1


def test_trade_frequency():
    f = profile(PERP_FILLS).frequency
    # 5 fills on 2 active UTC days (span 2 days): 2.5 per active day, 5 x 7 / 2 per week.
    assert (f.n_fills, f.active_days, f.span_days) == (5, 2, 2)
    assert f.per_active_day == 2.5 and f.per_week == 17.5
    # Two fills 9 days apart: 2 active days over a 10 day span.
    f = profile([perp(at(1, 1), "buy", "1", "5"), perp(at(10, 1), "sell", "1", "5")]).frequency
    assert (f.active_days, f.span_days) == (2, 10) and f.per_week == pytest.approx(1.4)


def test_heatmap_bins_weekday_and_hour_in_utc():
    grid = profile(PERP_FILLS).heatmap
    assert grid[0][10] == 2 and grid[0][12] == 1 and grid[1][10] == 2
    assert sum(map(sum, grid)) == 5
    # 23:30 on Monday in UTC-5 is 04:30 Tuesday UTC.
    local = datetime(2026, 9, 7, 23, 30, tzinfo=timezone(timedelta(hours=-5)))
    grid = profile([perp(local, "buy", "1", "5")]).heatmap
    assert grid[1][4] == 1 and sum(map(sum, grid)) == 1


def test_instrument_shares_and_hhi():
    fills = [
        fill(at(7, 1), "BTC-PERP", T.PERP, "buy", "6", "100", decimals=5),  # 600
        fill(at(7, 2), "ETH-PERP", T.PERP, "buy", "3", "100", decimals=5),  # 300
        fill(at(7, 3), "SOL/USDC", T.SPOT, "buy", "1", "100"),  # 100
    ]
    i = profile(fills).instruments
    assert [(x.instrument, x.notional_micros) for x in i.top] == [
        ("BTC-PERP", 600_000_000),
        ("ETH-PERP", 300_000_000),
        ("SOL/USDC", 100_000_000),
    ]
    assert [x.share for x in i.top] == pytest.approx([0.6, 0.3, 0.1])
    assert i.n_instruments == 3 and i.hhi == pytest.approx(0.36 + 0.09 + 0.01)
    # Seven equal instruments: HHI = 1/7, only the top five listed (ties by name).
    even = [perp(at(7, k), "buy", "1", "10", f"I{k}-PERP") for k in range(1, 8)]
    i = profile(even).instruments
    assert i.hhi == pytest.approx(1 / 7) and len(i.top) == tp.TOP_N
    assert [x.instrument for x in i.top] == [f"I{k}-PERP" for k in range(1, 6)]


def test_sizing_share_of_equity_at_entry_and_scaling():
    s = profile(PERP_FILLS, equity=EQUITY).sizing
    # Entry 1: 2 x 100 = 200 over Sep 6 equity 1000 = 0.2. Entry 2 is fill 4's
    # flip remainder: 195 of its 260 notional (1.5 of 2 sold) over Sep 7 equity
    # 1500 = 0.13. Median of two = 0.165; p90 by nearest rank = the larger.
    assert (s.n_entries, s.n_sized) == (2, 2)
    assert s.median_share == pytest.approx(0.165) and s.p90_share == pytest.approx(0.2)
    # Fill 2 adds to the position; fill 3 reduces it without closing; the flip
    # (fill 4) and the close (fill 5) are neither.
    assert (s.scale_ins, s.scale_outs) == (1, 1)


def test_p90_is_nearest_rank():
    fills = []
    for k in range(10):  # ten entries sized 1..10% of a 1000 equity
        fills.append(perp(at(7, k), "buy", str(k + 1), "10", f"X{k}-PERP"))
    s = profile(fills, equity=[EquityIn(date(2026, 9, 6), 1_000_000_000)]).sizing
    assert s.median_share == pytest.approx(0.055)  # (50 + 60) / 2 / 1000
    assert s.p90_share == pytest.approx(0.09)  # ceil(0.9 x 10) = 9th of 10 -> 90 / 1000


def test_no_equity_leaves_shares_and_leverage_none_but_counts_stand():
    s = profile(PERP_FILLS).sizing  # no series at all
    assert s.median_share is None and s.p90_share is None and s.n_sized == 0
    assert (s.n_entries, s.scale_ins, s.scale_outs) == (2, 1, 1)
    assert profile(PERP_FILLS).perps.avg_leverage is None
    # An entry before the series starts, or on a non-positive equity day, is skipped.
    late = [EquityIn(date(2026, 9, 7), 1_500_000_000), EquityIn(date(2026, 9, 6), 0)]
    s = profile(PERP_FILLS, equity=late).sizing
    assert s.n_sized == 1 and s.median_share == pytest.approx(0.13)


def test_perp_leverage_skew_and_funding():
    funding = [
        FundingIn(at(7, 11), 500_000),  # received 0.50
        FundingIn(at(7, 12), -2_000_000),
        FundingIn(at(8, 0), -1_000_000),
    ]
    p = profile(PERP_FILLS, equity=EQUITY, funding=funding).perps
    # Gross perp notional after each fill over the prior day's equity:
    # 2 x 100 / 1000 = 0.2; 3 x 110 / 1000 = 0.33; 0.5 x 120 / 1000 = 0.06;
    # 1.5 x 130 / 1500 = 0.13 (short); flat = 0.
    assert p.n_fills == 5
    assert p.avg_leverage == pytest.approx((0.2 + 0.33 + 0.06 + 0.13 + 0) / 5)
    assert p.max_leverage == pytest.approx(0.33)
    # Opening notional: buys 200 + 110 = 310; sells 195 (the flip remainder).
    assert (p.long_notional_micros, p.short_notional_micros) == (310_000_000, 195_000_000)
    assert p.long_share == pytest.approx(310 / 505)
    # Paid = minus the sum of the legs: -(0.5 - 2 - 1) = 2.5 USD.
    assert p.funding_paid_micros == 2_500_000


def test_funding_none_versus_zero_and_no_perps():
    assert profile(PERP_FILLS, equity=EQUITY).perps.funding_paid_micros is None  # not available
    assert profile(PERP_FILLS, equity=EQUITY, funding=[]).perps.funding_paid_micros == 0
    spot = [fill(at(7, 10), "ETH/USDC", T.SPOT, "buy", "1", "2000")]
    assert profile(spot, funding=[FundingIn(at(7, 11), -5)]).perps is None


def test_all_long_has_full_long_share_and_all_short_a_zero_one():
    longs = [perp(at(7, 1), "buy", "1", "10"), perp(at(7, 2), "sell", "1", "10")]
    assert profile(longs).perps.long_share == 1.0
    shorts = [perp(at(7, 1), "sell", "1", "10"), perp(at(7, 2), "buy", "1", "10")]
    p = profile(shorts).perps
    assert p.long_share == 0.0 and p.short_notional_micros == 10_000_000


def test_atoms_of_different_scales_are_compared_as_quantities():
    # 1.5 at 5 decimals opens; 1500000 atoms at 6 decimals is the same 1.5 and closes it.
    a = perp(at(7, 1), "buy", "1.5", "10")
    b = fill(at(7, 2), "BTC-PERP", T.PERP, "sell", "1.5", "10", decimals=6)
    assert (a.qty_decimals, a.qty_atoms) == (5, 150_000) and b.qty_atoms == 1_500_000
    h = profile([a, b]).holding
    assert h.n_round_trips == 1 and h.median_seconds == 3600


# ---- prediction markets -----------------------------------------------------------


def binary_fills():
    d0 = at(7)
    return [
        fill(d0.replace(hour=9), "A", T.BINARY, "buy", "10", "0.15", outcome="yes"),
        fill(d0.replace(hour=9, minute=30), "A", T.BINARY, "buy", "10", "0.45", outcome="yes"),
        fill(d0.replace(hour=11), "A", T.BINARY, "sell", "5", "0.90", outcome="yes"),
        fill(d0.replace(hour=12), "B", T.BINARY, "buy", "4", "0.85", outcome="no"),
        fill(d0.replace(hour=12), "B", T.BINARY, "buy", "6", "0.20", outcome="yes"),
        fill(d0.replace(hour=13), "C", T.BINARY, "buy", "3", "0.70", outcome="yes"),
    ]


SETTLES = [SettlementIn(at(9, 9), "A", "yes"), SettlementIn(at(9, 9), "B", "yes")]


def test_binary_buckets_held_share_and_win_rate():
    b = profile(binary_fills(), settlements=SETTLES).binary
    by = {x.label: x for x in b.buckets}
    assert b.n_entries == 5
    # 0-20: the 10 @ 0.15; FIFO sells 5 early, 5 held to a yes win.
    assert (by["0-20"].n_entries, by["0-20"].held_share, by["0-20"].win_rate) == (1, 0.5, 1.0)
    # 20-40: 6 yes @ 0.20 (edge is lower-inclusive) held and won.
    assert (by["20-40"].held_share, by["20-40"].win_rate) == (1.0, 1.0)
    assert (by["40-60"].held_share, by["40-60"].win_rate) == (1.0, 1.0)
    # 60-80: C never settled, so neither share exists yet.
    assert by["60-80"].n_entries == 1
    assert by["60-80"].held_share is None and by["60-80"].win_rate is None
    # 80-100: 4 no @ 0.85 held; market settled yes, so a loss.
    assert (by["80-100"].held_share, by["80-100"].win_rate) == (1.0, 0.0)
    assert by["80-100"].decided_qty == 4.0
    # Held 5 + 6 + 10 + 4 = 25, sold early 5: 25 / 30.
    assert b.held_to_settlement == pytest.approx(25 / 30)


def test_binary_holding_includes_settlement_closes():
    h = profile(binary_fills(), settlements=SETTLES).holding
    # A: 5 sold at 11:00 (lot opened 09:00, 2h); the 5 left of that lot settle
    # 48h after 09:00 and the 10 @ 09:30 47.5h after; B's two lots (12:00)
    # settle 45h later. C is open.
    assert h.n_round_trips == 5
    assert dict(h.buckets) == {"<1h": 0, "1-24h": 1, "1-7d": 4, "7-30d": 0, ">30d": 0}
    assert h.median_seconds == 162_000  # sorted: 7200, 162000, 162000, 171000, 172800


def test_binary_void_result_is_held_but_not_decided():
    fills = [fill(at(7, 9), "V", T.BINARY, "buy", "2", "0.50", outcome="yes")]
    b = profile(fills, settlements=[SettlementIn(at(8, 9), "V", "void")]).binary
    mid = {x.label: x for x in b.buckets}["40-60"]
    assert mid.held_share == 1.0 and mid.win_rate is None and mid.decided_qty == 0.0


def test_price_bucket_edges():
    assert [tp._bucket(tp.parse_price(p)) for p in ("0", "0.1999", "0.2", "0.8", "0.99", "1")] == [
        0,
        0,
        1,
        4,
        4,
        4,
    ]
    with pytest.raises(ValueError):
        tp._bucket(tp.parse_price("1.01"))


# ---- copy feasibility -------------------------------------------------------------


def test_copy_median_gap_groups_partial_fills_into_trades():
    fills = [
        perp(at(7, 10, 0, 0), "buy", "1", "10", order="o1"),
        perp(at(7, 10, 0, 5), "buy", "1", "10", order="o1"),  # same order: not a new trade
        perp(at(7, 10, 0, 40), "buy", "1", "10", order="o2"),
        perp(at(7, 11, 0, 40), "sell", "1", "10", order="o3"),
        perp(at(7, 11, 0, 50), "sell", "1", "10"),  # no order id: its own trade
    ]
    c = profile(fills).copy
    # Trades at 10:00:00, 10:00:40, 11:00:40, 11:00:50 -> gaps 40, 3600, 10; median 40.
    assert c.n_trades == 4 and c.median_gap_seconds == 40
    assert c.copy_delay_seconds == DELAY


def test_too_fast_share_by_count_and_by_notional():
    # Round trips of the five-fill example: pieces of 7200, 5400, 84600 and 1800 s,
    # entry notionals 200 (2 @ 100), 55 (0.5 @ 110), 55 (0.5 @ 110) and 195
    # (the flip's 1.5 @ 130). With a 3600 s delay only the 1800 s piece is too fast.
    c = profile(PERP_FILLS, copy_delay_seconds=3600).copy
    assert (c.n_round_trips, c.n_too_fast) == (4, 1)
    assert c.too_fast_count_share == pytest.approx(1 / 4)
    assert c.too_fast_notional_share == pytest.approx(195 / 505)
    # Strictly shorter: a delay equal to a piece's duration does not count it.
    assert profile(PERP_FILLS, copy_delay_seconds=1800).copy.n_too_fast == 0
    assert profile(PERP_FILLS, copy_delay_seconds=1801).copy.n_too_fast == 1
    assert profile(PERP_FILLS, copy_delay_seconds=10**6).copy.too_fast_notional_share == 1.0


def test_a_small_fast_piece_and_a_large_slow_one_weigh_differently():
    fills = [
        perp(at(7, 10), "buy", "1", "10", "A-PERP"),  # notional 10
        perp(at(7, 10, 1), "sell", "1", "10", "A-PERP"),  # closed after 60 s
        perp(at(7, 12), "buy", "10", "100", "B-PERP"),  # notional 1000
        perp(at(7, 13), "sell", "10", "100", "B-PERP"),  # closed after 3600 s
    ]
    c = profile(fills).copy  # 300 s delay
    assert (c.n_round_trips, c.n_too_fast) == (2, 1)
    assert c.too_fast_count_share == pytest.approx(0.5)
    assert c.too_fast_notional_share == pytest.approx(10 / 1010)


def test_binary_settlement_pieces_count_at_entry_notional():
    fills = [fill(at(7, 9), "A", T.BINARY, "buy", "10", "0.40", outcome="yes")]  # notional 4
    c = profile(fills, settlements=[SettlementIn(at(7, 9, 2), "A", "yes")]).copy
    assert (c.n_round_trips, c.n_too_fast) == (1, 1) and c.too_fast_notional_share == 1.0
    assert c.median_gap_seconds is None  # one trade


def test_copy_none_without_round_trips_or_gaps():
    assert profile([perp(at(7, 1), "buy", "1", "10")]).copy is None
    # Two trades, nothing closed: the gap exists, the too-fast share does not.
    c = profile([perp(at(7, 1), "buy", "1", "10"), perp(at(7, 2), "buy", "1", "10")]).copy
    assert c.median_gap_seconds == 3600 and c.n_round_trips == 0
    assert c.too_fast_count_share is None and c.too_fast_notional_share is None
    # One order, one closed round trip: a share but no gap.
    twins = [perp(at(7, 1, s), side, "1", "10", order="o") for s, side in ((0, "buy"), (1, "sell"))]
    c = profile(twins).copy
    assert c.n_trades == 1 and c.median_gap_seconds is None and c.too_fast_count_share == 1.0


# ---- empty / short histories ------------------------------------------------------


def test_no_fills_is_all_none():
    p = profile([])
    assert p.n_fills == 0 and not p.enough_history
    for name in (
        "holding",
        "frequency",
        "heatmap",
        "instruments",
        "sizing",
        "perps",
        "binary",
        "copy",
    ):
        assert getattr(p, name) is None, name
    assert p.first_at is None and p.last_at is None


def test_enough_history_threshold_and_missing_sections():
    spot = [fill(at(7, h), "ETH/USDC", T.SPOT, "buy", "1", "2000") for h in range(1, 10)]
    p = profile(spot)
    assert p.n_fills == 9 and not p.enough_history
    assert p.perps is None and p.binary is None and p.holding is None
    p = profile([*spot, fill(at(7, 10), "ETH/USDC", T.SPOT, "buy", "1", "2000")])
    assert p.enough_history and p.n_fills == tp.MIN_FILLS


def test_inputs_are_validated():
    with pytest.raises(ValueError, match="timezone-aware"):
        perp(datetime(2026, 9, 7, 10), "buy", "1", "10")
    with pytest.raises(ValueError, match="buy or sell"):
        perp(at(7), "hold", "1", "10")
    with pytest.raises(TypeError):
        FillIn(at(7), "X", T.PERP, "buy", 1, 5, 10.5, 1)  # prices are decimal strings
    with pytest.raises(ValueError, match="timezone-aware"):
        FundingIn(datetime(2026, 9, 7), 1)
