"""Public strategy page data (M4.2).

Only platform-computed numbers from venue data appear here (invariant #5,
PRD TR-1). The trader's description is passed through labeled unverified.
Privacy default: the page shows the display name, never the email.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.analytics import latest_stats
from alphamarket.charts import polyline
from alphamarket.db import get_db
from alphamarket.models import (
    EquitySnapshot,
    Settlement,
    Strategy,
    StrategyStatus,
    User,
    VenueAccount,
)
from alphamarket.stats import drawdown_series, micros_to_float, monthly_returns

INCUBATION_DAYS = 90
RECENT_SETTLEMENTS = 5
SPARKLINE_DAYS = 60

router = APIRouter(prefix="/api/strategies", tags=["public"])


def is_visible(strategy: Strategy) -> bool:
    return strategy.public and strategy.status not in (
        StrategyStatus.QUARANTINED,
        StrategyStatus.DELISTED,
    )


def page_data(db: Session, strategy: Strategy) -> dict:
    account = db.get(VenueAccount, strategy.venue_account_id)
    owner = db.get(User, strategy.user_id)
    stats = latest_stats(db, strategy.id)
    last_recon = db.scalar(
        select(EquitySnapshot)
        .where(EquitySnapshot.strategy_id == strategy.id)
        .order_by(EquitySnapshot.as_of.desc())
        .limit(1)
    )

    data = {
        "slug": strategy.slug,
        "name": strategy.name,
        "display_name": owner.display_name if owner else "",
        "venue": account.venue if account else None,
        "venue_env": account.venue_env if account else None,
        "status": strategy.status,
        "style_tags": [t.strip() for t in strategy.style_tags.split(",") if t.strip()],
        "description_unverified": strategy.description,
        "verified_since": account.anchor_at.isoformat() if account and account.anchor_at else None,
        "last_reconciled": last_recon.as_of.isoformat() if last_recon else None,
        "reconciled": last_recon.reconciled if last_recon else None,
        "stats": None,
    }
    if stats is None:
        return data

    daily = [(date.fromisoformat(d), int(e)) for d, e in stats.extras.get("daily_equity", [])]
    data["incubating"] = stats.track_days < INCUBATION_DAYS
    data["stats"] = {
        "as_of": stats.as_of.isoformat(),
        "track_days": stats.track_days,
        "n_fills": stats.n_fills,
        "n_settlements": stats.n_settlements,
        "total_return": stats.total_return,
        "max_drawdown": stats.max_drawdown,
        "sharpe": stats.sharpe,
        "sortino": stats.sortino,
        "win_rate": stats.win_rate,
        "risk_score": stats.risk_score,
        "equity": micros_to_float(stats.equity_micros),
        "realized_pnl": micros_to_float(stats.realized_pnl_micros),
        "flags": list(stats.flags),
        "top_series_share": stats.extras.get("top_series_share"),
        "equity_curve": [[d.isoformat(), micros_to_float(e)] for d, e in daily],
        "drawdown_curve": [[d.isoformat(), dd] for d, dd in drawdown_series(daily)],
        "monthly_returns": [[y, m, r] for y, m, r in monthly_returns(daily)],
    }
    data["recent_settlements"] = recent_settlements(db, strategy.id)
    return data


def recent_settlements(db: Session, strategy_id: int, limit: int = RECENT_SETTLEMENTS) -> list:
    """Newest settlements as the venue reported them: ticker, which side the
    trader held (contract counts), the market result and the payout. Payout is
    the venue's revenue figure, not a per-trade P&L (cost basis lives in fills)."""
    rows = db.scalars(
        select(Settlement)
        .where(Settlement.strategy_id == strategy_id)
        .order_by(Settlement.settled_at.desc(), Settlement.id.desc())
        .limit(limit)
    ).all()
    return [
        {
            "ticker": s.ticker,
            "result": s.market_result,
            "side": "yes" if s.yes_count_micros >= s.no_count_micros else "no",
            "count": micros_to_float(max(s.yes_count_micros, s.no_count_micros)),
            "payout": micros_to_float(s.revenue_micros),
            "settled_at": s.settled_at.date().isoformat(),
        }
        for s in rows
    ]


def list_visible(db: Session, include_incubating: bool = False, tag: str | None = None) -> list:
    """Rows for the explore page: every visible strategy that has stats, newest
    stats row each, sorted by total return. Incubating strategies (< 90 verified
    days) are hidden unless asked for; quarantined/delisted/unlisted never show."""
    rows = []
    for strategy in db.scalars(select(Strategy).order_by(Strategy.id)).all():
        if not is_visible(strategy):
            continue
        tags = [t.strip() for t in strategy.style_tags.split(",") if t.strip()]
        if tag and tag not in tags:
            continue
        stats = latest_stats(db, strategy.id)
        if stats is None:
            continue
        incubating = stats.track_days < INCUBATION_DAYS
        if incubating and not include_incubating:
            continue
        owner = db.get(User, strategy.user_id)
        last_recon = db.scalar(
            select(EquitySnapshot)
            .where(EquitySnapshot.strategy_id == strategy.id)
            .order_by(EquitySnapshot.as_of.desc())
            .limit(1)
        )
        daily = stats.extras.get("daily_equity", [])[-SPARKLINE_DAYS:]
        rows.append(
            {
                "slug": strategy.slug,
                "name": strategy.name,
                "display_name": owner.display_name if owner else "",
                "tags": tags,
                "incubating": incubating,
                "total_return": stats.total_return or 0.0,
                "max_drawdown": stats.max_drawdown,
                "risk_score": stats.risk_score,
                "track_days": stats.track_days,
                "reconciled": last_recon.reconciled if last_recon else None,
                "spark": polyline([float(e) for _, e in daily], 120, 32),
            }
        )
    rows.sort(key=lambda r: r["total_return"], reverse=True)
    return rows


def all_tags(rows: list) -> list[str]:
    return sorted({t for r in rows for t in r["tags"]})


def visible_strategy_or_404(db: Session, slug: str) -> Strategy:
    strategy = db.scalar(select(Strategy).where(Strategy.slug == slug))
    if strategy is None or not is_visible(strategy):
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


@router.get("/{slug}")
def strategy_json(slug: str, db: Session = Depends(get_db)) -> dict:
    return page_data(db, visible_strategy_or_404(db, slug))
