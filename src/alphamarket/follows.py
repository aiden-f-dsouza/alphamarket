"""Following and the feed (M6, the "monitor" half of the investor side).

A follow is a watch: it changes what the user's feed shows and nothing else.
The feed is built from platform-recorded events only (signals derived from
ingested fills, and settlements), so invariant #5 holds: no self-reported
activity appears.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.auth import current_user
from alphamarket.db import get_db
from alphamarket.models import Allocation, Follow, Settlement, Signal, Strategy, User
from alphamarket.public import is_visible, visible_strategy_or_404
from alphamarket.ratelimit import rate_limit
from alphamarket.stats import micros_to_float

router = APIRouter(tags=["follows"])

FEED_LIMIT = 50


class FollowOut(BaseModel):
    strategy_slug: str
    strategy_name: str
    created_at: datetime


def _follow_out(strategy: Strategy, follow: Follow) -> dict:
    return {
        "strategy_slug": strategy.slug,
        "strategy_name": strategy.name,
        "created_at": follow.created_at,
    }


@router.post(
    "/follows/{slug}",
    response_model=FollowOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("follow_user", by="user"))],  # p3-auth
)
def follow(slug: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    strategy = visible_strategy_or_404(db, slug)
    existing = db.scalar(
        select(Follow).where(Follow.user_id == user.id, Follow.strategy_id == strategy.id)
    )
    if existing:
        return _follow_out(strategy, existing)
    row = Follow(user_id=user.id, strategy_id=strategy.id)
    db.add(row)
    db.commit()
    return _follow_out(strategy, row)


@router.delete("/follows/{slug}", status_code=status.HTTP_204_NO_CONTENT)
def unfollow(slug: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    strategy = db.scalar(select(Strategy).where(Strategy.slug == slug))
    if strategy is None:
        raise HTTPException(status_code=404, detail="Strategy not found")
    row = db.scalar(
        select(Follow).where(Follow.user_id == user.id, Follow.strategy_id == strategy.id)
    )
    if row is not None:
        db.delete(row)
        db.commit()


@router.get("/follows", response_model=list[FollowOut])
def list_follows(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(Follow).where(Follow.user_id == user.id).order_by(Follow.id)).all()
    out = []
    for f in rows:
        strategy = db.get(Strategy, f.strategy_id)
        if strategy is not None:
            out.append(_follow_out(strategy, f))
    return out


# ---- feed ----------------------------------------------------------------------


def signal_item(strategy: Strategy, s: Signal) -> dict:
    return {
        "kind": "fill",
        "at": s.filled_at.isoformat(),
        "strategy_slug": strategy.slug,
        "strategy_name": strategy.name,
        "ticker": s.ticker,
        "side": s.side,
        "action": s.action,
        "count": micros_to_float(s.count_micros),
        "price": micros_to_float(s.price_micros),
        # Size relative to the creator's equity, never the dollar amount of a
        # follower: what the feed shows is proportion, which is what mirrors.
        "stake_fraction": (
            (s.count_micros * s.price_micros / 1_000_000) / s.creator_equity_micros
            if s.creator_equity_micros > 0
            else None
        ),
    }


def settlement_item(strategy: Strategy, s: Settlement) -> dict:
    return {
        "kind": "settlement",
        "at": s.settled_at.isoformat(),
        "strategy_slug": strategy.slug,
        "strategy_name": strategy.name,
        "ticker": s.ticker,
        "result": s.market_result,
        "side": "yes" if s.yes_count_micros >= s.no_count_micros else "no",
        "count": micros_to_float(max(s.yes_count_micros, s.no_count_micros)),
        "payout": micros_to_float(s.revenue_micros),
    }


def feed_items(db: Session, strategies: list[Strategy], limit: int = FEED_LIMIT) -> list[dict]:
    ids = [s.id for s in strategies if is_visible(s)]
    if not ids:
        return []
    by_id = {s.id: s for s in strategies}
    signals = db.scalars(
        select(Signal)
        .where(Signal.strategy_id.in_(ids))
        .order_by(Signal.filled_at.desc(), Signal.id.desc())
        .limit(limit)
    ).all()
    settlements = db.scalars(
        select(Settlement)
        .where(Settlement.strategy_id.in_(ids))
        .order_by(Settlement.settled_at.desc(), Settlement.id.desc())
        .limit(limit)
    ).all()
    items = [signal_item(by_id[s.strategy_id], s) for s in signals]
    items += [settlement_item(by_id[s.strategy_id], s) for s in settlements]
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[:limit]


@router.get("/api/feed")
def feed(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    """Newest fills and settlements across the strategies the user follows
    (and paper-tails: an allocation implies interest)."""
    ids = set(db.scalars(select(Follow.strategy_id).where(Follow.user_id == user.id)).all())
    ids |= set(
        db.scalars(select(Allocation.strategy_id).where(Allocation.user_id == user.id)).all()
    )
    strategies = db.scalars(select(Strategy).where(Strategy.id.in_(ids))).all() if ids else []
    return {"items": feed_items(db, strategies), "n_following": len(ids)}
