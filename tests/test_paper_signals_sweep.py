"""emit_asset_signals over valuation.LedgerSweep: output identical to the
per-instant evaluation it replaced, in one pass over the ledger.

Each seeded account is emitted in two syncs. Before each, the expected Signal
columns are computed the ORIGINAL way (holdings_at / perp_positions_at /
equity_before called afresh at every distinct fill instant, the code this
replaced, kept here as the oracle); the emit itself runs with those
per-instant functions patched to raise and with the leg rescales counted.
"""

import random
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from fractions import Fraction
from itertools import groupby

import pytest
from sqlalchemy import select

from alphamarket import paper_signals, prices, valuation, valuation_db
from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import aware
from alphamarket.models import (
    AnchorBalance,
    AssetFill,
    AssetQuote,
    Instrument,
    LedgerEntry,
    PriceDaily,
    Signal,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.valuation import parse_price

M = 1_000_000
D = date(2026, 9, 10)
DM1 = D - timedelta(days=1)
DAYS = 5  # fills on D .. D+4
TIMES = [(0, 0), (9, 0), (12, 0), (12, 0), (16, 30), (23, 59)]  # 12:00 twice: more ties


def at(day: date, hour: int = 12, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def quotes(monkeypatch):
    def capture(db, venue, venue_env, instrument, limiter):
        return AssetQuote(
            instrument_id=instrument.id,
            bid="1.5",
            ask="2.5",
            last="2",
            as_of=at(D, 12, 2),
            source=venue,
        )

    monkeypatch.setattr(prices, "capture_quote", capture)


# ---- the oracle: the per-instant evaluation emit_asset_signals used before the sweep ----------


def _oracle(db, strategy) -> dict[int, dict]:
    """Expected Signal columns per pending asset_fill_id, computed exactly as
    emit_asset_signals did before LedgerSweep: every per-instant function
    re-run over the whole ledger at each distinct pending fill instant."""
    account = db.get(VenueAccount, strategy.venue_account_id)
    inputs = valuation_db.account_inputs(db, account)
    pending = paper_signals._unsignalled_fills(db, account.id, inputs.anchor_at)
    if not pending:
        return {}
    walk = paper_signals._fills_from(db, account.id, aware(pending[0][0].filled_at))
    pending_ids = {f.id for f, _ in pending}
    max_lag = timedelta(seconds=get_profile(account.venue).max_copy_lag_seconds)
    last_day = max(aware(f.filled_at) for f, _ in pending).date()
    price_rows = valuation_db.price_inputs(
        db,
        paper_signals._price_assets(inputs),
        None,
        last_day,
        valuation_db.pricing_venues(account),
    )
    out: dict[int, dict] = {}
    for t, grouped in groupby(walk, key=lambda row: aware(row[0].filled_at)):
        group = list(grouped)
        if not any(f.id in pending_ids for f, _ in group):
            continue
        v = valuation.equity_before(inputs.anchor, inputs.legs, price_rows, inputs.perp_fills, t)
        equity, basis = (
            (v.equity_micros, valuation.basis_day(v)) if v.equity_micros > 0 else (0, None)
        )
        legs_before = [leg for leg in inputs.legs if leg.occurred_at < t]
        fills_before = [f for f in inputs.perp_fills if f.occurred_at < t]
        spot = valuation.holdings_at(inputs.anchor, legs_before, t)
        perps = valuation.perp_positions_at(inputs.anchor, fills_before, t)
        running: dict[str, tuple[int, int]] = {}
        for fill, inst in group:
            base, d = inst.base_asset, fill.qty_decimals
            if base in running:
                atoms, scale = running[base]
                before = int(Fraction(atoms, 10**scale) * 10**d)
            elif inst.type == InstrumentType.PERP:
                pos = perps.get(base)
                before = int(pos.qty.units * 10**d) if pos is not None else 0
            else:
                held = spot.get(base)
                before = int(held.units * 10**d) if held is not None else 0
            after = before + (fill.qty_atoms if fill.action == "buy" else -fill.qty_atoms)
            running[base] = (after, d)
            if fill.id not in pending_ids:
                continue
            fresh = aware(fill.ingested_at) - aware(fill.filled_at) <= max_lag
            out[fill.id] = {
                "ticker": inst.symbol,
                "side": paper_signals._side(before, after, fill.action),
                "action": fill.action,
                "qty_atoms": fill.qty_atoms,
                "qty_decimals": d,
                "price": fill.price,
                "notional_micros": paper_signals.notional_usd_micros(
                    fill.qty_atoms, d, fill.price, inst.quote_asset
                ),
                "position_before_atoms": before,
                "position_after_atoms": after,
                "creator_equity_micros": equity,
                "equity_basis_day": basis,
                "filled_at": t,
                "quote_last": "2" if fresh else None,
            }
    return out


def _emitted(db, strategy, fill_ids) -> dict[int, dict]:
    rows = db.scalars(
        select(Signal).where(Signal.strategy_id == strategy.id, Signal.asset_fill_id.in_(fill_ids))
    ).all()
    return {
        s.asset_fill_id: {
            "ticker": s.ticker,
            "side": s.side,
            "action": s.action,
            "qty_atoms": s.qty_atoms,
            "qty_decimals": s.qty_decimals,
            "price": s.price,
            "notional_micros": s.notional_micros,
            "position_before_atoms": s.position_before_atoms,
            "position_after_atoms": s.position_after_atoms,
            "creator_equity_micros": s.creator_equity_micros,
            "equity_basis_day": s.equity_basis_day,
            "filled_at": aware(s.filled_at),
            "quote_last": s.quote_last,
        }
        for s in rows
    }


@contextmanager
def _sweep_only(monkeypatch, counter: list):
    """The per-instant functions raise; every _rescale (one per leg, perp fill
    and anchor row in a sweep) is counted."""

    def boom(*args, **kwargs):
        raise AssertionError("per-instant valuation called from emit_asset_signals")

    real_rescale = valuation._rescale
    with monkeypatch.context() as m:
        for name in (
            "holdings_at",
            "perp_positions_at",
            "equity_before",
            "valuation_on",
            "daily_equity_series",
        ):
            m.setattr(valuation, name, boom)
            m.setattr(paper_signals, name, boom, raising=False)
        m.setattr(valuation, "_rescale", lambda *a: counter.append(a) or real_rescale(*a))
        yield


def _emit_and_compare(db, strategy, monkeypatch) -> dict[int, dict]:
    expected = _oracle(db, strategy)
    account = db.get(VenueAccount, strategy.venue_account_id)
    inputs = valuation_db.account_inputs(db, account)
    rescales: list = []
    with _sweep_only(monkeypatch, rescales):
        assert paper_signals.emit_asset_signals(db, strategy) == len(expected)
    assert _emitted(db, strategy, list(expected)) == expected
    # One pass: anchor rows (spot + perp seed), each leg and perp fill once, and
    # a rescale per scale widening (a handful of assets).
    bound = 2 * len(inputs.anchor) + len(inputs.legs) + len(inputs.perp_fills) + 20
    assert len(rescales) <= bound
    return expected


# ---- seeded accounts ------------------------------------------------------------------------


class Book:
    """Inserts an account's rows; unique venue refs per leg."""

    def __init__(self, db, rng, venue, slug, anchor):
        self.db, self.rng, self.n = db, rng, 0
        user = User(email=f"{slug}@example.com", password_hash="x", display_name=slug)
        db.add(user)
        db.flush()
        self.acct = VenueAccount(user_id=user.id, venue=venue, venue_env="prod", key_id=slug)
        db.add(self.acct)
        db.flush()
        for asset, atoms, decimals, entry in anchor:
            db.add(
                AnchorBalance(
                    venue_account_id=self.acct.id,
                    asset=asset,
                    atoms=atoms,
                    decimals=decimals,
                    entry_price=entry,
                    as_of=at(DM1, 0),
                )
            )
        self.strategy = Strategy(
            user_id=user.id, venue_account_id=self.acct.id, name=slug, slug=slug
        )
        db.add(self.strategy)
        db.commit()

    def instrument(self, symbol, base, quote, type_, decimals, closes_from=None, first=-2):
        """closes_from: the first close (whole USD), then a seeded walk from day
        D+first on, with days missing."""
        inst = Instrument(
            venue=self.acct.venue,
            symbol=symbol,
            type=type_,
            base_asset=base,
            quote_asset=quote,
            qty_decimals=decimals,
        )
        self.db.add(inst)
        self.db.flush()
        if closes_from is not None:
            whole = closes_from
            for n in range(first, DAYS):
                whole = whole * self.rng.randrange(95, 106) // 100
                if n > first and self.rng.random() < 0.3:
                    continue  # a missing day: the last close is carried forward
                close = f"{whole}.{self.rng.randrange(10**4):04d}"
                self.db.add(
                    PriceDaily(
                        instrument_id=inst.id,
                        day=D + timedelta(days=n),
                        close=close,
                        usd_micros_per_unit=int(parse_price(close) * M),
                        source=inst.venue,
                    )
                )
        return inst

    def leg(self, asset, delta, decimals, kind, when, ref=None):
        self.n += 1
        ref = ref or f"ev{self.n}"
        self.db.add(
            LedgerEntry(
                venue_account_id=self.acct.id,
                asset=asset,
                delta_atoms=delta,
                decimals=decimals,
                kind=kind,
                venue_ref=ref,
                group_ref=f"g:{ref}",
                occurred_at=when,
            )
        )

    def fill(self, inst, action, qty_atoms, price, when, *, stale=False, base_leg=True, fee=None):
        """An AssetFill with its legs; fee = (asset, atoms, decimals)."""
        self.n += 1
        fid = f"f{self.n}"
        lag = timedelta(hours=7) if stale else timedelta(minutes=1)
        self.db.add(
            AssetFill(
                venue_account_id=self.acct.id,
                instrument_id=inst.id,
                venue_fill_id=fid,
                action=action,
                qty_atoms=qty_atoms,
                qty_decimals=inst.qty_decimals,
                price=price,
                filled_at=when,
                ingested_at=when + lag,
            )
        )
        sign = 1 if action == "buy" else -1
        if base_leg:
            d = inst.qty_decimals
            self.leg(inst.base_asset, sign * qty_atoms, d, LegKind.FILL_BASE, when, fid)
        if inst.type != InstrumentType.PERP:
            q_dp = 8 if inst.quote_asset == "BTC" else 6
            cash = int(Fraction(qty_atoms, 10**inst.qty_decimals) * parse_price(price) * 10**q_dp)
            self.leg(inst.quote_asset, -sign * cash, q_dp, LegKind.FILL_QUOTE, when, fid)
        if fee is not None:
            asset, atoms, decimals = fee
            self.leg(asset, -atoms, decimals, LegKind.FEE, when, fid)


def _instant(rng) -> datetime:
    return at(D + timedelta(days=rng.randrange(DAYS)), *rng.choice(TIMES))


def _kraken(db, rng):
    """Spot: ETH/USD, BTC/USD and the BTC-quoted ETH/BTC; ETH fees at 12 dp
    (finer than the pair's 8), an unpriced anchor asset, USD flows, stale fills."""
    book = Book(
        db,
        rng,
        "kraken",
        "ks",
        [
            ("USD", 20_000 * M, 6, None),
            ("ETH", 2 * 10**8, 8, None),
            ("BTC", 10**9, 10, None),  # 0.1 BTC at 10 dp
            ("SOL", 5 * 10**9, 9, None),  # never priced on kraken
        ],
    )
    eth = book.instrument("XETHZUSD", "ETH", "USD", InstrumentType.SPOT, 8, closes_from=3_000)
    btc = book.instrument("XXBTZUSD", "BTC", "USD", InstrumentType.SPOT, 8, closes_from=60_000)
    pair = book.instrument("XETHXXBT", "ETH", "BTC", InstrumentType.SPOT, 8)
    db.commit()

    def spot_fill(when, **kw):
        inst = rng.choice([eth, eth, btc, pair])
        action = rng.choice(["buy", "sell"])
        qty = rng.randrange(1, 5 * 10**7)  # up to 0.5 of a unit at 8 dp
        price = {eth: "3050.25", btc: "60100.5", pair: "0.0507"}[inst]
        fee = None
        if inst.base_asset == "ETH" and rng.random() < 0.3:
            fee = ("ETH", rng.randrange(1, 10**9), 12)  # dust below the pair's 8 dp
        elif rng.random() < 0.3:
            fee = ("USD", rng.randrange(1, 5 * M), 6)
        return lambda: book.fill(inst, action, qty, price, when, fee=fee, **kw)

    def flow(when):
        kind = rng.choice([LegKind.DEPOSIT, LegKind.WITHDRAWAL])
        amount = rng.randrange(1, 2_000) * M * (1 if kind == LegKind.DEPOSIT else -1)
        return lambda: book.leg("USD", amount, 6, kind, when)

    return book, spot_fill, flow


def _hyperliquid(db, rng):
    """Perps BTC-PERP (own closes) and ETH-PERP (own closes only from D+2, so
    until then it is priced at the ETH spot close), a spot ETH pair, USDC fees,
    funding and realized P&L legs, an anchor perp with its entry; flips through
    zero."""
    book = Book(
        db,
        rng,
        "hyperliquid",
        "hl",
        [("USDC", 2_000 * M, 6, None), ("BTC-PERP", 1_000, 5, "60000")],
    )
    btc = book.instrument("BTC", "BTC-PERP", "USDC", InstrumentType.PERP, 5, closes_from=60_000)
    ethp = book.instrument("ETH", "ETH-PERP", "USDC", InstrumentType.PERP, 4, 3_010, first=2)
    eth = book.instrument("@ETH", "ETH", "USDC", InstrumentType.SPOT, 4, closes_from=3_000)
    db.commit()
    position = {btc.id: 1_000, ethp.id: 0}

    def perp_fill(when, **kw):
        if rng.random() < 0.2:
            inst, action, qty = eth, rng.choice(["buy", "sell"]), rng.randrange(1, 10**4)
            return lambda: book.fill(inst, action, qty, "3001.5", when, **kw)
        inst = rng.choice([btc, ethp])
        q = position[inst.id]
        shape = rng.choice(["add", "reduce", "flip"])
        if q == 0 or shape == "add":
            d = rng.randrange(1, 3_000) * (1 if q >= 0 else -1)
        elif shape == "reduce":
            d = -q // 2 or -q
        else:
            d = -2 * q - (1 if q > 0 else -1)  # through zero
        position[inst.id] = q + d  # a simulation in generation order (ties may reorder)
        action = "buy" if d > 0 else "sell"
        price = "60250.5" if inst is btc else "2999.25"
        fee = ("USDC", rng.randrange(1, 3 * M), 6)
        base_leg = rng.random() < 0.7  # sometimes the venue books no perp leg
        return lambda: book.fill(
            inst, action, abs(d), price, when, fee=fee, base_leg=base_leg, **kw
        )

    def flow(when):
        kind = rng.choice([LegKind.FUNDING, LegKind.REALIZED_PNL, LegKind.DEPOSIT])
        amount = rng.randrange(-50, 50) * M or M
        return lambda: book.leg("USDC", amount, 6, kind, when)

    return book, perp_fill, flow


@pytest.mark.parametrize("venue_seed", [("kraken", 1), ("kraken", 2), ("hl", 3), ("hl", 4)])
def test_two_syncs_match_the_per_instant_evaluation(db, quotes, monkeypatch, venue_seed):
    venue, seed = venue_seed
    rng = random.Random(seed)
    book, make_fill, make_flow = (_kraken if venue == "kraken" else _hyperliquid)(db, rng)

    events = [make_fill(_instant(rng), stale=rng.random() < 0.15) for _ in range(60)]
    events += [make_flow(_instant(rng)) for _ in range(25)]
    rng.shuffle(events)
    split = len(events) * 2 // 3

    for insert in events[:split]:
        insert()
    db.commit()
    first = _emit_and_compare(db, book.strategy, monkeypatch)
    assert len(first) > 20

    # Sync two: the rest (earlier, later and same instants as signalled fills),
    # plus one more fill at an instant whose fills were all signalled in sync one:
    # it continues that instant's running position.
    for insert in events[split:]:
        insert()
    shared = max(v["filled_at"] for v in first.values())
    make_fill(shared)()
    if venue == "hl":
        # Capital leaves before the last day's fills: equity is not positive (0, None).
        book.leg("USDC", -50_000 * M, 6, LegKind.WITHDRAWAL, at(D + timedelta(days=DAYS - 1), 0))
    db.commit()
    second = _emit_and_compare(db, book.strategy, monkeypatch)
    assert len(second) > 10

    # The cases the comparison has to cover actually occurred.
    rows = list(first.values()) + list(second.values())
    instants = [r["filled_at"] for r in rows]
    assert any(instants.count(t) > 1 for t in instants)  # same-instant ties
    assert any(r["filled_at"] == shared for r in second.values())
    assert any(r["quote_last"] is None for r in rows) and any(r["quote_last"] for r in rows)
    assert any(r["creator_equity_micros"] > 0 for r in rows)
    if venue == "kraken":
        assert any(r["notional_micros"] is None for r in rows)  # the BTC-quoted pair
    else:
        assert any(r["creator_equity_micros"] == 0 for r in rows)
        assert {r["side"] for r in rows} == {"long", "short"}

    # Idempotent: a third pass adds nothing.
    assert paper_signals.emit_asset_signals(db, book.strategy) == 0
