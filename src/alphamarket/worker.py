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

from alphamarket.analytics import compute_strategy_stats
from alphamarket.config import get_settings
from alphamarket.db import SessionLocal
from alphamarket.ingest import sync_account
from alphamarket.models import Strategy, VenueAccount, VenueAccountStatus
from alphamarket.reconcile import reconcile_account

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


def reconcile_all_accounts() -> None:
    """Nightly (BUILD_PLAN M2.2): compare ledger-implied cash with the venue
    balance; divergence quarantines the strategy."""
    with SessionLocal() as db:
        accounts = db.scalars(
            select(VenueAccount).where(VenueAccount.status != VenueAccountStatus.DISCONNECTED)
        ).all()
        for account in accounts:
            try:
                snap = reconcile_account(db, account)
                logger.info(
                    "reconciled account %s: divergence %d micros (tolerance %d) -> %s",
                    account.id,
                    snap.divergence_micros,
                    snap.tolerance_micros,
                    "ok" if snap.reconciled else "QUARANTINED",
                )
            except Exception:
                logger.exception("reconcile failed for venue account %s", account.id)


def compute_all_stats() -> None:
    """Nightly after reconciliation (BUILD_PLAN M3): refresh strategy_stats."""
    with SessionLocal() as db:
        strategies = db.scalars(select(Strategy)).all()
        for strategy in strategies:
            try:
                compute_strategy_stats(db, strategy)
            except Exception:
                logger.exception("stats failed for strategy %s", strategy.id)


def nightly() -> None:
    reconcile_all_accounts()
    compute_all_stats()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    interval = get_settings().ingest_interval_seconds
    scheduler = BlockingScheduler()
    scheduler.add_job(sync_all_accounts, "interval", seconds=interval, max_instances=1)
    scheduler.add_job(nightly, "cron", hour=4, minute=15, max_instances=1)
    logger.info("ingestion worker started, polling every %ss", interval)
    sync_all_accounts()  # one sweep immediately at startup
    scheduler.start()


if __name__ == "__main__":
    main()
