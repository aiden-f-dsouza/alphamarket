"""valuation.LedgerSweep: the per-instant valuation functions in one forward pass.

The sweep must answer EXACTLY what holdings_at / perp_positions_at /
equity_before answer at every instant (scales, flags and flows included), so
most of this file compares the two on seeded generated ledgers. A small
hand-computed case pins the semantics directly, and the scaling guard counts
the work done instead of timing it.
"""

import random
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from fractions import Fraction

import pytest

from alphamarket import valuation
from alphamarket.connectors.base import LegKind
from alphamarket.valuation import (
    AnchorIn,
    FlagReason,
    LedgerSweep,
    LegIn,
    PerpFillIn,
    PriceIn,
    Qty,
    basis_day,
    equity_before,
    holdings_at,
    parse_price,
    perp_positions_at,
)

M = 1_000_000
D0 = date(2026, 3, 1)
DAYS = 12
PLUS5 = timezone(timedelta(hours=5))  # same instants, another offset: UTC days still rule


def at(day: date, hour: int = 12, minute: int = 0, tz=UTC) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC).astimezone(tz)


# ---- a seeded ledger generator ----------------------------------------------------------------

# asset -> the decimals its legs may use; a finer one arriving mid-sweep widens the scale.
SPOT_DECIMALS = {
    "USD": (6, 2),
    "USDC": (6,),
    "ETH": (8, 12),
    "BTC": (8, 10),
    "AAPL": (0, 4),
    "SOL": (9,),  # no close ever: UNPRICED
    "DOGE": (8,),  # closes only from day 6 on: UNPRICED, then priced
}
PERP_DECIMALS = {"BTC-PERP": (5, 6), "ETH-PERP": (4,), "SOL-PERP": (2,)}
LEG_KINDS = (
    LegKind.FILL_BASE,
    LegKind.FILL_QUOTE,
    LegKind.FEE,
    LegKind.DEPOSIT,
    LegKind.WITHDRAWAL,
    LegKind.TRANSFER_IN,
    LegKind.TRANSFER_OUT,
    LegKind.STAKING,
    LegKind.FUNDING,
    LegKind.SPLIT,
)
BASE_PRICE = {"ETH": 3_000, "BTC": 60_000, "AAPL": 200, "DOGE": 0, "BTC-PERP": 60_000}

ANCHOR = (
    AnchorIn("USD", 10_000 * M, 6),
    AnchorIn("ETH", 2 * 10**8, 8),
    AnchorIn("AAPL", 10, 0),
    AnchorIn("BTC-PERP", 50_000, 5, entry_price="60000"),
    AnchorIn("ETH-PERP", -10_000, 4),  # opened before the anchor, entry unknown
    AnchorIn("SOL", 0, 9),  # a zero anchor row is never a holding
)


def _instant(rng: random.Random) -> datetime:
    """Few distinct times per day, so legs and fills often share an instant."""
    day = D0 + timedelta(days=rng.randrange(DAYS))
    hour, minute = rng.choice([(0, 0), (6, 0), (12, 0), (12, 30), (18, 0), (23, 59)])
    return at(day, hour, minute, tz=PLUS5 if rng.random() < 0.2 else UTC)


def _close_str(rng: random.Random, asset: str) -> str:
    if asset == "DOGE":
        return f"0.{rng.randrange(10**7, 10**8):08d}"  # sub-cent: the exact close matters
    whole = BASE_PRICE[asset] * rng.randrange(80, 120) // 100
    return f"{whole}.{rng.randrange(10**4):04d}"


def _prices(rng: random.Random) -> list[PriceIn]:
    out = []
    for n in range(-2, DAYS):
        day = D0 + timedelta(days=n)
        for asset in ("ETH", "BTC", "AAPL", "DOGE", "BTC-PERP"):
            if asset == "DOGE" and n < 6:
                continue
            if asset == "BTC-PERP" and n < 5:
                continue  # before day 5 a BTC-PERP is valued at BTC's close (the cut's has())
            if n > -2 and rng.random() < 0.3:
                continue  # a missing day: the last close is carried forward
            close = _close_str(rng, asset)
            micros = int(parse_price(close) * M)
            # Some rows carry the exact decimal close, some only the micros.
            out.append(PriceIn(asset, day, micros, close=close if rng.random() < 0.5 else None))
    rng.shuffle(out)  # input order never matters
    return out


def _legs(rng: random.Random, n: int) -> list[LegIn]:
    legs = []
    for _ in range(n):
        if rng.random() < 0.05:  # a perp leg the perp fills may disagree with
            asset = rng.choice(sorted(PERP_DECIMALS))
            decimals = rng.choice(PERP_DECIMALS[asset])
        else:
            asset = rng.choice(sorted(SPOT_DECIMALS))
            decimals = rng.choice(SPOT_DECIMALS[asset])
        fine = 10 ** max(decimals - 6, 0)  # 12 dp legs keep non-zero low digits
        size = rng.randrange(1, 5 * 10 ** min(decimals, 6)) * fine + rng.randrange(fine)
        if asset in ("USD", "USDC"):
            size *= 1_000
        kind = rng.choice(LEG_KINDS)
        sign = -1 if kind in (LegKind.FEE, LegKind.WITHDRAWAL, LegKind.TRANSFER_OUT) else 1
        if kind in (LegKind.FILL_BASE, LegKind.FILL_QUOTE, LegKind.FUNDING, LegKind.SPLIT):
            sign = rng.choice((-1, 1))
        legs.append(LegIn(asset, sign * size, decimals, kind, _instant(rng)))
    return legs


def _perp_fills(rng: random.Random, n: int) -> list[PerpFillIn]:
    """Opens, adds, partial reduces, exact closes, flips through zero and
    zero-quantity fills, simulated in time order so the shapes really occur."""
    instants = sorted(_instant(rng) for _ in range(n))
    position = {"BTC-PERP": 50_000 * 10, "ETH-PERP": -10_000, "SOL-PERP": 0}  # at max dp
    fills = []
    for when in instants:
        asset = rng.choice(sorted(PERP_DECIMALS))
        decimals = rng.choice(PERP_DECIMALS[asset])
        unit = 10 ** (max(PERP_DECIMALS[asset]) - decimals)
        q = position[asset] // unit  # current position at this fill's scale (approx.)
        shape = rng.choice(("open_add", "reduce", "close", "flip", "zero"))
        if shape == "open_add" or q == 0:
            d = rng.choice((-1, 1)) * rng.randrange(1, 10**decimals)
            if q:
                d = abs(d) if q > 0 else -abs(d)
        elif shape == "reduce":
            d = -q // 2 if abs(q) > 1 else -q
        elif shape == "close":
            d = -q
        elif shape == "flip":
            d = -2 * q - (1 if q > 0 else -1)
        else:
            d = 0
        position[asset] += d * unit
        price = f"{rng.randrange(100, 70_000)}.{rng.randrange(100):02d}"
        fills.append(PerpFillIn(asset, when, d, decimals, price))
    rng.shuffle(fills)  # equal instants keep this (input) order in both paths
    return fills


def _query_instants(rng, legs, fills) -> list[datetime]:
    """Every leg and fill instant, a few in-between ones, day boundaries, repeats."""
    instants = {leg.occurred_at for leg in legs} | {f.occurred_at for f in fills}
    instants |= {_instant(rng) + timedelta(seconds=rng.randrange(1, 3600)) for _ in range(20)}
    instants |= {at(D0 + timedelta(days=n), 0) for n in range(-1, DAYS + 2)}
    ordered = sorted(instants)
    return sorted(ordered + rng.sample(ordered, len(ordered) // 4))  # some asked twice


def _check_equivalence(seed: int, n_legs: int, n_fills: int) -> dict[str, object]:
    rng = random.Random(seed)
    prices = _prices(rng)
    legs = _legs(rng, n_legs)
    fills = _perp_fills(rng, n_fills)
    sweep = LedgerSweep(ANCHOR, legs, prices, fills)
    seen: dict[str, object] = {"flags": set(), "flows": 0, "scales": {}, "signs": {}, "unknown": 0}
    for t in _query_instants(rng, legs, fills):
        legs_before = [leg for leg in legs if leg.occurred_at < t]
        fills_before = [f for f in fills if f.occurred_at < t]
        expected_held = holdings_at(ANCHOR, legs_before, t)
        expected_perps = perp_positions_at(ANCHOR, fills_before, t)
        expected_value = equity_before(ANCHOR, legs, prices, fills, t)
        # Ask in a seeded order: answers at one instant never depend on it.
        asks = ["held", "perps", "value"]
        rng.shuffle(asks)
        got = {}
        for ask in asks:
            if ask == "held":
                got[ask] = sweep.holdings_before(t)
            elif ask == "perps":
                got[ask] = sweep.perp_positions_before(t)
            else:
                got[ask] = sweep.equity_before(t)
        assert got["held"] == expected_held, t
        assert got["perps"] == expected_perps, t
        assert got["value"] == expected_value, t
        assert basis_day(got["value"]) == basis_day(expected_value)
        # What the run exercised (asserted by the callers, so the test is not vacuous).
        seen["flags"] |= {f.reason for f in expected_value.flags}
        seen["flows"] += expected_value.flow_micros != 0
        seen["unknown"] += expected_value.equity_micros <= 0
        for asset, qty in expected_held.items():
            seen["scales"].setdefault(asset, set()).add(qty.decimals)
        for asset, pos in expected_perps.items():
            seen["signs"].setdefault(asset, set()).add(pos.qty.atoms > 0)
    return seen


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_sweep_equals_the_per_instant_functions_on_generated_ledgers(seed):
    seen = _check_equivalence(seed, n_legs=160, n_fills=60)
    assert {
        FlagReason.CARRIED_FORWARD,
        FlagReason.UNPRICED,
        FlagReason.PERP_ENTRY_UNKNOWN,
        FlagReason.PERP_QTY_MISMATCH,
    } <= seen["flags"]
    assert seen["flows"] > 0  # same-day deposits/withdrawals before t
    assert seen["scales"]["ETH"] == {8, 12}  # anchored at 8 dp, widened mid-sweep
    assert seen["scales"]["AAPL"] == {0, 4}
    assert any(signs == {True, False} for signs in seen["signs"].values())  # a perp flipped


def test_an_empty_ledger_and_anchor_only():
    sweep = LedgerSweep(ANCHOR, [], [], [])
    t = at(D0, 12)
    assert sweep.holdings_before(t) == holdings_at(ANCHOR, [], t)
    assert sweep.perp_positions_before(t) == perp_positions_at(ANCHOR, [], t)
    assert sweep.equity_before(t) == equity_before(ANCHOR, [], [], [], t)
    empty = LedgerSweep([], [], [], [])
    assert empty.holdings_before(t) == {} and empty.equity_before(t).equity_micros == 0


# ---- hand-computed ------------------------------------------------------------------------------


def test_hand_computed_holdings_flows_and_equity():
    # Anchor $1,000. D 10:00 buy 1 ETH for $3,000 (cash goes negative). D 11:00
    # deposit $500. D 11:00 a 0.000000000001 ETH (12 dp) fee. ETH closes: 2,900 on
    # D-1, 3,100 on D (never used on D).
    d = D0
    anchor = [AnchorIn("USD", 1_000 * M, 6)]
    legs = [
        LegIn("ETH", 10**8, 8, LegKind.FILL_BASE, at(d, 10)),
        LegIn("USD", -3_000 * M, 6, LegKind.FILL_QUOTE, at(d, 10)),
        LegIn("USD", 500 * M, 6, LegKind.DEPOSIT, at(d, 11)),
        LegIn("ETH", -1, 12, LegKind.FEE, at(d, 11)),
    ]
    prices = [PriceIn("ETH", d - timedelta(days=1), 2_900 * M), PriceIn("ETH", d, 3_100 * M)]
    sweep = LedgerSweep(anchor, legs, prices, [])

    # At 10:00 nothing is before: $1,000 cash, no flow.
    v = sweep.equity_before(at(d, 10))
    assert (v.equity_micros, v.flow_micros, basis_day(v)) == (1_000 * M, 0, None)
    # At 11:00 the 10:00 legs are in: 1 ETH x 2,900 - 2,000 = 900.
    assert sweep.holdings_before(at(d, 11)) == {"USD": Qty(-2_000 * M, 6), "ETH": Qty(10**8, 8)}
    v = sweep.equity_before(at(d, 11))
    assert (v.equity_micros, v.flow_micros, basis_day(v)) == (900 * M, 0, d - timedelta(days=1))
    # At 12:00: the deposit is a day-D flow; ETH is now held at 12 dp.
    held = sweep.holdings_before(at(d, 12))
    assert held == {"USD": Qty(-1_500 * M, 6), "ETH": Qty(10**12 - 1, 12)}
    v = sweep.equity_before(at(d, 12))
    # 0.999999999999 x 2,900 = 2,899.9999999971 -> 2,899.999999 (truncated); - 1,500.
    assert (v.equity_micros, v.flow_micros) == (1_399_999_999, 500 * M)
    # Next day: the D-day flow is no longer "today's"; D's close (3,100) now values ETH.
    v = sweep.equity_before(at(d + timedelta(days=1), 0, 1))
    assert (v.flow_micros, basis_day(v)) == (0, d)
    assert v.equity_micros == int(Fraction(10**12 - 1, 10**12) * 3_100 * M) - 1_500 * M


def test_perp_book_widens_and_flips_like_the_batch_book():
    # Anchor long 0.5 at 60,000 (5 dp). A 6 dp fill sells 1.000001: a flip
    # through zero to short 0.500001 at the flip price.
    anchor = [AnchorIn("BTC-PERP", 50_000, 5, entry_price="60000")]
    fills = [PerpFillIn("BTC-PERP", at(D0, 10), -1_000_001, 6, "61000")]
    sweep = LedgerSweep(anchor, [], [], fills)
    before = sweep.perp_positions_before(at(D0, 10))["BTC-PERP"]
    assert (before.qty, before.entry) == (Qty(50_000, 5), Fraction(60_000))
    after = sweep.perp_positions_before(at(D0, 11))["BTC-PERP"]
    assert (after.qty, after.entry) == (Qty(-500_001, 6), Fraction(61_000))


def test_instants_must_not_go_back():
    sweep = LedgerSweep(ANCHOR, [], [], [])
    sweep.holdings_before(at(D0, 12))
    sweep.equity_before(at(D0, 12))  # the same instant again is fine
    with pytest.raises(ValueError, match="non-decreasing"):
        sweep.perp_positions_before(at(D0, 11))
    with pytest.raises(ValueError, match="timezone-aware"):
        LedgerSweep(ANCHOR, [], [], []).equity_before(datetime(2026, 3, 1, 12))


def test_price_index_before_matches_an_index_of_the_earlier_closes():
    rng = random.Random(7)
    prices = _prices(rng)
    full = valuation._PriceIndex(prices)
    for n in range(-3, DAYS + 2):
        cut = D0 + timedelta(days=n)
        filtered = valuation._PriceIndex([p for p in prices if p.day < cut])
        view = full.before(cut)
        for asset in ("ETH", "BTC", "AAPL", "DOGE", "BTC-PERP", "ETH-PERP", "SOL", "SOL-PERP"):
            assert view.has(asset) == filtered.has(asset), (asset, cut)
            for m in range(-3, DAYS + 2):
                day = D0 + timedelta(days=m)
                assert view.close(asset, day) == filtered.close(asset, day), (asset, cut, day)
                if asset.endswith(valuation.PERP_SUFFIX):
                    assert view.perp_close(asset, day) == filtered.perp_close(asset, day)
    # BTC-PERP has own closes only from day 5: before then the view falls back to BTC.
    early = full.before(D0 + timedelta(days=3))
    assert not early.has("BTC-PERP") and full.has("BTC-PERP")
    assert early.perp_close("BTC-PERP", D0 + timedelta(days=3)) == early.close(
        "BTC", D0 + timedelta(days=3)
    )


# ---- scaling guard (counts work, never the clock) -----------------------------------------------


def _spread(rng: random.Random, rows):
    """The same rows at distinct instants (any second of the period), so the
    number of instants grows with the number of rows."""
    span = DAYS * 86_400
    return [
        replace(r, occurred_at=at(D0, 0) + timedelta(seconds=rng.randrange(span))) for r in rows
    ]


def test_each_leg_and_fill_is_applied_once_however_many_instants(monkeypatch):
    rng = random.Random(11)
    prices = _prices(rng)
    legs = _spread(rng, _legs(rng, 3_000))
    fills = _spread(rng, _perp_fills(rng, 600))
    instants = sorted({leg.occurred_at for leg in legs} | {f.occurred_at for f in fills})
    assert len(instants) > 3_000

    rescales = []
    real_rescale = valuation._rescale
    monkeypatch.setattr(valuation, "_rescale", lambda *a: rescales.append(a) or real_rescale(*a))
    applied = []
    real_apply = valuation._PerpBook.apply
    monkeypatch.setattr(
        valuation._PerpBook, "apply", lambda self, f: applied.append(f) or real_apply(self, f)
    )
    indexes = []
    real_init = valuation._PriceIndex.__init__
    monkeypatch.setattr(
        valuation._PriceIndex,
        "__init__",
        lambda self, p: indexes.append(1) or real_init(self, p),
    )

    sweep = LedgerSweep(ANCHOR, legs, prices, fills)
    for t in instants:
        sweep.holdings_before(t)
        sweep.perp_positions_before(t)
        sweep.equity_before(t)

    # O(N + K): one rescale per anchor row and leg (plus one per perp anchor seed,
    # perp fill, and scale widening of an asset: a handful), however many instants.
    widenings = sum(len(d) for d in SPOT_DECIMALS.values()) + sum(
        len(d) for d in PERP_DECIMALS.values()
    )
    assert len(rescales) <= 2 * len(ANCHOR) + len(legs) + len(fills) + 2 * widenings
    assert len(applied) == len(fills)
    assert len(indexes) == 1  # closes indexed once; each instant reads a view
    # Per instant, the batch functions re-sum every earlier leg: ~K x N / 2 rescales.
    assert len(instants) * len(legs) // 2 > 1_000 * len(rescales)
