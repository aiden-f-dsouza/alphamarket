"""Ingestion worker (M1.3, extended in M6): poll every connected account on a
schedule, then run the signal and paper-mirroring engine; refresh public
market prices; nightly reconcile, stats, and follower valuations.

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

from alphamarket.analytics import compute_strategy_stats, promote_incubating
from alphamarket.config import get_settings
from alphamarket.db import SessionLocal
from alphamarket.ingest import sync_account
from alphamarket.marketdata import open_tickers_by_venue, refresh_prices
from alphamarket.mirror import process_allocation
from alphamarket.models import Allocation, Strategy, VenueAccount, VenueAccountStatus
from alphamarket.portfolio import snapshot_allocation
from alphamarket.reconcile import reconcilable, reconcile_account
from alphamarket.signals import emit_signals

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
    emit_all_signals()
    mirror_all_allocations()


def emit_all_signals() -> None:
    """After every sync (M6): every new post-anchor fill becomes a signal."""
    with SessionLocal() as db:
        for strategy in db.scalars(select(Strategy)).all():
            try:
                emit_signals(db, strategy)
            except Exception:
                logger.exception("signal emission failed for strategy %s", strategy.id)


def mirror_all_allocations() -> None:
    """After signals (M6): apply new signals and resolved markets to every
    allocation. Stopped allocations still settle so their curves finish."""
    with SessionLocal() as db:
        for allocation in db.scalars(select(Allocation)).all():
            try:
                mirrored, settled = process_allocation(db, allocation)
                if mirrored or settled:
                    logger.info(
                        "allocation %s: +%d mirror fills, +%d settlements",
                        allocation.id,
                        mirrored,
                        settled,
                    )
            except Exception:
                logger.exception("mirroring failed for allocation %s", allocation.id)


def refresh_all_prices() -> None:
    """Public quotes for every ticker that may still be open (M6)."""
    with SessionLocal() as db:
        for (venue, venue_env), tickers in open_tickers_by_venue(db).items():
            try:
                n = refresh_prices(db, venue, venue_env, tickers)
                logger.info("refreshed %d/%d %s %s quotes", n, len(tickers), venue, venue_env)
            except Exception:
                logger.exception("price refresh failed for %s %s", venue, venue_env)


def value_all_allocations() -> None:
    """Nightly (M6): one AllocationSnapshot per allocation, drawdown limits
    enforced."""
    with SessionLocal() as db:
        for allocation in db.scalars(select(Allocation)).all():
            try:
                snapshot_allocation(db, allocation)
            except Exception:
                logger.exception("valuation failed for allocation %s", allocation.id)


def reconcile_all_accounts() -> None:
    """Nightly (BUILD_PLAN M2.2): compare ledger-implied cash with the venue
    balance; divergence quarantines the strategy."""
    with SessionLocal() as db:
        accounts = db.scalars(
            select(VenueAccount).where(VenueAccount.status != VenueAccountStatus.DISCONNECTED)
        ).all()
        for account in accounts:
            if not reconcilable(account):
                continue  # fills-only tier: nothing to compare against
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
        logger.info("promoted %d strategies to live", promote_incubating(db))


def nightly() -> None:
    reconcile_all_accounts()
    refresh_all_prices()
    compute_all_stats()
    value_all_allocations()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    interval = get_settings().ingest_interval_seconds
    scheduler = BlockingScheduler()
    scheduler.add_job(sync_all_accounts, "interval", seconds=interval, max_instances=1)
    scheduler.add_job(
        refresh_all_prices,
        "interval",
        seconds=get_settings().price_refresh_interval_seconds,
        max_instances=1,
    )
    scheduler.add_job(nightly, "cron", hour=4, minute=15, max_instances=1)
    logger.info("ingestion worker started, polling every %ss", interval)
    sync_all_accounts()  # one sweep immediately at startup
    scheduler.start()


if __name__ == "__main__":
    main()
