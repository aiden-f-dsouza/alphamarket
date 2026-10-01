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
| split | ca:<corporate_action_id> | SPLIT +/-delta <base> |
| cash dividend | ca:<corporate_action_id> | DIVIDEND +USD |

Every perp position change (signal fill or liquidation) also has a
mirror_asset_fills row: load_book derives the average-cost perp book from
those rows, and valuation flags PERP_QTY_MISMATCH if they ever disagree
with the FILL_BASE legs.

Rounding (always against the follower, methodology "Paper copying v2"):
cost and debits round UP to the micro, proceeds and credits round DOWN
(cost_micros_up / proceeds_micros_down below). Fees follow the profile.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import InstrumentType, LegKind
from alphamarket.jobs import aware
from alphamarket.models import Allocation, Instrument, MirrorAssetFill, MirrorLedgerEntry
from alphamarket.valuation import (
    AnchorIn,
    LegIn,
    PerpFillIn,
    PerpPosition,
    Qty,
    holdings_at,
    parse_price,
    perp_positions_at,
)

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
    """The book at `at` (inclusive; None = everything recorded)."""
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
    fills = db.execute(
        select(MirrorAssetFill, Instrument)
        .join(Instrument, Instrument.id == MirrorAssetFill.instrument_id)
        .where(
            MirrorAssetFill.allocation_id == allocation.id,
            Instrument.type == InstrumentType.PERP,
        )
        .order_by(MirrorAssetFill.filled_at, MirrorAssetFill.id)
    ).all()
    perp_fills = tuple(
        PerpFillIn(
            asset=inst.base_asset,
            occurred_at=aware(fill.filled_at),
            qty_delta_atoms=fill.qty_atoms if fill.action == "buy" else -fill.qty_atoms,
            decimals=fill.qty_decimals,
            price=fill.price,
        )
        for fill, inst in fills
        if aware(fill.filled_at) <= t
    )
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
