"""Ingestion worker (M1.3): poll every connected account on a schedule.

Run as its own process alongside the API:
    uv run python -m alphamarket.worker

APScheduler in one process is enough at MVP scale (BUILD_PLAN 2.1); a failed
account is marked status=error with the message on the row and is retried on
the next tick. max_instances=1 means a slow sweep skips a tick instead of
overlapping itself.
"""

import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import select

from alphamarket.config import get_settings
from alphamarket.db import SessionLocal
from alphamarket.ingest import sync_account
from alphamarket.models import VenueAccount, VenueAccountStatus

logger = logging.getLogger(__name__)


def sync_all_accounts() -> None:
    with SessionLocal() as db:
        accounts = db.scalars(
            select(VenueAccount).where(VenueAccount.status != VenueAccountStatus.DISCONNECTED)
        ).all()
        for account in accounts:
            try:
                sync_account(db, account)
            except Exception:
                # Recorded on the account row by sync_account; keep sweeping.
                logger.exception("sync failed for venue account %s", account.id)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    interval = get_settings().ingest_interval_seconds
    scheduler = BlockingScheduler()
    scheduler.add_job(sync_all_accounts, "interval", seconds=interval, max_instances=1)
    logger.info("ingestion worker started, polling every %ss", interval)
    sync_all_accounts()  # one sweep immediately at startup
    scheduler.start()


if __name__ == "__main__":
    main()
