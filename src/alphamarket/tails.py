"""Paper tailing API (M6, the "invest" half of the investor side).

An allocation is virtual capital that mirrors a strategy's signals
proportionally (mirror.py) and is valued at public market marks
(portfolio.py). Nothing here moves money and nothing here touches a venue
credential. Live mode is refused by the execution layer regardless of input.

Amounts arrive as decimal strings and are converted with money.to_micros so
floats never touch a stored value (invariant #1).
"""

from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket.auth import current_user
from alphamarket.db import get_db
from alphamarket.mirror import process_allocation
from alphamarket.models import Allocation, AllocationMode, AllocationStatus, Signal, User
from alphamarket.money import to_micros
from alphamarket.portfolio import (
    allocation_view,
    portfolio_summary,
    snapshot_allocation,
    stop_allocation,
)
from alphamarket.ratelimit import rate_limit
from alphamarket.watchlist import tailable_strategy_or_404

router = APIRouter(tags=["tails"])

MIN_ALLOCATION_MICROS = 100 * 1_000_000  # $100
MAX_ALLOCATION_MICROS = 1_000_000 * 1_000_000  # $1M of paper


class AllocationIn(BaseModel):
    strategy_slug: str
    allocation: str = Field(description="Decimal dollars, e.g. '2000.00'")
    drawdown_limit_bps: int = Field(default=1500, ge=100, le=9000)
    mode: str = AllocationMode.PAPER


def active_allocation(db: Session, user_id: int, strategy_id: int) -> Allocation | None:
    """The user's running paper tail of this strategy (at most one)."""
    return db.scalar(
        select(Allocation).where(
            Allocation.user_id == user_id,
            Allocation.strategy_id == strategy_id,
            Allocation.status == AllocationStatus.ACTIVE,
        )
    )


def active_tail_count(db: Session, strategy_id: int) -> int:
    """How many paper tails of this strategy are running (the page's
    "Paper tails · N following")."""
    return (
        db.scalar(
            select(func.count(Allocation.id)).where(
                Allocation.strategy_id == strategy_id,
                Allocation.status == AllocationStatus.ACTIVE,
            )
        )
        or 0
    )


def create_allocation(db: Session, user: User, payload: AllocationIn) -> Allocation:
    try:
        mode = AllocationMode(payload.mode)
    except ValueError:
        raise HTTPException(status_code=422, detail="Unknown mode") from None
    if mode is not AllocationMode.PAPER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only paper tailing is available. Live mirroring requires a licensed party.",
        )
    # A public strategy, or a watched address on the user's own watchlist.
    strategy = tailable_strategy_or_404(db, user, payload.strategy_slug)
    if strategy.user_id == user.id:
        raise HTTPException(status_code=422, detail="You cannot tail your own strategy")
    try:
        micros = to_micros(Decimal(payload.allocation))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Allocation must be a decimal amount") from None
    if not MIN_ALLOCATION_MICROS <= micros <= MAX_ALLOCATION_MICROS:
        raise HTTPException(
            status_code=422, detail="Allocation must be between $100 and $1,000,000"
        )
    if active_allocation(db, user.id, strategy.id) is not None:
        raise HTTPException(status_code=409, detail="You already have an active tail here")
    row = Allocation(
        user_id=user.id,
        strategy_id=strategy.id,
        mode=mode,
        allocation_micros=micros,
        drawdown_limit_bps=payload.drawdown_limit_bps,
        high_water_micros=micros,
    )
    db.add(row)
    db.flush()
    # A follower joins going forward: every signal already emitted is
    # considered handled so the first engine pass never replays history.
    row.last_signal_id = (
        db.scalar(select(func.max(Signal.id)).where(Signal.strategy_id == strategy.id)) or 0
    )
    db.commit()
    snapshot_allocation(db, row)  # the curve starts at the allocation
    return row


@router.post(
    "/allocations",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("follow_user", by="user"))],  # p3-auth
)
def create(
    payload: AllocationIn, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return allocation_view(db, create_allocation(db, user, payload))


@router.get("/allocations")
def list_allocations(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(
        select(Allocation).where(Allocation.user_id == user.id).order_by(Allocation.id)
    ).all()
    return [allocation_view(db, a) for a in rows]


def owned_allocation(db: Session, allocation_id: int, user: User) -> Allocation:
    row = db.get(Allocation, allocation_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Allocation not found")
    return row


@router.get("/allocations/{allocation_id}")
def get_allocation(
    allocation_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    return allocation_view(db, owned_allocation(db, allocation_id, user))


def stop_by_user(db: Session, row: Allocation) -> None:
    """The follower stops a tail (JSON route and the /portfolio form). A tail
    that already stopped stays as it is."""
    if row.status == AllocationStatus.ACTIVE:
        stop_allocation(db, row, "user")
        snapshot_allocation(db, row)


@router.post("/allocations/{allocation_id}/stop")
def stop(allocation_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = owned_allocation(db, allocation_id, user)
    stop_by_user(db, row)
    return allocation_view(db, row)


@router.post("/allocations/{allocation_id}/run")
def run_now(allocation_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Apply pending signals and settlements now (the worker also does this
    after every sync)."""
    row = owned_allocation(db, allocation_id, user)
    mirrored, settled = process_allocation(db, row)
    snapshot_allocation(db, row)
    return {"mirrored": mirrored, "settled": settled, **allocation_view(db, row)}


@router.get("/api/portfolio")
def portfolio(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    return portfolio_summary(db, user)
