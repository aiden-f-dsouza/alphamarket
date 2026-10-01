"""Follower portfolio valuation (M6): what a paper allocation is worth now,
its history, and the drawdown limit that stops it.

    cash(t)       = allocation + mirror fill flows + mirror settlement revenue
    equity(t)     = cash(t) + open positions valued at the latest public mark
                    (positions without a mark are carried at cost and the
                    unmarked cost is reported alongside)
    drawdown      = 1 - equity / high_water
    stop rule     = drawdown > drawdown_limit  ->  status "stopped", no new
                    mirrors; open positions run to settlement

Every valuation is stored as an AllocationSnapshot; the series of snapshots
is the follower's curve. Nothing here is real money.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.marketdata import latest_marks
from alphamarket.mirror import allocation_ledger, is_asset_allocation, replay_allocation
from alphamarket.models import (
    Allocation,
    AllocationSnapshot,
    AllocationStatus,
    Strategy,
    User,
    VenueAccount,
    utcnow,
)
from alphamarket.stats import OpenPosition, mark_positions, micros_to_float

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AllocationValue:
    cash_micros: int
    open_cost_micros: int
    open_value_micros: int
    unmarked_cost_micros: int
    positions: list[OpenPosition]
    n_fills: int
    n_settlements: int
    # wave2: asset allocations' positions, in paper.asset_view_extras' shape.
    asset_positions: tuple = ()

    @property
    def equity_micros(self) -> int:
        return self.cash_micros + self.open_value_micros


def _venue_of(db: Session, allocation: Allocation) -> tuple[str, str]:
    strategy = db.get(Strategy, allocation.strategy_id)
    account = db.get(VenueAccount, strategy.venue_account_id)
    return account.venue, account.venue_env


def value_allocation(
    db: Session, allocation: Allocation, marks: dict[str, int | None] | None = None
) -> AllocationValue:
    if is_asset_allocation(db, allocation):  # wave2
        from alphamarket.paper import value_asset_allocation

        return value_asset_allocation(db, allocation, marks)
    rep = replay_allocation(db, allocation)
    cash = rep.points[-1].cash_micros if rep.points else allocation.allocation_micros
    if marks is None:
        venue, env = _venue_of(db, allocation)
        marks = latest_marks(db, venue, env, [p.ticker for p in rep.open_positions])
    marked = mark_positions(rep.open_positions, marks)
    fills, settlements = allocation_ledger(db, allocation)
    return AllocationValue(
        cash_micros=cash,
        open_cost_micros=marked.open_cost_micros,
        open_value_micros=marked.open_value_micros,
        unmarked_cost_micros=marked.unmarked_cost_micros,
        positions=rep.open_positions,
        n_fills=len(fills),
        n_settlements=len(settlements),
    )


def drawdown_from_high(equity_micros: int, high_water_micros: int) -> float:
    if high_water_micros <= 0:
        return 0.0
    return max(0.0, 1.0 - equity_micros / high_water_micros)


def snapshot_allocation(
    db: Session,
    allocation: Allocation,
    marks: dict[str, int | None] | None = None,
    as_of: datetime | None = None,
) -> AllocationSnapshot:
    """Value, record, raise the high-water mark, and enforce the drawdown
    limit. Safe to call for stopped allocations (their curve keeps updating
    while positions run off)."""
    value = value_allocation(db, allocation, marks)
    equity = value.equity_micros
    snap = AllocationSnapshot(
        allocation_id=allocation.id,
        as_of=as_of or utcnow(),
        cash_micros=value.cash_micros,
        open_cost_micros=value.open_cost_micros,
        open_value_micros=value.open_value_micros,
        equity_micros=equity,
        unmarked_cost_micros=value.unmarked_cost_micros,
    )
    db.add(snap)
    allocation.high_water_micros = max(allocation.high_water_micros, equity)
    dd = drawdown_from_high(equity, allocation.high_water_micros)
    if allocation.status == AllocationStatus.ACTIVE and dd * 10_000 > allocation.drawdown_limit_bps:
        stop_allocation(db, allocation, "drawdown_limit", commit=False)
        logger.info("allocation %s stopped: drawdown %.4f", allocation.id, dd)
    db.commit()
    return snap


def stop_allocation(db: Session, allocation: Allocation, reason: str, commit: bool = True) -> None:
    allocation.status = AllocationStatus.STOPPED
    allocation.stop_reason = reason
    allocation.stopped_at = utcnow()
    if commit:
        db.commit()


# ---- read side ---------------------------------------------------------------


def allocation_view(db: Session, allocation: Allocation) -> dict:
    strategy = db.get(Strategy, allocation.strategy_id)
    value = value_allocation(db, allocation)
    equity = value.equity_micros
    snaps = db.scalars(
        select(AllocationSnapshot)
        .where(AllocationSnapshot.allocation_id == allocation.id)
        .order_by(AllocationSnapshot.as_of, AllocationSnapshot.id)
    ).all()
    view = {
        "id": allocation.id,
        "strategy_slug": strategy.slug,
        "strategy_name": strategy.name,
        "mode": allocation.mode,
        "status": allocation.status,
        "stop_reason": allocation.stop_reason,
        "started_at": allocation.started_at.isoformat(),
        "stopped_at": allocation.stopped_at.isoformat() if allocation.stopped_at else None,
        "allocation": micros_to_float(allocation.allocation_micros),
        "drawdown_limit": allocation.drawdown_limit_bps / 10_000,
        "cash": micros_to_float(value.cash_micros),
        "open_cost": micros_to_float(value.open_cost_micros),
        "open_value": micros_to_float(value.open_value_micros),
        "unmarked_cost": micros_to_float(value.unmarked_cost_micros),
        "equity": micros_to_float(equity),
        "pnl": micros_to_float(equity - allocation.allocation_micros),
        "return": equity / allocation.allocation_micros - 1.0
        if allocation.allocation_micros > 0
        else 0.0,
        "drawdown": drawdown_from_high(equity, max(allocation.high_water_micros, equity)),
        "n_fills": value.n_fills,
        "n_settlements": value.n_settlements,
        "positions": [
            {
                "ticker": p.ticker,
                "side": p.side,
                "count": micros_to_float(p.count_micros),
                "cost": micros_to_float(p.cost_micros),
            }
            for p in value.positions
        ],
        "curve": [[s.as_of.isoformat(), micros_to_float(s.equity_micros)] for s in snaps],
        "paper": allocation.mode == "paper",
    }
    if is_asset_allocation(db, allocation):  # wave2: positions v2 and copy metrics
        from alphamarket.paper import asset_view_extras

        view.update(asset_view_extras(db, allocation, value))
    return view


def portfolio_summary(db: Session, user: User) -> dict:
    allocations = db.scalars(
        select(Allocation).where(Allocation.user_id == user.id).order_by(Allocation.id)
    ).all()
    rows = [allocation_view(db, a) for a in allocations]
    allocated = sum(a.allocation_micros for a in allocations)
    equity = sum(r["equity"] for r in rows)
    return {
        "paper": True,
        "allocations": rows,
        "n_active": sum(1 for a in allocations if a.status == AllocationStatus.ACTIVE),
        "allocated": micros_to_float(allocated),
        "value": equity,
        "pnl": equity - micros_to_float(allocated),
        "as_of": datetime.now(UTC).isoformat(),
    }
