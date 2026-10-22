"""Lighter's two derived ledger streams, as pure functions: realized PnL by
average cost (connectors.lighter.apply_trade) and hourly funding
(funding_payment), each with hand-computed values, then checked against
Lighter's own figures in captures of 2026-10-22 (tests/fixtures/lighter/,
provenance in its README). No network (invariant 6)."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from alphamarket.connectors.lighter import (
    LighterError,
    apply_trade,
    funding_payment,
    round_atoms,
)

FIXTURES = Path(__file__).parent / "fixtures" / "lighter"
DAY = "2026-10-22"


def fx(name: str):
    return json.loads((FIXTURES / f"{name}_{DAY}.json").read_text(), parse_float=Decimal)


def quote_at(price_micros_per_unit: int, size_decimals: int):
    """exit_quote_of for a fill at one price: qty atoms -> micros."""
    return lambda qty: price_micros_per_unit * qty // 10**size_decimals


# ---- average cost: the plan's worked example ---------------------------------------------


def test_average_cost_open_add_close_flip_and_cover():
    """Sizes at 2 dp (atoms), quotes in micros.

    open  buy 2.00 @ 100: entry quote 200.00, nothing realized
    add   buy 1.00 @ 130: entry quote 330.00 (average 110)
    close sell 1.50 @ 120: keep floor(330 x 1.50 / 3.00) = 165.00, closed 165.00,
          realized 1.50 x 120 - 165 = 180 - 165 = +15.00; long 1.50 @ 110 left
    flip  sell 2.50 @ 90: closes 1.50 at 90 = 135.00, realized 135 - 165 = -30.00;
          opens short 1.00 @ 90 (entry quote 225 - 135 = 90.00)
    cover buy 0.40 @ 80: keep floor(90 x 0.60 / 1.00) = 54.00, closed 36.00,
          realized (short) 36.00 - 0.40 x 80 = 36 - 32 = +4.00"""
    pos, entry, pnl = apply_trade(0, 0, 200, 200_000_000, quote_at(100_000_000, 2))
    assert (pos, entry, pnl) == (200, 200_000_000, 0)
    pos, entry, pnl = apply_trade(pos, entry, 100, 130_000_000, quote_at(130_000_000, 2))
    assert (pos, entry, pnl) == (300, 330_000_000, 0)
    pos, entry, pnl = apply_trade(pos, entry, -150, 180_000_000, quote_at(120_000_000, 2))
    assert (pos, entry, pnl) == (150, 165_000_000, 15_000_000)
    pos, entry, pnl = apply_trade(pos, entry, -250, 225_000_000, quote_at(90_000_000, 2))
    assert (pos, entry, pnl) == (-100, 90_000_000, -30_000_000)
    pos, entry, pnl = apply_trade(pos, entry, 40, 32_000_000, quote_at(80_000_000, 2))
    assert (pos, entry, pnl) == (-60, 54_000_000, 4_000_000)


def test_exact_close_realizes_the_whole_entry_and_flattens():
    """long 3 @ 10 (entry 30.00) sold 3 @ 12: realized 36 - 30 = +6.00, flat."""
    assert apply_trade(3, 30_000_000, -3, 36_000_000, quote_at(12_000_000, 0)) == (0, 0, 6_000_000)


def test_entry_quote_after_a_decrease_is_floored_to_the_micro():
    """Lighter's first REST row (trade 35897949034, LIT perp, maker bid):
    short 705,999.99 with entry quote 2,880,193.010572; buys 80.66 @ 3.7203.

      quote  = 80.66 x 3.7203 = 300.079398
      keep   = floor(2,880,193.010572 x 705,919.33 / 705,999.99)
             = floor(2,879,863.950555...) = 2,879,863.950555
      closed = 2,880,193.010572 - 2,879,863.950555 = 329.060017
      pnl    = closed - quote = 329.060017 - 300.079398 = +28.980619
    which are the next row's entry quote and this row's bid_account_pnl."""
    pos, entry, pnl = apply_trade(
        -70_599_999, 2_880_193_010_572, 8_066, 300_079_398, quote_at(3_720_300, 2)
    )
    assert (pos, entry, pnl) == (-70_591_933, 2_879_863_950_555, 28_980_619)


def test_replay_matches_lighters_entry_and_pnl_on_every_rest_row():
    """trades_714710_perps: 100 consecutive LIT-perp trades of index 714710
    from REST (a cross-check reference only; the connector never calls REST
    trades). From the first row's `*_entry_quote_before`, the replay must give
    every next row's position and entry quote and every row's
    `*_account_pnl` exactly (Lighter's figures are gross of the fee)."""
    rows = fx("trades_714710_perps")
    me = 714710

    def side(r):
        me_ask = r["ask_account_id"] == me
        taker_ask = not r["is_maker_ask"]
        role = "taker" if me_ask == taker_ask else "maker"
        return role, me_ask

    role, _ = side(rows[0])
    pos = int(Decimal(rows[0][f"{role}_position_size_before"]) * 100)
    entry = int(Decimal(rows[0][f"{role}_entry_quote_before"]) * 1_000_000)
    for r in rows:
        role, me_ask = side(r)
        assert pos == int(Decimal(r[f"{role}_position_size_before"]) * 100), r["trade_id"]
        assert entry == int(Decimal(r[f"{role}_entry_quote_before"]) * 1_000_000), r["trade_id"]
        qty = int(Decimal(r["size"]) * 100)
        price = int(Decimal(r["price"]) * 1_000_000)
        quote = int(Decimal(r["usd_amount"]) * 1_000_000)
        assert quote == price * qty // 100  # price x size is exact at 6 dp
        pos, entry, pnl = apply_trade(
            pos, entry, -qty if me_ask else qty, quote, quote_at(price, 2)
        )
        venue_pnl = r.get("ask_account_pnl" if me_ask else "bid_account_pnl")
        assert pnl == int(Decimal(venue_pnl) * 1_000_000), r["trade_id"]


# ---- funding ---------------------------------------------------------------------------------


def test_funding_payment_sign_and_scale():
    """BTC (size 5 dp), long 0.06000 = 6,000 atoms, value 0.84553400 USD per BTC.
    Longs pay: -6,000 x 84,553,400 = -507,320,400,000 at 5 + 8 = 13 dp
    = -0.0507320400000 USDC, which is -50,732,040,000 at 12 dp (exact).
    Shorts pay (direction "short"): the long receives +0.05073204."""
    assert funding_payment(6_000, 5, "0.84553400", "long") == (-50_732_040_000, 12)
    assert funding_payment(6_000, 5, "0.84553400", "short") == (50_732_040_000, 12)
    # A short pays when longs receive: short 2.00 SOL (2 dp) x 0.00012000 short-pays
    # = -(200 x 12,000) at 10 dp = -0.0024000000.
    assert funding_payment(-200, 2, "0.00012000", "short") == (-2_400_000, 10)
    assert funding_payment(-200, 2, "0.00012000", "long") == (2_400_000, 10)
    with pytest.raises(LighterError):
        funding_payment(1, 2, "0.1", "sideways")


def test_funding_rounds_half_even_only_past_12_dp():
    """1 atom of BTC (0.00001) x 0.00000005 = 5e-13: half-even at 12 dp -> 0.
    1 atom x 0.00000015 = 1.5e-12 -> 2e-12. 3 atoms x 0.00000005 = 1.5e-12 -> 2e-12;
    1 atom x 0.00000025 = 2.5e-12 -> 2e-12 (ties to even)."""
    assert funding_payment(1, 5, "0.00000005", "short") == (0, 12)
    assert funding_payment(1, 5, "0.00000015", "short") == (2, 12)
    assert funding_payment(3, 5, "0.00000005", "short") == (2, 12)
    assert funding_payment(1, 5, "0.00000025", "short") == (2, 12)
    assert round_atoms(123, 12) == (123, 12)


def _signed_hours(name: str) -> dict[int, Decimal]:
    return {
        int(r["timestamp"]): Decimal(r["value"]) * (1 if r["direction"] == "long" else -1)
        for r in fx(name)["fundings"]
    }


def _ts(iso: str) -> int:
    from datetime import datetime

    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def test_prefix_sum_differences_equal_the_hourly_values():
    """Lighter's `funding_rate_prefix_sum` (TradeWithFunding rows) is USD per
    base atom x 1e12, so per base unit it is x 10^(12 - size_decimals):
    BTC (5 dp) 1e7, LIT (2 dp) 1e10. Between two checkpoints it rises by the
    hourly values in between (direction "long" counts +):

      BTC 2026-09-28 09:08 -> 10-01 08:33: 114,968,098,359 - 114,766,364,528
          = 201,733,831 = 20.1733831 x 1e7, and the 71 hourly values sum to
          20.17338310 exactly.
      LIT 2026-10-22 15:40 -> 16:04: 2,288,258,804 - 2,288,476,016 = -217,212
          = -0.0000217212 x 1e10; the one hour (16:00, shorts pay) prints
          0.00002172 (8 dp), within half a unit of the 8th place per hour."""
    btc = _signed_hours("fundings_1_sep")
    first, last = _ts("2026-09-28T09:08:48Z"), _ts("2026-10-01T08:33:03Z")
    total = sum(v for h, v in btc.items() if first < h <= last)
    assert total * 10**7 == (114_968_098_359 - 114_766_364_528)

    lit = _signed_hours("fundings_120")
    rows = sorted(fx("explorer_logs_TradeWithFunding"), key=lambda r: r["time"])
    checks = 0
    for a, b in zip(rows, rows[1:], strict=False):
        pa = a["pubdata"]["trade_pubdata_with_funding"]
        pb = b["pubdata"]["trade_pubdata_with_funding"]
        hours = [h for h in lit if _ts(a["time"]) < h <= _ts(b["time"])]
        delta = Decimal(pb["funding_rate_prefix_sum"] - pa["funding_rate_prefix_sum"])
        values = sum((lit[h] for h in hours), Decimal(0))
        assert abs(delta / 10**10 - values) <= Decimal("0.000000005") * max(1, len(hours))
        checks += 1
    assert checks == 4
