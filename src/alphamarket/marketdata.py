"""Public market prices (M6): refresh and read the marks used to value open
positions, for creators' mark-to-market figures and for paper allocations.

Prices come from the venue's unauthenticated market endpoint, so no user
credential is ever involved. Every observation is stored (append-only by
convention) and the newest per ticker is the mark.
"""

import logging
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import MarketDataSource
from alphamarket.connectors.registry import build_market_data
from alphamarket.models import (
    Allocation,
    Fill,
    MarketPrice,
    MirrorFill,
    MirrorSettlement,
    Settlement,
    Strategy,
    VenueAccount,
)

logger = logging.getLogger(__name__)


def refresh_prices(
    db: Session,
    venue: str,
    venue_env: str,
    tickers: Iterable[str],
    source: MarketDataSource | None = None,
) -> int:
    """Fetch and store the current quote for each ticker. Returns rows added."""
    tickers = sorted(set(tickers))
    if not tickers:
        return 0
    source = source or build_market_data(venue, venue_env)
    added = 0
    for ticker in tickers:
        try:
            market = source.fetch_market(ticker)
        except Exception:
            logger.exception("price fetch failed for %s %s", venue, ticker)
            continue
        if market is None:
            continue
        db.add(
            MarketPrice(
                venue=venue,
                venue_env=venue_env,
                ticker=ticker,
                status=market.status,
                yes_bid_micros=market.yes_bid_micros,
                yes_ask_micros=market.yes_ask_micros,
                last_price_micros=market.last_price_micros,
                result=market.result,
                as_of=market.as_of,
            )
        )
        added += 1
    db.commit()
    return added


def latest_prices(
    db: Session, venue: str, venue_env: str, tickers: Iterable[str]
) -> dict[str, MarketPrice]:
    """Newest stored observation per ticker."""
    tickers = list(set(tickers))
    if not tickers:
        return {}
    newest = (
        select(MarketPrice.ticker, func.max(MarketPrice.id).label("id"))
        .where(
            MarketPrice.venue == venue,
            MarketPrice.venue_env == venue_env,
            MarketPrice.ticker.in_(tickers),
        )
        .group_by(MarketPrice.ticker)
        .subquery()
    )
    rows = db.scalars(select(MarketPrice).join(newest, MarketPrice.id == newest.c.id)).all()
    return {r.ticker: r for r in rows}


def latest_marks(
    db: Session, venue: str, venue_env: str, tickers: Iterable[str]
) -> dict[str, int | None]:
    """ticker -> YES mark in micros, for stats.mark_positions."""
    return {t: p.yes_mark_micros for t, p in latest_prices(db, venue, venue_env, tickers).items()}


def open_tickers_by_venue(db: Session) -> dict[tuple[str, str], set[str]]:
    """Tickers that may still be open somewhere, grouped by (venue, env):
    every creator fill without a creator settlement, plus every mirror fill
    without a mirror settlement. Over-inclusive is fine; a refresh costs one
    request per ticker."""
    out: dict[tuple[str, str], set[str]] = {}
    venue_of: dict[int, tuple[str, str]] = {}
    for strategy_id, venue, env in db.execute(
        select(Strategy.id, VenueAccount.venue, VenueAccount.venue_env).join(
            VenueAccount, VenueAccount.id == Strategy.venue_account_id
        )
    ).all():
        venue_of[strategy_id] = (venue, env)

    settled = set(db.execute(select(Settlement.strategy_id, Settlement.ticker).distinct()).all())
    for strategy_id, ticker in db.execute(select(Fill.strategy_id, Fill.ticker).distinct()).all():
        if (strategy_id, ticker) not in settled and strategy_id in venue_of:
            out.setdefault(venue_of[strategy_id], set()).add(ticker)

    mirror_settled = set(
        db.execute(select(MirrorSettlement.allocation_id, MirrorSettlement.ticker).distinct()).all()
    )
    for allocation_id, strategy_id, ticker in db.execute(
        select(MirrorFill.allocation_id, Allocation.strategy_id, MirrorFill.ticker)
        .join(Allocation, Allocation.id == MirrorFill.allocation_id)
        .distinct()
    ).all():
        if (allocation_id, ticker) not in mirror_settled and strategy_id in venue_of:
            out.setdefault(venue_of[strategy_id], set()).add(ticker)
    return out
