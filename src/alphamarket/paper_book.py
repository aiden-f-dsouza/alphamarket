"""The paper follower book for ASSET strategies (wave2, implementation_plan P6).

A follower's allocation is a small ledger of its own: mirror_ledger_entries
(legs, append-only) plus mirror_asset_fills (the readable trade rows). It is
shaped like the creator's ledger v2 so valuation.py values both with the same
code. Binary allocations keep the M6 tables (mirror_fills / settlements).

Starting cash is an implicit anchor, not a row: USD at 6 decimals, so cash
atoms ARE micros. Every cash leg is USD at 6 decimals.

Leg conventions (one group per event; `ref` is the idempotency key, unique
per (allocation, ref, kind, asset)):

| event | ref | legs |
|---|---|---|
| spot/equity buy | sig:<signal_id> | FILL_BASE +q <base>; FILL_QUOTE -cost USD; FEE -fee USD |
| spot/equity sell | sig:<signal_id> | FILL_BASE -q <base>; FILL_QUOTE +proceeds USD; FEE -fee USD |
| perp fill | sig:<signal_id> | FILL_BASE +/-q <COIN>-PERP; REALIZED_PNL +/- USD (reducing |
|  |  | part only); FEE -fee USD |
| perp funding | fund:<instrument_id>:<epoch> | FUNDING +/- USD |
| liquidation | liq:<instrument_id>:<epoch> | FILL_BASE -/+q <COIN>-PERP; LIQUIDATION +/- USD |
|  |  | (P&L realized at the mark) |
| stop close | stop:<instrument_id>:<allocation_id> | as the spot/equity sell or perp fill |
|  |  | above, of the whole position, at the bid (ask to cover a short) |
| split | ca:<corporate_action_id> | SPLIT +/-delta <base> |
| cash dividend | ca:<corporate_action_id> | DIVIDEND +USD |

Every perp position change (signal fill, liquidation or stop close) also has a
mirror_asset_fills row: load_book derives the average-cost perp book from
those rows, and valuation flags PERP_QTY_MISMATCH if they ever disagree
with the FILL_BASE legs.

One mirror_asset_fills row per signal and one leg per (kind, asset) per
group (unique keys): a perp flip through zero is a single FILL_BASE leg of
the net delta, REALIZED_PNL for the closed part only.

append_group returns [] for an already-applied ref AND for a group whose
legs all round to zero (nothing is written, so the ref stays unapplied and
is retried next pass): callers count an event only when rows come back.

Rounding (always against the follower, methodology "Paper copying v2"):
cost and debits round UP to the micro, proceeds and credits round DOWN
(cost_micros_up / proceeds_micros_down below). Fees follow the profile.

Checkpoints (w3-perf). load_book replays the whole follower ledger; with
hourly perp funding that grows by ~24 legs a day per allocation, and the
mirror pass needs the book several times per allocation every ~30 s. The hot
paths therefore call book_state, which starts from the allocation's
paper_book_checkpoints row (the end state folded through stored entry and
fill ids, valuation.LedgerFold) and folds only the rows after it. The
checkpoint is a cache, never the source of truth:

- it is used only when entry_count / fill_count equal a fresh count of the
  allocation's rows at or below its through ids (the ledger is append-only,
  so equal counts mean the very rows it folded: a row that committed after
  the checkpoint was computed but below its ids is caught here), when its
  state parses over the allocation's current anchor, and, for a book at an
  instant `at`, when everything it folded is at or before `at`;
- otherwise, or when a newer perp fill predates its last one (fills are
  order-dependent), book_state replays in full and logs why;
- only save_checkpoint writes it, at the end of paper.process_asset_allocation
  (and of paper_lifecycle.close_now, the inline stop close), in the same
  transaction as the legs it folded. The worker runs that under the
  allocation's FOR UPDATE SKIP LOCKED row lock, and close_now under a
  waiting FOR UPDATE on the same row, so one pass per allocation writes at
  a time; a writer without the lock (the "run now"
  API) can at worst lose the insert race (caught, the pass still commits)
  or store a checkpoint whose counts no longer match, which the check above
  rejects and the next pass rebuilds;
- verify_checkpoint replays the rows at or below its ids and compares, and
  housekeeping drops any checkpoint that disagrees.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, InterfaceError, OperationalError
from sqlalchemy.orm import Session

from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.jobs import aware
from alphamarket.models import (
    Allocation,
    Instrument,
    MirrorAssetFill,
    MirrorLedgerEntry,
    PaperBookCheckpoint,
    utcnow,
)
from alphamarket.valuation import (
    AnchorIn,
    LedgerFold,
    LegIn,
    OutOfOrderFill,
    PerpFillIn,
    PerpPosition,
    Qty,
    holdings_at,
    parse_price,
    perp_positions_at,
)

logger = logging.getLogger(__name__)

CASH_ASSET = "USD"
CASH_DECIMALS = 6  # cash atoms are micros
_END_OF_TIME = datetime.max.replace(tzinfo=UTC)


def cost_micros_up(qty_atoms: int, decimals: int, price: str) -> int:
    """qty x price in USD micros, rounded UP (what a buyer pays)."""
    exact = Fraction(qty_atoms, 10**decimals) * parse_price(price) * 1_000_000
    return -((-exact.numerator) // exact.denominator)


def proceeds_micros_down(qty_atoms: int, decimals: int, price: str) -> int:
    """qty x price in USD micros, rounded DOWN (what a seller receives)."""
    exact = Fraction(qty_atoms, 10**decimals) * parse_price(price) * 1_000_000
    return exact.numerator // exact.denominator


@dataclass(frozen=True)
class LegSpec:
    """One leg to append (see the table in the module docstring)."""

    asset: str
    delta_atoms: int
    decimals: int
    kind: LegKind
    instrument_id: int | None = None


@dataclass(frozen=True)
class FollowerBook:
    """An allocation's state at a moment, plus the inputs that produced it so
    callers can run any valuation.py function over them."""

    cash_micros: int
    holdings: dict[str, Qty]  # every non-cash asset with a non-zero holding (perps too)
    perps: dict[str, PerpPosition]  # open perp positions, average-cost entry
    anchor: tuple[AnchorIn, ...]
    legs: tuple[LegIn, ...]
    perp_fills: tuple[PerpFillIn, ...]
    refs: frozenset[str] = field(default_factory=frozenset)  # applied group refs


def anchor_for(allocation: Allocation) -> tuple[AnchorIn, ...]:
    return (AnchorIn(CASH_ASSET, allocation.allocation_micros, CASH_DECIMALS),)


def load_book(db: Session, allocation: Allocation, at: datetime | None = None) -> FollowerBook:
    """The book at `at` (inclusive; None = everything recorded), replayed in
    full from the ledger, with the legs and perp fills that produced it. The
    reference implementation: hot paths that need only the state call
    book_state, which equals it (tests/test_paper_checkpoints.py)."""
    t = aware(at) if at is not None else _END_OF_TIME
    entries = db.scalars(
        select(MirrorLedgerEntry)
        .where(MirrorLedgerEntry.allocation_id == allocation.id)
        .order_by(MirrorLedgerEntry.occurred_at, MirrorLedgerEntry.id)
    ).all()
    legs = tuple(
        LegIn(e.asset, e.delta_atoms, e.decimals, LegKind(e.kind), aware(e.occurred_at))
        for e in entries
        if aware(e.occurred_at) <= t
    )
    for leg in legs:
        if leg.asset == CASH_ASSET and leg.decimals != CASH_DECIMALS:
            raise ValueError(f"allocation {allocation.id}: USD leg at {leg.decimals} dp")
    perp_fills = tuple(f for f in load_perp_fills(db, allocation) if f.occurred_at <= t)
    anchor = anchor_for(allocation)
    held = holdings_at(anchor, legs, t)
    cash = held.pop(CASH_ASSET, Qty(0, CASH_DECIMALS))
    return FollowerBook(
        cash_micros=cash.atoms,
        holdings=held,
        perps=perp_positions_at(anchor, perp_fills, t),
        anchor=anchor,
        legs=legs,
        perp_fills=perp_fills,
        refs=frozenset(e.ref for e in entries if aware(e.occurred_at) <= t),
    )


def _perp_fill_rows(allocation_id: int):
    return (
        select(
            MirrorAssetFill.id,
            MirrorAssetFill.action,
            MirrorAssetFill.qty_atoms,
            MirrorAssetFill.qty_decimals,
            MirrorAssetFill.price,
            MirrorAssetFill.filled_at,
            Instrument.base_asset,
        )
        .join(Instrument, Instrument.id == MirrorAssetFill.instrument_id)
        .where(
            MirrorAssetFill.allocation_id == allocation_id,
            Instrument.type == InstrumentType.PERP,
        )
    )


def _perp_fill_in(row) -> PerpFillIn:
    return PerpFillIn(
        asset=row.base_asset,
        occurred_at=aware(row.filled_at),
        qty_delta_atoms=row.qty_atoms if row.action == "buy" else -row.qty_atoms,
        decimals=row.qty_decimals,
        price=row.price,
    )


def load_perp_fills(db: Session, allocation: Allocation) -> tuple[PerpFillIn, ...]:
    """Every perp position change of the allocation (mirror_asset_fills on perp
    instruments, signal fills and liquidations), in (filled_at, id) order."""
    rows = db.execute(
        _perp_fill_rows(allocation.id).order_by(MirrorAssetFill.filled_at, MirrorAssetFill.id)
    ).all()
    return tuple(_perp_fill_in(row) for row in rows)


def booked_through(db: Session, allocation: Allocation) -> datetime | None:
    """The latest occurred_at in the allocation's book (None while empty).
    A new paper fill is never dated before it (paper.py: late signals)."""
    return aware(
        db.scalar(
            select(func.max(MirrorLedgerEntry.occurred_at)).where(
                MirrorLedgerEntry.allocation_id == allocation.id
            )
        )
    )


def has_group(db: Session, allocation: Allocation, ref: str) -> bool:
    return (
        db.scalar(
            select(MirrorLedgerEntry.id).where(
                MirrorLedgerEntry.allocation_id == allocation.id, MirrorLedgerEntry.ref == ref
            )
        )
        is not None
    )


def append_group(
    db: Session,
    allocation: Allocation,
    *,
    ref: str,
    legs: Sequence[LegSpec],
    occurred_at: datetime,
    group_ref: str | None = None,
) -> list[MirrorLedgerEntry]:
    """Append one event's legs. Idempotent on `ref`: a group already applied
    returns [] and writes nothing. Zero legs are skipped. Flushes, does not
    commit (the caller commits with its mirror_asset_fills row)."""
    if has_group(db, allocation, ref):
        return []
    rows = [
        MirrorLedgerEntry(
            allocation_id=allocation.id,
            instrument_id=leg.instrument_id,
            asset=leg.asset,
            delta_atoms=leg.delta_atoms,
            decimals=leg.decimals,
            kind=LegKind(leg.kind),
            ref=ref,
            group_ref=group_ref or ref,
            occurred_at=occurred_at,
        )
        for leg in legs
        if leg.delta_atoms != 0
    ]
    for leg in rows:
        if leg.asset == CASH_ASSET and leg.decimals != CASH_DECIMALS:
            raise ValueError("cash legs are USD at 6 decimals")
    db.add_all(rows)
    db.flush()
    return rows


def cash_leg(delta_micros: int, kind: LegKind) -> LegSpec:
    return LegSpec(CASH_ASSET, delta_micros, CASH_DECIMALS, kind)


# ---- checkpoints (see the module docstring) ------------------------------------------


@dataclass(frozen=True)
class BookState:
    """The allocation's book at a moment: load_book's cash_micros, holdings and
    perps, without the legs and fills that produced them."""

    cash_micros: int
    holdings: dict[str, Qty]  # every non-cash asset with a non-zero holding (perps too)
    perps: dict[str, PerpPosition]  # open perp positions, average-cost entry


@dataclass
class _Folded:
    fold: LedgerFold
    resumed: bool  # started from a usable checkpoint
    new_rows: int  # rows folded on top of it
    through_entry_id: int = 0
    entry_count: int = 0
    entries_through_at: datetime | None = None
    through_fill_id: int = 0
    fill_count: int = 0
    fills_through_at: datetime | None = None


def _checkpoint(db: Session, allocation: Allocation) -> PaperBookCheckpoint | None:
    return db.scalars(
        select(PaperBookCheckpoint)
        .where(PaperBookCheckpoint.allocation_id == allocation.id)
        .execution_options(populate_existing=True)
    ).first()


def _checkpoint_with_counts(
    db: Session, allocation: Allocation
) -> tuple[PaperBookCheckpoint, int, int] | None:
    """The checkpoint and, in the same statement, a fresh count of the
    allocation's entries at or below its through_entry_id and of its perp
    fills at or below its through_fill_id."""
    cp = PaperBookCheckpoint
    entries = (
        select(func.count(MirrorLedgerEntry.id))
        .where(
            MirrorLedgerEntry.allocation_id == cp.allocation_id,
            MirrorLedgerEntry.id <= cp.through_entry_id,
        )
        .correlate(cp)
        .scalar_subquery()
    )
    fills = (
        select(func.count(MirrorAssetFill.id))
        .join(Instrument, Instrument.id == MirrorAssetFill.instrument_id)
        .where(
            MirrorAssetFill.allocation_id == cp.allocation_id,
            Instrument.type == InstrumentType.PERP,
            MirrorAssetFill.id <= cp.through_fill_id,
        )
        .correlate(cp)
        .scalar_subquery()
    )
    row = db.execute(
        select(cp, entries, fills)
        .where(cp.allocation_id == allocation.id)
        .execution_options(populate_existing=True)
    ).first()
    if row is None:
        return None
    return row[0], int(row[1] or 0), int(row[2] or 0)


def _resume(
    db: Session, allocation: Allocation, anchor: tuple[AnchorIn, ...], t: datetime
) -> _Folded | None:
    """The allocation's checkpoint as a fold, when it may be used for the book
    at `t`; None (logged unless it is merely newer than `t`) otherwise."""
    found = _checkpoint_with_counts(db, allocation)
    if found is None:
        return None
    cp, entries, fills = found
    for through in (cp.entries_through_at, cp.fills_through_at):
        if through is not None and aware(through) > t:
            return None  # folded events after `t`: valid, but not for this instant
    try:
        fold = LedgerFold.from_state(anchor, cp.state)
    except (ArithmeticError, KeyError, TypeError, ValueError, AttributeError) as exc:
        logger.warning(
            "allocation %s: book checkpoint unusable (%s); full replay", allocation.id, exc
        )
        return None
    if (entries, fills) != (cp.entry_count, cp.fill_count):
        # INFO: the "run now" path has no row lock, so a row committing below
        # the checkpoint's ids is a known, self-healing race (rebuilt next pass).
        logger.info(
            "allocation %s: book checkpoint does not match the ledger (entries %d vs %d, "
            "perp fills %d vs %d at or below its ids); full replay",
            allocation.id,
            entries,
            cp.entry_count,
            fills,
            cp.fill_count,
        )
        return None
    return _Folded(
        fold,
        resumed=True,
        new_rows=0,
        through_entry_id=cp.through_entry_id,
        entry_count=cp.entry_count,
        entries_through_at=aware(cp.entries_through_at),
        through_fill_id=cp.through_fill_id,
        fill_count=cp.fill_count,
        fills_through_at=aware(cp.fills_through_at),
    )


def _fold_rows(
    db: Session,
    allocation: Allocation,
    folded: _Folded,
    t: datetime,
    *,
    entry_ids_upto: int | None = None,
    fill_ids_upto: int | None = None,
) -> _Folded:
    """Fold the entries and perp fills after folded's through ids (and at or
    below *_ids_upto, when given) whose time is at or before `t`. The through
    ids, counts and times track every row read, folded or not, so they
    describe the fold only when `t` is the end of time (save_checkpoint)."""
    query = select(
        MirrorLedgerEntry.id,
        MirrorLedgerEntry.asset,
        MirrorLedgerEntry.delta_atoms,
        MirrorLedgerEntry.decimals,
        MirrorLedgerEntry.kind,
        MirrorLedgerEntry.occurred_at,
    ).where(
        MirrorLedgerEntry.allocation_id == allocation.id,
        MirrorLedgerEntry.id > folded.through_entry_id,
    )
    if entry_ids_upto is not None:
        query = query.where(MirrorLedgerEntry.id <= entry_ids_upto)
    for row in db.execute(query.order_by(MirrorLedgerEntry.id)).all():
        occurred = aware(row.occurred_at)
        folded.through_entry_id = max(folded.through_entry_id, row.id)
        folded.entry_count += 1
        folded.new_rows += 1
        if folded.entries_through_at is None or occurred > folded.entries_through_at:
            folded.entries_through_at = occurred
        if occurred > t:
            continue
        if row.asset == CASH_ASSET and row.decimals != CASH_DECIMALS:
            raise ValueError(f"allocation {allocation.id}: USD leg at {row.decimals} dp")
        leg = LegIn(row.asset, row.delta_atoms, row.decimals, LegKind(row.kind), occurred)
        folded.fold.add_leg(leg)
    fills = _perp_fill_rows(allocation.id).where(MirrorAssetFill.id > folded.through_fill_id)
    if fill_ids_upto is not None:
        fills = fills.where(MirrorAssetFill.id <= fill_ids_upto)
    for row in db.execute(fills.order_by(MirrorAssetFill.filled_at, MirrorAssetFill.id)).all():
        fill = _perp_fill_in(row)
        folded.through_fill_id = max(folded.through_fill_id, row.id)
        folded.fill_count += 1
        folded.new_rows += 1
        if folded.fills_through_at is None or fill.occurred_at > folded.fills_through_at:
            folded.fills_through_at = fill.occurred_at
        if fill.occurred_at > t:
            continue
        folded.fold.add_fill(fill)  # OutOfOrderFill: the caller replays in full
    return folded


def _fold(db: Session, allocation: Allocation, t: datetime) -> _Folded:
    anchor = anchor_for(allocation)
    folded = _resume(db, allocation, anchor, t)
    if folded is not None:
        try:
            return _fold_rows(db, allocation, folded, t)
        except OutOfOrderFill as exc:
            logger.warning(
                "allocation %s: a perp fill predates the book checkpoint (%s); full replay",
                allocation.id,
                exc,
            )
    return _fold_rows(db, allocation, _Folded(LedgerFold(anchor), resumed=False, new_rows=0), t)


def _state(fold: LedgerFold) -> BookState:
    held = fold.holdings()
    cash = held.pop(CASH_ASSET, Qty(0, CASH_DECIMALS))
    return BookState(cash_micros=cash.atoms, holdings=held, perps=fold.perp_positions())


def book_state(db: Session, allocation: Allocation, at: datetime | None = None) -> BookState:
    """load_book(db, allocation, at)'s cash_micros, holdings and perps, equal to
    the atom (scales included), from the checkpoint plus the rows after it
    when the checkpoint is usable, else from a full replay. Read-only."""
    t = aware(at) if at is not None else _END_OF_TIME
    return _state(_fold(db, allocation, t).fold)


def holding_at(db: Session, allocation: Allocation, asset: str, at: datetime) -> Qty | None:
    """load_book(db, allocation, at).holdings.get(asset), from that asset's legs
    only: anchor (cash only) plus every leg of `asset` at or before `at`, at
    the max decimals of those legs; None when it is zero. For a corporate
    action's record instant, which is usually older than the checkpoint."""
    t = aware(at)
    rows = db.execute(
        select(
            MirrorLedgerEntry.delta_atoms, MirrorLedgerEntry.decimals, MirrorLedgerEntry.occurred_at
        ).where(MirrorLedgerEntry.allocation_id == allocation.id, MirrorLedgerEntry.asset == asset)
    ).all()
    anchor = [a for a in anchor_for(allocation) if a.asset == asset]
    legs = [
        LegIn(asset, delta, decimals, LegKind.FILL_BASE, aware(occurred))
        for delta, decimals, occurred in rows
        if aware(occurred) <= t
    ]
    return holdings_at(anchor, legs, t).get(asset)


def save_checkpoint(db: Session, allocation: Allocation) -> bool:
    """Bring the allocation's checkpoint up to every row this transaction sees.
    Flushes, never commits (the caller commits it with the legs it folded).
    Writes nothing when the checkpoint is current or the book is empty.
    Returns whether a row was written. Losing an insert race to another pass
    is not an error: that pass wrote a checkpoint of its own."""
    folded = _fold(db, allocation, _END_OF_TIME)
    if folded.resumed and folded.new_rows == 0:
        return False
    if folded.entry_count == 0 and folded.fill_count == 0:
        return False
    # Flush the pass's own pending rows first, outside the savepoint: an error
    # in them must fail the pass, never be taken for a lost checkpoint race
    # (the savepoint rollback would silently drop them).
    db.flush()
    values = {
        "through_entry_id": folded.through_entry_id,
        "entry_count": folded.entry_count,
        "entries_through_at": folded.entries_through_at,
        "through_fill_id": folded.through_fill_id,
        "fill_count": folded.fill_count,
        "fills_through_at": folded.fills_through_at,
        "state": folded.fold.to_state(),
        "updated_at": utcnow(),
    }
    try:
        with db.begin_nested():
            cp = db.get(PaperBookCheckpoint, allocation.id)  # refreshed by _fold above
            if cp is None:
                db.add(PaperBookCheckpoint(allocation_id=allocation.id, **values))
            else:
                for name, value in values.items():
                    setattr(cp, name, value)
            db.flush()
    except IntegrityError:
        logger.info("allocation %s: book checkpoint written by another pass", allocation.id)
        return False
    return True


def save_checkpoint_safely(db: Session, allocation: Allocation) -> bool:
    """save_checkpoint for the mirror pass: a cache must never fail the pass it
    rides on, so anything but a lost connection is logged and skipped."""
    try:
        return save_checkpoint(db, allocation)
    except (OperationalError, InterfaceError):
        raise
    except Exception:
        logger.exception("allocation %s: book checkpoint not saved", allocation.id)
        return False


def verify_checkpoint(db: Session, allocation: Allocation) -> bool | None:
    """Replay the rows at or below the checkpoint's ids from scratch and compare
    the state. None when there is no checkpoint; False when it disagrees."""
    cp = _checkpoint(db, allocation)
    if cp is None:
        return None
    fresh = _fold_rows(
        db,
        allocation,
        _Folded(LedgerFold(anchor_for(allocation)), resumed=False, new_rows=0),
        _END_OF_TIME,
        entry_ids_upto=cp.through_entry_id,
        fill_ids_upto=cp.through_fill_id,
    )
    return (
        fresh.fold.to_state() == cp.state
        and fresh.entry_count == cp.entry_count
        and fresh.fill_count == cp.fill_count
        and fresh.entries_through_at == aware(cp.entries_through_at)
        and fresh.fills_through_at == aware(cp.fills_through_at)
    )


def drop_bad_checkpoints(db: Session, sample: int | None = None) -> int:
    """Delete every checkpoint verify_checkpoint rejects (logged); the next
    mirror pass rebuilds it from the ledger. `sample`: check only that many,
    chosen at random (each check is a full replay). Commits. Returns rows
    deleted."""
    query = select(PaperBookCheckpoint.allocation_id)
    if sample is not None:
        query = query.order_by(func.random()).limit(sample)
    dropped = 0
    for allocation_id in db.scalars(query).all():
        allocation = db.get(Allocation, allocation_id)
        try:
            ok = allocation is not None and verify_checkpoint(db, allocation) is not False
        except (OperationalError, InterfaceError):
            raise
        except Exception:
            logger.exception("allocation %s: book checkpoint unverifiable", allocation_id)
            ok = False
        if ok:
            continue
        logger.error(
            "allocation %s: book checkpoint disagrees with the ledger; dropped", allocation_id
        )
        cp = db.get(PaperBookCheckpoint, allocation_id)
        if cp is not None:
            db.delete(cp)
        dropped += 1
    db.commit()
    return dropped
