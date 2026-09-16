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
from alphamarket.charts import polyline, window_x
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
TICKER_ITEMS = 12

router = APIRouter(prefix="/api/strategies", tags=["public"])


def is_visible(strategy: Strategy, account: VenueAccount | None = None) -> bool:
    """Public when the creator wants it, it is not quarantined or delisted,
    and ownership of the venue account is proven (M7: address-keyed venues
    need a profile-code claim; an unclaimed account could be anyone's)."""
    if not strategy.public or strategy.status in (
        StrategyStatus.QUARANTINED,
        StrategyStatus.DELISTED,
    ):
        return False
    return account is None or account.claimed


def verification_tier(account: VenueAccount | None) -> str | None:
    if account is None:
        return None
    from alphamarket.venues import verification_tier as _tier  # venues imports public

    return _tier(account)


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
        "verification_tier": verification_tier(account),
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
        "peak_invested": micros_to_float(stats.extras.get("peak_invested_micros") or 0),
        "return_on_peak_invested": stats.extras.get("return_on_peak_invested"),
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


def recent_public_settlements(db: Session, limit: int = TICKER_ITEMS) -> list:
    """Newest settlements across every visible strategy, for the landing
    ticker. Same visibility rule as the marketplace: an unlisted, quarantined
    or unclaimed account's trades never appear."""
    rows = db.scalars(
        select(Settlement).order_by(Settlement.settled_at.desc(), Settlement.id.desc()).limit(200)
    ).all()
    seen: dict[int, bool] = {}
    names: dict[int, str] = {}
    out = []
    for s in rows:
        if s.strategy_id not in seen:
            strategy = db.get(Strategy, s.strategy_id)
            account = db.get(VenueAccount, strategy.venue_account_id) if strategy else None
            seen[s.strategy_id] = bool(strategy) and is_visible(strategy, account)
            if strategy:
                names[s.strategy_id] = strategy.name
        if not seen[s.strategy_id]:
            continue
        side = "yes" if s.yes_count_micros >= s.no_count_micros else "no"
        out.append(
            {
                "strategy": names[s.strategy_id],
                "ticker": s.ticker,
                "side": side,
                "won": s.market_result == side,
                "payout": micros_to_float(s.revenue_micros),
                "settled_at": s.settled_at.date().isoformat(),
            }
        )
        if len(out) >= limit:
            break
    return out


def list_visible(db: Session, include_incubating: bool = False, tag: str | None = None) -> list:
    """Rows for the explore page: every visible strategy that has stats, newest
    stats row each, sorted by total return. Incubating strategies (< 90 verified
    days) are hidden unless asked for; quarantined/delisted/unlisted never show."""
    rows = []
    for strategy in db.scalars(select(Strategy).order_by(Strategy.id)).all():
        account = db.get(VenueAccount, strategy.venue_account_id)
        if not is_visible(strategy, account):
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
        daily = [float(e) for _, e in stats.extras.get("daily_equity", [])[-SPARKLINE_DAYS:]]
        rows.append(
            {
                "slug": strategy.slug,
                "name": strategy.name,
                "display_name": owner.display_name if owner else "",
                "tags": tags,
                "incubating": incubating,
                "venue": account.venue if account else None,
                "verification_tier": verification_tier(account),
                "total_return": stats.total_return
                if stats.total_return is not None
                else (stats.extras.get("return_on_peak_invested") or 0.0),
                "max_drawdown": stats.max_drawdown,
                "risk_score": stats.risk_score,
                "track_days": stats.track_days,
                "reconciled": last_recon.reconciled if last_recon else None,
                "spark": polyline(daily, 120, 32),
                "spark_dd": window_x(daily, 120),
            }
        )
    rows.sort(key=lambda r: r["total_return"], reverse=True)
    return rows


def all_tags(rows: list) -> list[str]:
    return sorted({t for r in rows for t in r["tags"]})


def visible_strategy_or_404(db: Session, slug: str) -> Strategy:
    strategy = db.scalar(select(Strategy).where(Strategy.slug == slug))
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    account = db.get(VenueAccount, strategy.venue_account_id)
    if not is_visible(strategy, account):
        raise HTTPException(status_code=404, detail="Strategy not found")
    return strategy


@router.get("/{slug}")
def strategy_json(slug: str, db: Session = Depends(get_db)) -> dict:
    return page_data(db, visible_strategy_or_404(db, slug))


@router.get("/{slug}/signals")
def strategy_signals(slug: str, db: Session = Depends(get_db)) -> dict:
    """Recent fills of a visible strategy as signals (M6). Sizes are shown as
    a fraction of the creator's equity, not in dollars."""
    from alphamarket.follows import signal_item
    from alphamarket.signals import recent_signals

    strategy = visible_strategy_or_404(db, slug)
    return {
        "slug": strategy.slug,
        "signals": [signal_item(strategy, s) for s in recent_signals(db, strategy.id)],
    }
