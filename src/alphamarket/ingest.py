"""Ingestion: pull new fills/settlements for a connected venue account and
append them to the ledger.

Idempotent by construction: rows are keyed on the unique venue ids, already-
seen rows are skipped, and nothing here ever UPDATEs or DELETEs a ledger row
(invariant #3). Re-polling with overlap is therefore safe and deliberate.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, object_session

from alphamarket import credentials
from alphamarket.connectors.base import VenueConnector
from alphamarket.connectors.registry import build_connector, get_profile
from alphamarket.models import Fill, Settlement, Strategy, VenueAccount, VenueAccountStatus, utcnow
from alphamarket.redaction import redact

logger = logging.getLogger(__name__)

# Re-fetch window behind the newest stored fill, so a fill that landed in the
# same second as the previous poll's cutoff is never missed. Duplicates are
# discarded by venue-id dedup, so overlap costs nothing but bandwidth.
OVERLAP = timedelta(minutes=5)


@dataclass
class SyncResult:
    new_fills: int
    new_settlements: int


def sync_account(db: Session, account: VenueAccount) -> SyncResult:
    """Fetch and append new ledger rows for one account. Raises on failure
    after recording it on the account row; the worker retries next tick."""
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    if strategy is None:
        raise ValueError(f"venue account {account.id} has no strategy")
    connector = connector_for(account)

    try:
        result = _sync(db, connector, strategy)
    except Exception as exc:
        account.status = VenueAccountStatus.ERROR
        # Exception text only; never credentials (invariant #4).
        account.last_sync_error = redact(f"{type(exc).__name__}: {exc}")
        db.commit()
        raise

    account.status = VenueAccountStatus.ACTIVE
    account.last_sync_error = None
    account.last_synced_at = utcnow()
    db.commit()
    return result


def connector_for(
    account: VenueAccount, db: Session | None = None, purpose: str = "sync"
) -> VenueConnector:
    """Rebuild the venue client from the stored (encrypted) credential via
    credentials.py, which audits the decrypt. A venue that takes no secret
    (Polymarket) is built from the public id."""
    db = db if db is not None else object_session(account)
    if db is None:
        raise ValueError(f"venue account {account.id} is detached; cannot audit the decrypt")
    if get_profile(account.venue).needs_secret:
        opened = credentials.open_credential(db, account, purpose=purpose)
        secret, account_ref = opened.secret, opened.account_ref
    else:
        secret, account_ref = None, credentials.account_ref_for(db, account)
    return build_connector(
        account.venue, account.venue_env, account.key_id, secret, account_ref=account_ref
    )


def _sync(db: Session, connector, strategy: Strategy) -> SyncResult:
    since = _cursor(db, Fill.filled_at, strategy.id)
    fills = connector.fetch_fills(since=since)
    new_fills = 0
    if fills:
        existing = set(
            db.scalars(
                select(Fill.venue_fill_id).where(
                    Fill.venue_fill_id.in_([f.venue_fill_id for f in fills])
                )
            )
        )
        for f in fills:
            if f.venue_fill_id in existing:
                continue
            db.add(
                Fill(
                    strategy_id=strategy.id,
                    venue_fill_id=f.venue_fill_id,
                    ticker=f.ticker,
                    side=f.side,
                    action=f.action,
                    count_micros=f.count_micros,
                    price_micros=f.price_micros,
                    fee_micros=f.fee_micros,
                    notional_micros=f.notional_micros,
                    filled_at=f.filled_at,
                )
            )
            new_fills += 1

    since = _cursor(db, Settlement.settled_at, strategy.id)
    settlements = connector.fetch_settlements(since=since)
    new_settlements = 0
    if settlements:
        existing = set(
            db.scalars(
                select(Settlement.venue_settlement_id).where(
                    Settlement.venue_settlement_id.in_([s.venue_settlement_id for s in settlements])
                )
            )
        )
        for s in settlements:
            if s.venue_settlement_id in existing:
                continue
            db.add(
                Settlement(
                    strategy_id=strategy.id,
                    venue_settlement_id=s.venue_settlement_id,
                    ticker=s.ticker,
                    market_result=s.market_result,
                    yes_count_micros=s.yes_count_micros,
                    no_count_micros=s.no_count_micros,
                    revenue_micros=s.revenue_micros,
                    fee_micros=s.fee_micros,
                    settled_at=s.settled_at,
                )
            )
            new_settlements += 1

    db.commit()
    logger.info(
        "synced strategy %s: +%d fills, +%d settlements", strategy.id, new_fills, new_settlements
    )
    return SyncResult(new_fills=new_fills, new_settlements=new_settlements)


def _cursor(db: Session, ts_column, strategy_id: int) -> datetime | None:
    """Newest stored timestamp minus overlap; None means full history."""
    table = ts_column.class_
    newest = db.scalar(select(func.max(ts_column)).where(table.strategy_id == strategy_id))
    if newest is None:
        return None
    if newest.tzinfo is None:  # SQLite loses tzinfo; stored values are UTC (invariant #2)
        newest = newest.replace(tzinfo=UTC)
    return newest - OVERLAP
