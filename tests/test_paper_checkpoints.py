"""w3-perf equivalence: the incremental paths equal the full replays to the atom.

- valuation.LedgerFold == holdings_at / perp_positions_at (random ledgers,
  through a to_state/from_state round trip).
- paper_book.book_state / holding_at == load_book, from a checkpoint plus the
  rows after it, at the end of time and at earlier instants, and after every
  way a checkpoint can go wrong (count mismatch, a row committed below its
  ids, a perp fill older than its last one, a new anchor, a corrupt state):
  each falls back to a full replay, and verify_checkpoint /
  drop_bad_checkpoints catch a corrupt state whose counts still match.
- End to end: one seeded random world (perps with hourly funding and a
  liquidation; an equity with a split and a dividend) is driven through
  paper.process_asset_allocation twice, once on the pre-w3-perf code paths
  (load_book everywhere, perp_positions_at per funding row, no checkpoints)
  and once incremental with checkpoints randomly invalidated between passes.
  Every row the engine wrote must be identical.
- Signals: valuation_db.account_inputs(valued_from=...) answers LedgerSweep
  exactly as the full leg list does, and emit_asset_signals writes the same
  rows with and without it.
"""

import logging
import random
from datetime import UTC, datetime, time, timedelta
from fractions import Fraction

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from alphamarket import paper, paper_book, paper_lifecycle, paper_signals, prices, valuation_db
from alphamarket.config import get_settings
from alphamarket.connectors.base import CorporateActionKind, InstrumentType, LegKind
from alphamarket.db import Base
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    AnchorBalance,
    AssetFill,
    AssetQuote,
    CorporateActionRow,
    FundingRateRow,
    Instrument,
    LedgerEntry,
    MirrorAssetFill,
    MirrorLedgerEntry,
    MirrorSkip,
    PaperBookCheckpoint,
    PriceDaily,
    Signal,
    Strategy,
    User,
    VenueAccount,
)
from alphamarket.paper_book import BookState, LegSpec, cash_leg
from alphamarket.prices import Mark
from alphamarket.valuation import (
    AnchorIn,
    LedgerFold,
    LedgerSweep,
    LegIn,
    OutOfOrderFill,
    PerpFillIn,
    holdings_at,
    perp_positions_at,
)

M = 1_000_000
START = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
END = datetime.max.replace(tzinfo=UTC)


def _factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


def _contract_latest_mark(db, instrument_id, now):
    """prices.latest_mark's contract with the clock honoured (no quote from
    after `now`): simulated passes must not see the future."""
    oldest = now - timedelta(seconds=get_settings().quote_max_age_seconds)
    row = db.scalars(
        select(AssetQuote)
        .where(
            AssetQuote.instrument_id == instrument_id,
            AssetQuote.as_of <= now,
            AssetQuote.as_of >= oldest,
        )
        .order_by(AssetQuote.as_of.desc(), AssetQuote.id.desc())
        .limit(1)
    ).first()
    if row is None:
        return None
    return Mark(instrument_id, row.last, aware(row.as_of), row.source, "last")


@pytest.fixture(autouse=True)
def _marks(monkeypatch):
    monkeypatch.setattr(prices, "latest_mark", _contract_latest_mark)


def _state_of(book) -> tuple:
    return (book.cash_micros, book.holdings, book.perps)


# ---- pure: LedgerFold ---------------------------------------------------------------------------


def _random_ledger(rng: random.Random, n_legs: int, n_fills: int):
    anchor = (
        AnchorIn("USD", rng.randrange(10**9), 6),
        AnchorIn("ETH", rng.randrange(10**12), 9),
        AnchorIn("SOL-PERP", rng.choice([0, 5_000, -3_000]), 3, entry_price="150.5"),
    )
    assets = [("USD", [6]), ("ETH", [6, 9, 12]), ("BTC", [5, 8]), ("SOL-PERP", [3, 4])]
    legs = []
    for _ in range(n_legs):
        asset, scales = rng.choice(assets)
        legs.append(
            LegIn(
                asset,
                rng.randrange(-(10**7), 10**7),
                rng.choice(scales),
                rng.choice(list(LegKind)),
                START + timedelta(minutes=rng.randrange(60 * 24 * 30)),
            )
        )
    fills = []
    for _ in range(n_fills):
        asset = rng.choice(["BTC-PERP", "SOL-PERP"])
        fills.append(
            PerpFillIn(
                asset,
                START + timedelta(minutes=rng.randrange(60 * 24 * 30)),
                rng.choice([-1, 1]) * rng.randrange(1, 4_000),
                rng.choice([3, 4, 5]),
                f"{rng.randrange(100, 90_000)}.{rng.randrange(100):02d}",
            )
        )
    fills.sort(key=lambda f: f.occurred_at)
    return anchor, legs, fills


@pytest.mark.parametrize("seed", range(12))
def test_ledger_fold_equals_holdings_at_and_perp_positions_at(seed):
    rng = random.Random(seed)
    anchor, legs, fills = _random_ledger(rng, 300, 80)
    fold = LedgerFold(anchor)
    cut_legs, cut_fills = rng.randrange(len(legs)), rng.randrange(len(fills))
    for leg in legs[:cut_legs]:
        fold.add_leg(leg)
    for fill in fills[:cut_fills]:
        fold.add_fill(fill)
    # Round trip through the JSON-safe state, as a checkpoint does.
    state = fold.to_state()
    assert all(isinstance(v, str) for v in state["held"].values())
    fold = LedgerFold.from_state(anchor, state)
    for leg in legs[cut_legs:]:
        fold.add_leg(leg)
    for fill in fills[cut_fills:]:
        fold.add_fill(fill)
    assert fold.holdings() == holdings_at(anchor, legs, END)
    assert fold.perp_positions() == perp_positions_at(anchor, fills, END)


def test_ledger_fold_refuses_an_older_fill_and_a_foreign_state():
    anchor = (AnchorIn("USD", 1_000 * M, 6),)
    fold = LedgerFold(anchor)
    fold.add_fill(PerpFillIn("BTC-PERP", START + timedelta(hours=2), 100, 5, "60000"))
    fold.add_fill(PerpFillIn("BTC-PERP", START + timedelta(hours=2), 100, 5, "60001"))  # tie ok
    with pytest.raises(OutOfOrderFill):
        fold.add_fill(PerpFillIn("BTC-PERP", START + timedelta(hours=1), 100, 5, "60000"))
    state = fold.to_state()
    with pytest.raises(ValueError, match="different anchor"):
        LedgerFold.from_state((AnchorIn("USD", 999 * M, 6),), state)
    with pytest.raises(ValueError, match="version"):
        LedgerFold.from_state(anchor, {**state, "v": 0})


# ---- paper_book: book_state from a checkpoint ---------------------------------------------------


def _hl_allocation(db, dollars: int = 1_000, tag: str = "") -> tuple[Allocation, Instrument]:
    creator = User(email=f"c{tag}@x.test", password_hash="x", display_name="c")
    db.add(creator)
    db.flush()
    acct = VenueAccount(user_id=creator.id, venue="hyperliquid", venue_env="prod", key_id="k")
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=creator.id, venue_account_id=acct.id, name=tag, slug=f"s{tag}")
    follower = User(email=f"f{tag}@x.test", password_hash="x", display_name="f")
    db.add_all([strategy, follower])
    db.flush()
    inst = db.scalars(select(Instrument).where(Instrument.symbol == "BTC")).first()
    if inst is None:
        inst = Instrument(
            venue="hyperliquid",
            symbol="BTC",
            type=InstrumentType.PERP,
            base_asset="BTC-PERP",
            quote_asset="USDC",
            qty_decimals=5,
        )
        db.add(inst)
        db.flush()
    alloc = Allocation(
        user_id=follower.id,
        strategy_id=strategy.id,
        allocation_micros=dollars * M,
        drawdown_limit_bps=9000,
        high_water_micros=dollars * M,
        started_at=START,
    )
    db.add(alloc)
    db.commit()
    return alloc, inst


def _perp_fill(db, alloc, inst, n: int, qty: int, price: str, at: datetime):
    db.add(
        MirrorAssetFill(
            allocation_id=alloc.id,
            signal_id=None,
            reason="signal",
            mode="paper",
            instrument_id=inst.id,
            action="buy" if qty > 0 else "sell",
            qty_atoms=abs(qty),
            qty_decimals=inst.qty_decimals,
            price=price,
            notional_micros=0,
            fee_micros=0,
            price_basis="ask",
            filled_at=at,
        )
    )
    paper_book.append_group(
        db,
        alloc,
        ref=f"t:{n}",
        legs=[
            LegSpec("BTC-PERP", qty, inst.qty_decimals, LegKind.FILL_BASE, inst.id),
            cash_leg(-(abs(qty) * 3), LegKind.FEE),
        ],
        occurred_at=at,
    )


def _random_events(db, alloc, inst, rng, n: int, start_n: int, t0: datetime) -> datetime:
    t = t0
    for k in range(start_n, start_n + n):
        t += timedelta(minutes=rng.randrange(1, 90))
        roll = rng.random()
        if roll < 0.3:
            _perp_fill(
                db,
                alloc,
                inst,
                k,
                rng.choice([-1, 1]) * rng.randrange(1, 900),
                f"{rng.randrange(50_000, 70_000)}.{rng.randrange(10)}",
                t,
            )
        elif roll < 0.8:
            # funding, sometimes back-dated (a row whose mark arrived late)
            when = t - timedelta(minutes=rng.choice([0, 0, 0, 30, 300]))
            paper_book.append_group(
                db,
                alloc,
                ref=f"fund:{k}",
                occurred_at=when,
                legs=[LegSpec("USD", rng.randrange(-5_000, 5_000), 6, LegKind.FUNDING, inst.id)],
            )
        else:
            paper_book.append_group(
                db,
                alloc,
                ref=f"x:{k}",
                occurred_at=t,
                legs=[
                    LegSpec("ETH", rng.randrange(1, 10**6), rng.choice([6, 9]), LegKind.FILL_BASE)
                ],
            )
    db.commit()
    return t


def _assert_equal_to_replay(db, alloc, rng, horizon: datetime) -> None:
    assert _state_of(paper_book.book_state(db, alloc)) == _state_of(paper_book.load_book(db, alloc))
    for _ in range(3):
        at = START + (horizon - START) * rng.random()
        assert _state_of(paper_book.book_state(db, alloc, at=at)) == _state_of(
            paper_book.load_book(db, alloc, at=at)
        ), at
        held = paper_book.load_book(db, alloc, at=at).holdings
        for asset in ("ETH", "BTC-PERP"):
            assert paper_book.holding_at(db, alloc, asset, at) == held.get(asset)


@pytest.mark.parametrize("seed", range(6))
def test_book_state_from_a_checkpoint_equals_load_book(db, seed):
    rng = random.Random(seed)
    alloc, inst = _hl_allocation(db, tag=str(seed))
    t = _random_events(db, alloc, inst, rng, 40, 0, START)
    assert paper_book.save_checkpoint(db, alloc)
    db.commit()
    assert not paper_book.save_checkpoint(db, alloc)  # current: nothing written
    for step in range(4):
        t = _random_events(db, alloc, inst, rng, 25, 100 * (step + 1), t)
        _assert_equal_to_replay(db, alloc, rng, t)
        if rng.random() < 0.7:
            assert paper_book.save_checkpoint(db, alloc)
            db.commit()
            _assert_equal_to_replay(db, alloc, rng, t)
        assert paper_book.verify_checkpoint(db, alloc) is True


def test_every_broken_checkpoint_falls_back_to_a_full_replay(db, caplog):
    rng = random.Random(7)
    alloc, inst = _hl_allocation(db, tag="broken")
    t = _random_events(db, alloc, inst, rng, 30, 0, START)
    paper_book.save_checkpoint(db, alloc)
    db.commit()
    cp = db.get(PaperBookCheckpoint, alloc.id)

    # 1. A row that committed late BELOW the checkpoint's ids: the checkpoint
    # claims to have folded the newest entry, which it has not.
    t = _random_events(db, alloc, inst, rng, 1, 500, t)
    newest = db.scalar(select(MirrorLedgerEntry.id).order_by(MirrorLedgerEntry.id.desc()).limit(1))
    cp.through_entry_id = newest
    db.commit()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="alphamarket.paper_book"):
        _assert_equal_to_replay(db, alloc, rng, t)
    assert "does not match the ledger" in caplog.text
    assert paper_book.verify_checkpoint(db, alloc) is False
    assert paper_book.save_checkpoint(db, alloc)  # rebuilt from a full replay
    db.commit()
    assert paper_book.verify_checkpoint(db, alloc) is True

    # 2. A perp fill dated before the last one the checkpoint folded.
    last = max(aware(f.filled_at) for f in db.scalars(select(MirrorAssetFill)).all())
    _perp_fill(db, alloc, inst, 600, 123, "61000", last - timedelta(hours=3))
    db.commit()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="alphamarket.paper_book"):
        _assert_equal_to_replay(db, alloc, rng, t)
    assert "predates the book checkpoint" in caplog.text
    paper_book.save_checkpoint(db, alloc)
    db.commit()

    # 3. The anchor changed (another starting cash): the state is not reused.
    alloc.allocation_micros += 5 * M
    db.commit()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="alphamarket.paper_book"):
        _assert_equal_to_replay(db, alloc, rng, t)
    assert "different anchor" in caplog.text
    paper_book.save_checkpoint(db, alloc)
    db.commit()

    # 4. A corrupt state with matching counts: book_state cannot see it, the
    # nightly verification does, and the next pass rebuilds the row.
    cp = db.get(PaperBookCheckpoint, alloc.id)
    state = dict(cp.state)
    state["held"] = {**state["held"], "USD": str(int(state["held"]["USD"]) + 1)}
    cp.state = state
    db.commit()
    assert paper_book.verify_checkpoint(db, alloc) is False
    assert paper_book.drop_bad_checkpoints(db) == 1
    assert db.get(PaperBookCheckpoint, alloc.id) is None
    _assert_equal_to_replay(db, alloc, rng, t)
    paper_book.save_checkpoint(db, alloc)
    db.commit()
    assert paper_book.drop_bad_checkpoints(db, sample=10) == 0

    # 5. An unreadable state.
    cp = db.get(PaperBookCheckpoint, alloc.id)
    cp.state = {"v": 1}
    db.commit()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="alphamarket.paper_book"):
        _assert_equal_to_replay(db, alloc, rng, t)
    assert "unusable" in caplog.text


def test_a_checkpoint_newer_than_the_instant_is_not_used(db, monkeypatch):
    rng = random.Random(3)
    alloc, inst = _hl_allocation(db, tag="newer")
    t = _random_events(db, alloc, inst, rng, 30, 0, START)
    paper_book.save_checkpoint(db, alloc)
    db.commit()
    resumed = []
    real = paper_book._fold_rows
    monkeypatch.setattr(
        paper_book,
        "_fold_rows",
        lambda db_, a, folded, t_, **kw: (
            resumed.append(folded.resumed) or real(db_, a, folded, t_, **kw)
        ),
    )
    paper_book.book_state(db, alloc, at=START + timedelta(minutes=5))
    paper_book.book_state(db, alloc, at=t + timedelta(days=1))
    assert resumed == [False, True]


# ---- end to end: the engine with and without checkpoints ----------------------------------------


class _ReferencePositions:
    """paper_lifecycle's per-row positions as before w3-perf: perp_positions_at
    over every fill, for each funding row."""

    def __init__(self, anchor, fills):
        self.anchor, self.fills = anchor, fills

    def at(self, t):
        return perp_positions_at(self.anchor, self.fills, t)


def _reference_paths(monkeypatch) -> None:
    """The pre-w3-perf code paths: a full load_book replay wherever the engine
    reads the book, and no checkpoint is ever written."""

    def book_state(db, allocation, at=None):
        b = paper_book.load_book(db, allocation, at)
        return BookState(b.cash_micros, b.holdings, b.perps)

    def holding_at(db, allocation, asset, at):
        return paper_book.load_book(db, allocation, at=at).holdings.get(asset)

    monkeypatch.setattr(paper_book, "book_state", book_state)
    monkeypatch.setattr(paper_book, "holding_at", holding_at)
    monkeypatch.setattr(paper_book, "save_checkpoint_safely", lambda db, allocation: False)
    monkeypatch.setattr(paper_lifecycle, "_PositionsAt", _ReferencePositions)


def _original_cost_basis(db, allocation) -> dict[str, int]:
    """paper.cost_basis_micros before w3-perf (every leg read)."""
    entries = db.scalars(
        select(MirrorLedgerEntry)
        .where(MirrorLedgerEntry.allocation_id == allocation.id)
        .order_by(MirrorLedgerEntry.occurred_at, MirrorLedgerEntry.id)
    ).all()
    groups: dict[str, list] = {}
    for e in entries:
        groups.setdefault(e.ref, []).append(e)
    qty: dict[str, Fraction] = {}
    cost: dict[str, Fraction] = {}
    for legs in groups.values():
        paid = -sum(e.delta_atoms for e in legs if e.kind == LegKind.FILL_QUOTE)
        for e in legs:
            if e.asset == "USD" or e.asset.endswith("-PERP"):
                continue
            delta = Fraction(e.delta_atoms, 10**e.decimals)
            held = qty.get(e.asset, Fraction(0))
            if e.kind == LegKind.FILL_BASE and delta > 0:
                cost[e.asset] = cost.get(e.asset, Fraction(0)) + max(paid, 0)
            elif e.kind == LegKind.FILL_BASE and delta < 0 and held > 0:
                cost[e.asset] = cost.get(e.asset, Fraction(0)) * (held + delta) / held
            qty[e.asset] = held + delta
            if qty[e.asset] <= 0:
                cost[e.asset] = Fraction(0)
    return {asset: int(c) for asset, c in cost.items()}


def _original_instruments_by_asset(db, allocation) -> dict:
    rows = db.execute(
        select(MirrorLedgerEntry.asset, Instrument)
        .join(Instrument, Instrument.id == MirrorLedgerEntry.instrument_id)
        .where(MirrorLedgerEntry.allocation_id == allocation.id)
        .order_by(MirrorLedgerEntry.id)
    ).all()
    return {asset: inst.id for asset, inst in rows}


def _world(db, rng: random.Random) -> dict:
    """Two strategies: Hyperliquid BTC perp and Alpaca AAPL, three followers
    each, signals over 4 days with hourly funding and quotes, an AAPL 2:1
    split and a dividend, and a BTC crash that liquidates the leveraged."""
    out: dict = {"allocations": []}
    for venue in ("hyperliquid", "alpaca"):
        creator = User(email=f"c-{venue}@x.test", password_hash="x", display_name="c")
        db.add(creator)
        db.flush()
        acct = VenueAccount(user_id=creator.id, venue=venue, venue_env="prod", key_id="k")
        db.add(acct)
        db.flush()
        strategy = Strategy(user_id=creator.id, venue_account_id=acct.id, name=venue, slug=venue)
        db.add(strategy)
        db.flush()
        out[venue] = (acct, strategy)
        for i, dollars in enumerate((1_000, 250, 37)):
            follower = User(email=f"f{i}-{venue}@x.test", password_hash="x", display_name="f")
            db.add(follower)
            db.flush()
            alloc = Allocation(
                user_id=follower.id,
                strategy_id=strategy.id,
                allocation_micros=dollars * M + rng.randrange(M),
                drawdown_limit_bps=9900,
                high_water_micros=dollars * M,
                started_at=START,
            )
            db.add(alloc)
            out["allocations"].append(alloc)
    btc = Instrument(
        venue="hyperliquid",
        symbol="BTC",
        type=InstrumentType.PERP,
        base_asset="BTC-PERP",
        quote_asset="USDC",
        qty_decimals=5,
        min_notional="10",
        margin_tiers=[{"lower_bound": "0", "max_leverage": 40}],
    )
    aapl = Instrument(
        venue="alpaca",
        symbol="AAPL",
        type=InstrumentType.EQUITY,
        base_asset="AAPL",
        quote_asset="USD",
        qty_decimals=9,
        min_qty_atoms=1,
        min_notional="1",
    )
    db.add_all([btc, aapl])
    db.flush()
    out["btc"], out["aapl"] = btc, aapl
    split_day, div_ex, div_pay = (
        START.date() + timedelta(days=2),
        START.date() + timedelta(days=1),
        START.date() + timedelta(days=3),
    )
    db.add_all(
        [
            CorporateActionRow(
                source="t",
                symbol="AAPL",
                kind=CorporateActionKind.SPLIT,
                ex_date=split_day,
                ratio_new=2,
                ratio_old=1,
            ),
            CorporateActionRow(
                source="t",
                symbol="AAPL",
                kind=CorporateActionKind.CASH_DIVIDEND,
                ex_date=div_ex,
                pay_date=div_pay,
                cash_per_share="0.25",
            ),
        ]
    )
    db.commit()
    return out


def _hour_events(db, world, rng, h: int, state: dict) -> None:
    """Hour h's public data and creator fills (identical in both runs: rng)."""
    t = START + timedelta(hours=h)
    btc, aapl = world["btc"], world["aapl"]
    crash = 60 <= h < 64
    state["btc"] = state["btc"] * (0.78 if h == 60 else 1) * (1 + rng.uniform(-0.004, 0.004))
    state["aapl"] = state["aapl"] * (1 + rng.uniform(-0.003, 0.003))
    if t.date() >= START.date() + timedelta(days=2) and not state.get("split"):
        state["aapl"] /= 2  # traded split-adjusted from the ex-date
        state["split"] = True
    btc_px, aapl_px = f"{state['btc']:.1f}", f"{state['aapl']:.2f}"
    db.add(
        FundingRateRow(
            instrument_id=btc.id,
            at=t,
            rate=f"{rng.uniform(-3e-5, 6e-5):.8f}",
            mark_price=btc_px if rng.random() < 0.8 else None,
            source="t",
        )
    )
    for inst, px in ((btc, btc_px), (aapl, aapl_px)):
        db.add(
            AssetQuote(
                instrument_id=inst.id,
                bid=None,
                ask=None,
                last=px,
                as_of=t + timedelta(minutes=25),
                source="t",
            )
        )
        if t.hour == 0:
            db.add(
                PriceDaily(
                    instrument_id=inst.id,
                    day=t.date(),
                    close=px,
                    source="t",
                    usd_micros_per_unit=int(Fraction(px) * M),
                )
            )
    for venue, inst, px, key in (
        ("hyperliquid", btc, btc_px, "pos_btc"),
        ("alpaca", aapl, aapl_px, "pos_aapl"),
    ):
        if crash or (rng.random() > 0.35 and h not in (1, 58)):
            continue
        acct, strategy = world[venue]
        before = state[key]
        if venue == "hyperliquid":
            qty = rng.randrange(5_000, 40_000)  # 0.05 - 0.4 BTC
            action = "buy" if before <= 0 or rng.random() < 0.6 else "sell"
            if h == 58:  # long ~9x into the crash: the followers get liquidated
                action, qty = "buy", max(150_000 - before, 10_000)
        else:
            qty = rng.randrange(1, 30) * 10**9
            # Hold AAPL through the dividend's ex-date and the split.
            action = "buy" if before <= 0 or h < 60 or rng.random() < 0.6 else "sell"
            qty = min(qty, before) if action == "sell" else qty
        after = before + (qty if action == "buy" else -qty)
        state[key] = after
        state["n"] += 1
        filled = t + timedelta(minutes=rng.randrange(1, 20))
        fill = AssetFill(
            venue_account_id=acct.id,
            instrument_id=inst.id,
            venue_fill_id=f"f{state['n']}",
            action=action,
            qty_atoms=qty,
            qty_decimals=inst.qty_decimals,
            price=px,
            filled_at=filled,
            ingested_at=filled + timedelta(seconds=20),
        )
        db.add(fill)
        db.flush()
        db.add(
            Signal(
                strategy_id=strategy.id,
                asset_fill_id=fill.id,
                instrument_id=inst.id,
                ticker=inst.symbol,
                side="long" if after >= 0 else "short",
                action=action,
                qty_atoms=qty,
                qty_decimals=inst.qty_decimals,
                price=px,
                creator_equity_micros=10_000 * M,
                position_before_atoms=before,
                position_after_atoms=after,
                filled_at=filled,
                observed_at=filled + timedelta(seconds=20),
                quote_bid=px if rng.random() < 0.7 else None,
                quote_ask=px if rng.random() < 0.7 else None,
                quote_at=filled + timedelta(seconds=20),
                quote_source="t",
            )
        )
    db.commit()


def _snapshot(db) -> dict:
    def rows(model, *cols):
        return [
            tuple(getattr(r, c) for c in cols)
            for r in db.scalars(select(model).order_by(model.id)).all()
        ]

    return {
        "legs": rows(
            MirrorLedgerEntry,
            "allocation_id",
            "instrument_id",
            "asset",
            "delta_atoms",
            "decimals",
            "kind",
            "ref",
            "group_ref",
            "occurred_at",
        ),
        "fills": rows(
            MirrorAssetFill,
            "allocation_id",
            "signal_id",
            "reason",
            "instrument_id",
            "action",
            "qty_atoms",
            "qty_decimals",
            "price",
            "notional_micros",
            "fee_micros",
            "price_basis",
            "penalty_bps",
            "filled_at",
        ),
        "skips": rows(MirrorSkip, "allocation_id", "signal_id", "reason"),
        "allocations": rows(Allocation, "status", "stop_reason", "last_signal_id"),
    }


def _drive(seed: int, *, incremental: bool, monkeypatch) -> dict:
    rng, events_rng = random.Random(seed * 7 + 1), random.Random(seed)
    factory = _factory()
    with monkeypatch.context() as patch, factory() as db:
        if not incremental:
            _reference_paths(patch)
        world = _world(db, events_rng)
        state = {"btc": 60_000.0, "aapl": 220.0, "pos_btc": 0, "pos_aapl": 0, "n": 0}
        valuations = []
        for h in range(80):  # dividend ex day 1, split day 2, crash h60, pay day 3
            _hour_events(db, world, events_rng, h, state)
            now = START + timedelta(hours=h, minutes=30)
            for alloc in world["allocations"]:
                paper.process_asset_allocation(db, alloc, now)
                v = paper.value_asset_allocation(db, alloc, now=now)
                valuations.append((v.cash_micros, v.open_value_micros, v.asset_positions))
                if incremental:
                    if rng.random() < 0.15:  # invalidate: a full replay must agree
                        cp = db.get(PaperBookCheckpoint, alloc.id)
                        if cp is not None:
                            cp.entry_count += rng.choice([-1, 1])
                            db.commit()
                    if rng.random() < 0.05:
                        cp = db.get(PaperBookCheckpoint, alloc.id)
                        if cp is not None:
                            db.delete(cp)
                            db.commit()
                    assert _state_of(paper_book.book_state(db, alloc)) == _state_of(
                        paper_book.load_book(db, alloc)
                    )
                    at = START + (now - START) * rng.random()
                    assert _state_of(paper_book.book_state(db, alloc, at=at)) == _state_of(
                        paper_book.load_book(db, alloc, at=at)
                    )
                    assert paper.cost_basis_micros(db, alloc) == _original_cost_basis(db, alloc)
                    assert {
                        a: i.id for a, i in paper._instruments_by_asset(db, alloc).items()
                    } == _original_instruments_by_asset(db, alloc)
        snap = _snapshot(db)
        snap["valuations"] = valuations
        snap["checkpoints"] = len(db.scalars(select(PaperBookCheckpoint)).all())
    return snap


@pytest.mark.parametrize("seed", [1, 2])
def test_the_engine_writes_the_same_rows_with_checkpoints(seed, monkeypatch):
    reference = _drive(seed, incremental=False, monkeypatch=monkeypatch)
    incremental = _drive(seed, incremental=True, monkeypatch=monkeypatch)
    assert reference.pop("checkpoints") == 0
    assert incremental.pop("checkpoints") > 0
    kinds = {row[5] for row in reference["legs"]}
    # The world exercised every lifecycle path this workstream touches.
    assert {"funding", "liquidation", "split", "dividend", "fill_base"} <= kinds, kinds
    assert any(row[2] == "liquidation" for row in reference["fills"])
    for key in reference:
        assert incremental[key] == reference[key], key


# ---- signals: the creator's ledger folded before the first pending day --------------------------


def _creator(db, rng: random.Random, days: int = 20, until: datetime | None = None):
    """A Hyperliquid creator: USDC and ETH (two scales) anchored, hourly cash
    legs of every kind (flows included), ETH legs at 4 and 8 dp, BTC perp and
    ETH spot fills, daily closes. With `until`, only events up to it are
    inserted; _creator_rest inserts the others (same rng stream)."""
    user = User(email="creator@x.test", password_hash="x", display_name="c")
    db.add(user)
    db.flush()
    acct = VenueAccount(user_id=user.id, venue="hyperliquid", venue_env="prod", key_id="k")
    db.add(acct)
    db.flush()
    strategy = Strategy(user_id=user.id, venue_account_id=acct.id, name="c", slug="c")
    btc = Instrument(
        venue="hyperliquid",
        symbol="BTC",
        type=InstrumentType.PERP,
        base_asset="BTC-PERP",
        quote_asset="USDC",
        qty_decimals=5,
    )
    eth = Instrument(
        venue="hyperliquid",
        symbol="ETH/USDC",
        type=InstrumentType.SPOT,
        base_asset="ETH",
        quote_asset="USDC",
        qty_decimals=4,
    )
    db.add_all([strategy, btc, eth])
    db.flush()
    anchor_at = START - timedelta(hours=1)
    db.add_all(
        [
            AnchorBalance(
                venue_account_id=acct.id,
                asset="USDC",
                atoms=50_000 * M,
                decimals=6,
                as_of=anchor_at,
            ),
            AnchorBalance(
                venue_account_id=acct.id, asset="ETH", atoms=2 * 10**8, decimals=8, as_of=anchor_at
            ),
        ]
    )
    for day in range(-1, days + 1):
        d = START.date() + timedelta(days=day)
        db.add(
            PriceDaily(
                instrument_id=btc.id,
                day=d,
                close=f"{60_000 + 37 * day}",
                usd_micros_per_unit=(60_000 + 37 * day) * M,
                source="t",
            )
        )
        db.add(
            PriceDaily(
                instrument_id=eth.id,
                day=d,
                close=f"{3_000 + day}.5",
                usd_micros_per_unit=(3_000 + day) * M + M // 2,
                source="t",
            )
        )
    events = []
    for n, hour in enumerate(range(days * 24), start=1):
        t = START + timedelta(hours=hour, minutes=rng.randrange(60))
        kind = rng.choice(
            [
                LegKind.FUNDING,
                LegKind.FUNDING,
                LegKind.DEPOSIT,
                LegKind.FEE,
                LegKind.WITHDRAWAL,
                LegKind.INTEREST,
            ]
        )
        events.append(
            (
                t,
                LedgerEntry(
                    venue_account_id=acct.id,
                    asset="USDC",
                    delta_atoms=rng.randrange(-30 * M, 30 * M),
                    decimals=6,
                    kind=kind,
                    venue_ref=f"e{n}",
                    group_ref=f"e{n}",
                    occurred_at=t,
                ),
            )
        )
        if rng.random() < 0.2:
            events.append(
                (
                    t,
                    LedgerEntry(
                        venue_account_id=acct.id,
                        asset="ETH",
                        delta_atoms=rng.randrange(-(10**4), 10**4),
                        decimals=rng.choice([4, 8]),
                        kind=rng.choice(list(LegKind)),
                        venue_ref=f"e{n}",
                        group_ref=f"e{n}",
                        occurred_at=t,
                    ),
                )
            )
        if rng.random() < 0.15:
            inst = rng.choice([btc, eth])
            qty = rng.randrange(1, 5_000)
            action = rng.choice(["buy", "sell"])
            events.append(
                (
                    t,
                    AssetFill(
                        venue_account_id=acct.id,
                        instrument_id=inst.id,
                        venue_fill_id=f"x{n}",
                        action=action,
                        qty_atoms=qty,
                        qty_decimals=inst.qty_decimals,
                        price="60000.5",
                        filled_at=t,
                        ingested_at=t + timedelta(days=1),
                    ),
                )
            )
            events.append(
                (
                    t,
                    LedgerEntry(
                        venue_account_id=acct.id,
                        instrument_id=inst.id,
                        asset=inst.base_asset,
                        delta_atoms=qty if action == "buy" else -qty,
                        decimals=inst.qty_decimals,
                        kind=LegKind.FILL_BASE,
                        venue_ref=f"x{n}",
                        group_ref=f"x{n}",
                        occurred_at=t,
                    ),
                )
            )
    now = [row for t, row in events if until is None or t <= until]
    db.add_all(now)
    db.commit()
    rest = [row for t, row in events if until is not None and t > until]
    return acct, strategy, rest


@pytest.mark.parametrize("seed", range(4))
def test_account_inputs_valued_from_answers_the_sweep_exactly(db, seed):
    rng = random.Random(seed)
    acct, _, _ = _creator(db, rng)
    fills = db.scalars(select(AssetFill).order_by(AssetFill.filled_at, AssetFill.id)).all()
    first = aware(fills[len(fills) // 2].filled_at)
    # Pin the cut: a deposit at exactly 00:00 UTC of the first pending day is
    # kept as its own leg (a flow on that day), one a microsecond earlier is
    # folded; counting either twice or dropping either changes equity_before.
    midnight = datetime.combine(first.date(), time(0), UTC)
    for ref, at in (("cut-at", midnight), ("cut-before", midnight - timedelta(microseconds=1))):
        db.add(
            LedgerEntry(
                venue_account_id=acct.id,
                asset="USDC",
                delta_atoms=(7 if ref == "cut-at" else 11) * M,
                decimals=6,
                kind=LegKind.DEPOSIT,
                venue_ref=ref,
                group_ref=ref,
                occurred_at=at,
            )
        )
    db.commit()
    full = valuation_db.account_inputs(db, acct)
    folded = valuation_db.account_inputs(db, acct, valued_from=first)
    assert len(folded.legs) < len(full.legs) - 100
    assert folded.perp_fills == full.perp_fills and folded.assets == full.assets
    venues = valuation_db.pricing_venues(acct)
    prices_in = valuation_db.price_inputs(db, full.assets | {"BTC"}, None, None, venues)
    a = LedgerSweep(full.anchor, full.legs, prices_in, full.perp_fills)
    b = LedgerSweep(folded.anchor, folded.legs, prices_in, folded.perp_fills)
    instants = {aware(f.filled_at) for f in fills if aware(f.filled_at) >= first}
    instants |= {first, first + timedelta(days=2, hours=5), midnight, midnight + timedelta(hours=1)}
    for t in sorted(instants):
        assert a.holdings_before(t) == b.holdings_before(t), t
        assert a.perp_positions_before(t) == b.perp_positions_before(t), t
        assert a.equity_before(t) == b.equity_before(t), t


def test_emit_asset_signals_writes_the_same_rows_with_the_folded_ledger(monkeypatch):
    """Two passes (the second sees fills from day 6 on, so its ledger before
    that day is folded): the same signals as with every leg read."""

    def run(fold: bool) -> list[tuple]:
        factory = _factory()
        with monkeypatch.context() as patch, factory() as db:
            patch.setattr(prices, "capture_quote", lambda *a, **k: None)
            if not fold:
                real = valuation_db.account_inputs
                patch.setattr(
                    valuation_db,
                    "account_inputs",
                    lambda db_, account, valued_from=None: real(db_, account),
                )
            until = START + timedelta(days=6, hours=7)
            _, strategy, rest = _creator(db, random.Random(11), days=12, until=until)
            first = paper_signals.emit_asset_signals(db, strategy)
            db.add_all(rest)
            db.commit()
            second = paper_signals.emit_asset_signals(db, strategy)
            assert first > 0 and second > 0
            assert paper_signals.emit_asset_signals(db, strategy) == 0
            return [
                (
                    s.asset_fill_id,
                    s.position_before_atoms,
                    s.position_after_atoms,
                    s.creator_equity_micros,
                    s.equity_basis_day,
                    s.side,
                    s.notional_micros,
                )
                for s in db.scalars(select(Signal).order_by(Signal.id)).all()
            ]

    assert run(fold=True) == run(fold=False)
