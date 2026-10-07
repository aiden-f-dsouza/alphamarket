"""PENDING venue accounts (wave2, w2-p8-connect): a connect started but not
finished (SnapTrade portal round trip).

Contract: abandon_stale(db, now) -> int
    Accounts PENDING for longer than settings.pending_connect_ttl_hours:
    erase the credential (credentials.disconnect), delete the SnapTrade user
    (registry wrapper, via credentials.disconnect; a failed delete is retried
    by an AGGREGATOR_DELETE_USER job, aggregator_jobs.py), set DISCONNECTED.
    Returns accounts abandoned. Called by housekeeping, which passes the
    worker's outbound limiter.

A PENDING account has no strategy, no anchor and no ledger rows, and the
worker never touches it (jobs.INACTIVE_ACCOUNT_STATUSES); the only thing it
holds is the aggregator user secret in venue_credentials, which is exactly
what abandoning erases.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import credentials
from alphamarket.config import get_settings
from alphamarket.models import VenueAccount, VenueAccountStatus
from alphamarket.venue_limits import VenueLimiter

logger = logging.getLogger(__name__)


def stale_cutoff(now: datetime) -> datetime:
    return now - timedelta(hours=get_settings().pending_connect_ttl_hours)


def abandon_stale(db: Session, now: datetime, limiter: VenueLimiter | None = None) -> int:
    cutoff = stale_cutoff(now)
    accounts = db.scalars(
        select(VenueAccount)
        .where(
            VenueAccount.status == VenueAccountStatus.PENDING,
            VenueAccount.created_at < cutoff,
        )
        .order_by(VenueAccount.id)
    ).all()
    abandoned = 0
    for account in accounts:
        # Lock and re-check: the owner may have finished the connect since the
        # select (venues.connect_account locks the same row before activating).
        db.refresh(account, with_for_update=True)
        if account.status != VenueAccountStatus.PENDING:
            db.commit()  # release the lock; it is not ours to abandon any more
            continue
        try:
            credentials.disconnect(
                db, account, actor_user_id=None, reason="pending_abandoned", limiter=limiter
            )
        except Exception:  # one bad row must not stop the sweep
            db.rollback()
            logger.exception("could not abandon pending account %s", account.id)
            continue
        abandoned += 1
    return abandoned
