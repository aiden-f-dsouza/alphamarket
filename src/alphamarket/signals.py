"""Signals (M6): the creator's post-anchor fills, re-expressed as the events
followers monitor and paper allocations mirror.

One signal per fill, emitted after each sync, in fill order. Append-only
(invariant #3 extended): a signal is a fact about a fill that already
happened, never a recommendation. Each row carries the creator's equity just
before the fill so a follower can be sized proportionally without replaying
the creator's whole ledger.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.models import Fill, Settlement, Signal, Strategy, VenueAccount
from alphamarket.stats import replay

logger = logging.getLogger(__name__)


def emit_signals(db: Session, strategy: Strategy) -> int:
    """Create Signal rows for every post-anchor fill that has none. Idempotent
    (unique on fill_id). Returns rows added."""
    account = db.get(VenueAccount, strategy.venue_account_id)
    if account is None or account.anchor_balance_micros is None or account.anchor_at is None:
        return 0
    fills = db.scalars(
        select(Fill).where(Fill.strategy_id == strategy.id).order_by(Fill.filled_at, Fill.id)
    ).all()
    if not fills:
        return 0
    done = set(db.scalars(select(Signal.fill_id).where(Signal.strategy_id == strategy.id)).all())
    settlements = db.scalars(select(Settlement).where(Settlement.strategy_id == strategy.id)).all()
    rep = replay(
        account.anchor_balance_micros,
        account.anchor_at,
        fills,
        settlements,
        account.anchor_positions,
    )
    added = 0
    for f in fills:
        if f.id in done:
            continue
        ctx = rep.fill_context.get(f.venue_fill_id)
        if ctx is None:  # pre-anchor fill: not part of the verified record
            continue
        equity_before, position_after = ctx
        db.add(
            Signal(
                strategy_id=strategy.id,
                fill_id=f.id,
                ticker=f.ticker,
                side=f.side,
                action=f.action,
                count_micros=f.count_micros,
                price_micros=f.price_micros,
                creator_equity_micros=equity_before,
                position_after_micros=position_after,
                filled_at=f.filled_at,
            )
        )
        added += 1
    db.commit()
    if added:
        logger.info("emitted %d signals for strategy %s", added, strategy.id)
    return added


def recent_signals(db: Session, strategy_id: int, limit: int = 20) -> list[Signal]:
    return db.scalars(
        select(Signal)
        .where(Signal.strategy_id == strategy_id)
        .order_by(Signal.filled_at.desc(), Signal.id.desc())
        .limit(limit)
    ).all()
