"""Asset market data persistence (wave2, workstream w2-market-data).

STUB. The worker calls run_daily (PRICES_DAILY job, nightly per pricing
venue) and refresh_asset_marks (end of every PRICES job). Contract:

run_daily(ctx) -> None
    ctx.job.payload["venue"] is a pricing venue. For every asset held by an
    account priced by that venue (anchors, creator legs, follower legs),
    resolve MarketDataSource.pricing_symbol, else the CoinGecko fallback
    (venue "coingecko"); upsert instruments (margin_tiers for perps via
    fetch_margin_tiers); insert MISSING prices_daily days up to yesterday
    UTC (usd_micros_per_unit via usd_micros_per_unit()); corporate actions
    for equity symbols held by followers. BudgetExhausted/RateLimited ->
    jobs.Defer (the missing days are the cursor).

refresh_asset_marks(db, limiter) -> int
    asset_quotes (and funding_rates for perps) for instruments in open
    follower books. Returns quotes written.

capture_quote(db, venue, venue_env, instrument, limiter) -> AssetQuote | None
    One quote now, through the instrument's pricing venue
    (profile.pricing_venue), stored in asset_quotes and returned. None when
    the source has no quote or the budget is spent (never raises for those).

latest_mark(db, instrument_id, now) -> Mark | None
    Newest asset_quotes row no older than settings.quote_max_age_seconds:
    mid of bid/ask when both, else last.

usd_micros_per_unit(close, quote_asset) -> int | None
    close (decimal string) x 1e6 truncated toward zero when the quote asset
    is USD or a stablecoin at par; None otherwise.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from alphamarket.models import AssetQuote, Instrument

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mark:
    instrument_id: int
    price: str  # decimal string, quote asset per unit
    as_of: datetime
    source: str
    basis: str  # "mid" | "last"


def run_daily(ctx) -> None:
    logger.info("prices_daily: not implemented yet (%s)", (ctx.job.payload or {}).get("venue"))


def refresh_asset_marks(db: Session, limiter) -> int:
    return 0


def capture_quote(
    db: Session, venue: str, venue_env: str, instrument: Instrument, limiter
) -> AssetQuote | None:
    return None


def latest_mark(db: Session, instrument_id: int, now: datetime) -> Mark | None:
    return None


def usd_micros_per_unit(close: str, quote_asset: str) -> int | None:
    raise NotImplementedError
