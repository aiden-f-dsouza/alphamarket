"""Persist stats (M3): compute from the ledger and store a strategy_stats row.

Rows are snapshots (one per run); the newest is what the dashboard and the
public page read. Nothing here writes to fills or settlements.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.models import Fill, Settlement, Strategy, StrategyStats, VenueAccount
from alphamarket.stats import compute_stats

logger = logging.getLogger(__name__)


def compute_strategy_stats(
    db: Session, strategy: Strategy, as_of: datetime | None = None
) -> StrategyStats:
    account = db.get(VenueAccount, strategy.venue_account_id)
    if account is None or account.anchor_balance_micros is None or account.anchor_at is None:
        raise ValueError(f"strategy {strategy.id} has no anchored venue account")
    fills = db.scalars(select(Fill).where(Fill.strategy_id == strategy.id)).all()
    settlements = db.scalars(select(Settlement).where(Settlement.strategy_id == strategy.id)).all()
    r = compute_stats(
        account.anchor_balance_micros,
        account.anchor_at,
        fills,
        settlements,
        as_of=as_of or datetime.now(UTC),
        anchor_positions=account.anchor_positions,
    )
    row = StrategyStats(
        strategy_id=strategy.id,
        as_of=r.as_of,
        track_days=r.track_days,
        n_fills=r.n_fills,
        n_settlements=r.n_settlements,
        total_return=r.total_return,
        max_drawdown=r.max_drawdown,
        sharpe=r.sharpe,
        sortino=r.sortino,
        win_rate=r.win_rate,
        risk_score=r.risk_score,
        equity_micros=r.equity_micros,
        realized_pnl_micros=r.realized_pnl_micros,
        flags=list(r.flags),
        extras={
            "top_ticker_share": r.top_ticker_share,
            "top_series_share": r.top_series_share,
            "max_stake_fraction": r.max_stake_fraction,
            "daily_equity": [[d.isoformat(), e] for d, e in r.daily_equity],
        },
    )
    db.add(row)
    db.commit()
    logger.info("stats for strategy %s: risk %d flags %s", strategy.id, r.risk_score, r.flags)
    return row


def latest_stats(db: Session, strategy_id: int) -> StrategyStats | None:
    return db.scalar(
        select(StrategyStats)
        .where(StrategyStats.strategy_id == strategy_id)
        .order_by(StrategyStats.as_of.desc(), StrategyStats.id.desc())
        .limit(1)
    )
