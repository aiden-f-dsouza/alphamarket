"""Worker (M1.3, M6; job queue since P2): APScheduler ticks ENQUEUE work, a
claim loop RUNS it.

Run as its own process alongside the API (as many as you like; see
docs/worker.md):
    uv run python -m alphamarket.worker

Ticks (enqueue only, idempotent across processes thanks to dedupe keys):
- every worker_sync_tick_seconds: a `sync` job for each account whose
  next_sync_at has come (deterministic per-account jitter), a global
  `mirror` sweep, and a `backfill` for paged venues not yet backfilled;
- every price_refresh_interval_seconds: a global `prices` job;
- nightly 04:15 UTC: `reconcile` per reconcilable account, then one global
  `stats` job (waits for the reconciles), and housekeeping.

The claim loop (jobs.JobRunner) takes jobs with FOR UPDATE SKIP LOCKED and
runs them through the handlers below. A budget/rate-limit/open-circuit
deferral re-queues without counting an attempt; a real failure backs off
exponentially and dead-letters after worker_max_attempts, setting the
account to `error` with a user-facing reason.
"""

import logging
import signal
from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from alphamarket.analytics import compute_strategy_stats, promote_incubating
from alphamarket.config import get_settings
from alphamarket.connectors.base import BudgetExhausted, InstrumentKind, RateLimited
from alphamarket.connectors.registry import build_market_data, get_profile
from alphamarket.db import SessionLocal, engine
from alphamarket.ingest import sync_account
from alphamarket.jobs import (
    PAGE_RUNNERS,
    PRIORITY_SYNC_FOLLOWED,
    Defer,
    JobContext,
    JobKind,
    JobRunner,
    active_jobs,
    aware,
    enqueue,
    enqueue_due_syncs,
    get_schedule,
    prune_finished,
    register_handler,
    run_page_streams,
    sync_priority,
    utcnow,
)
from alphamarket.marketdata import open_tickers_by_venue, refresh_prices
from alphamarket.mirror import process_allocation
from alphamarket.models import (
    AccountSchedule,
    Allocation,
    Strategy,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.portfolio import snapshot_allocation
from alphamarket.reconcile import reconcilable, reconcile_account
from alphamarket.signals import emit_signals
from alphamarket.venue_limits import build_limiter, rate_limit_from_http

logger = logging.getLogger(__name__)

# The nightly stats job waits for that night's reconcile jobs, but not forever.
STATS_BARRIER_MAX = timedelta(hours=1)
STATS_BARRIER_POLL_SECONDS = 60


def _strategy_for(db: Session, account: VenueAccount) -> Strategy | None:
    return db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))


def _is_paged(account: VenueAccount) -> bool:
    """Non-binary venues ingest through the v2 page streams (PAGE_RUNNERS)."""
    return get_profile(account.venue).instrument is not InstrumentKind.BINARY


# ---- handlers -------------------------------------------------------------------


@register_handler(JobKind.SYNC)
def handle_sync(ctx: JobContext) -> None:
    """Incremental sync of one account, then its signals (and so mirrors)."""
    db, account = ctx.db, ctx.account
    assert account is not None
    if _is_paged(account):
        run_page_streams(ctx, incremental=True)
        account.status = VenueAccountStatus.ACTIVE
        account.last_sync_error = None
        account.last_synced_at = ctx.clock()
        db.commit()
    else:
        _sync_binary(ctx)
    enqueue(db, JobKind.SIGNALS, account_id=account.id, priority=sync_priority(db, account.id))
    db.commit()


def _sync_binary(ctx: JobContext) -> None:
    """The M7 path (ingest.sync_account). It records every failure on the
    account row; a deferral is not a failure, so the prior state is put back
    before the deferral propagates."""
    db, account = ctx.db, ctx.account
    before = (account.status, account.last_sync_error)
    try:
        sync_account(db, account)
    except Exception as exc:
        if isinstance(exc, BudgetExhausted | RateLimited) or rate_limit_from_http(exc):
            account.status, account.last_sync_error = before
            db.commit()
        raise


@register_handler(JobKind.BACKFILL)
def handle_backfill(ctx: JobContext) -> None:
    """Full history through the page streams, resumable from the job's cursor."""
    account = ctx.account
    assert account is not None
    run_page_streams(ctx, incremental=False)
    sched = get_schedule(ctx.db, account.id)
    sched.backfill_done_at = ctx.clock()
    ctx.db.commit()


@register_handler(JobKind.RECONCILE)
def handle_reconcile(ctx: JobContext) -> None:
    account = ctx.account
    assert account is not None
    if not reconcilable(account):
        return  # fills-only tier: nothing to compare against
    snap = reconcile_account(ctx.db, account)
    logger.info(
        "reconciled account %s: divergence %d micros (tolerance %d) -> %s",
        account.id,
        snap.divergence_micros,
        snap.tolerance_micros,
        "ok" if snap.reconciled else "QUARANTINED",
    )


@register_handler(JobKind.SIGNALS)
def handle_signals(ctx: JobContext) -> None:
    """Every new post-anchor creator fill becomes a signal; then mirror the
    strategy's allocations at the same priority."""
    db = ctx.db
    if ctx.account is not None:
        strategy = _strategy_for(db, ctx.account)
        strategies = [strategy] if strategy is not None else []
    else:
        strategies = list(db.scalars(select(Strategy)).all())
    for strategy in strategies:
        try:
            emit_signals(db, strategy)
        except Exception:
            db.rollback()
            logger.exception("signal emission failed for strategy %s", strategy.id)
            continue
        if ctx.account is not None:
            enqueue(
                db,
                JobKind.MIRROR,
                payload={"strategy_id": strategy.id},
                dedupe_key=f"mirror:strategy:{strategy.id}",
                priority=ctx.job.priority,
            )
    db.commit()


@register_handler(JobKind.MIRROR)
def handle_mirror(ctx: JobContext) -> None:
    """Apply new signals and resolved markets to allocations (one strategy's,
    or all). Stopped allocations still settle so their curves finish."""
    db = ctx.db
    q = select(Allocation.id)
    strategy_id = (ctx.job.payload or {}).get("strategy_id")
    if strategy_id is not None:
        q = q.where(Allocation.strategy_id == strategy_id)
    for allocation_id in db.scalars(q).all():
        # The global `mirror` job and per-strategy `mirror:strategy:<id>` jobs
        # can run at once on different workers. mirror_pending reads
        # last_signal_id and appends mirror_fills (append-only, no unique key
        # per signal), so two concurrent passes would mirror a signal twice.
        # Lock the allocation row for the pass; a worker that finds it locked
        # skips it (the holder is already applying those signals).
        allocation = db.scalars(
            select(Allocation)
            .where(Allocation.id == allocation_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).first()
        if allocation is None:
            db.rollback()
            continue
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
            db.rollback()
            logger.exception("mirroring failed for allocation %s", allocation.id)


@register_handler(JobKind.PRICES)
def handle_prices(ctx: JobContext) -> None:
    """Public quotes for every ticker that may still be open. Each venue's
    market data spends that venue's IP bucket. A venue that is out of budget,
    answers 429 or has an open circuit is left for the next prices tick: the
    quotes already fetched are kept, and the job is NOT re-queued early,
    because a refresh has no cursor and an early re-run would spend the
    venue's shared IP budget re-fetching the same leading tickers."""
    db, limiter = ctx.db, ctx.limiter
    for (venue, venue_env), tickers in open_tickers_by_venue(db).items():
        if limiter is not None and limiter.breaker_open_until(venue) is not None:
            continue
        try:
            source = build_market_data(venue, venue_env)
            if limiter is not None:
                source.set_throttle(limiter.throttle(venue, None))
            n = refresh_prices(db, venue, venue_env, tickers, source=source)
            logger.info("refreshed %d/%d %s %s quotes", n, len(tickers), venue, venue_env)
        except (BudgetExhausted, RateLimited) as exc:
            db.rollback()
            if isinstance(exc, RateLimited) and limiter is not None:
                limiter.on_rate_limited(venue, None, exc.retry_after_seconds, exc.endpoint)
            logger.info(
                "%s %s price refresh stopped until the next tick: %s", venue, venue_env, exc
            )
        except Exception:
            db.rollback()
            logger.exception("price refresh failed for %s %s", venue, venue_env)


@register_handler(JobKind.STATS)
def handle_stats(ctx: JobContext) -> None:
    """Nightly after reconciliation (BUILD_PLAN M3): strategy_stats,
    incubation promotion, and one valuation snapshot per allocation."""
    db = ctx.db
    created = aware(ctx.job.created_at)
    if active_jobs(db, JobKind.RECONCILE) and ctx.clock() - created < STATS_BARRIER_MAX:
        raise Defer(STATS_BARRIER_POLL_SECONDS, "waiting for tonight's reconcile jobs")
    for strategy in db.scalars(select(Strategy)).all():
        try:
            compute_strategy_stats(db, strategy)
        except Exception:
            db.rollback()
            logger.exception("stats failed for strategy %s", strategy.id)
    logger.info("promoted %d strategies to live", promote_incubating(db))
    for allocation in db.scalars(select(Allocation)).all():
        try:
            snapshot_allocation(db, allocation)
        except Exception:
            db.rollback()
            logger.exception("valuation failed for allocation %s", allocation.id)


# ---- ticks (enqueue only) ---------------------------------------------------------


def tick_syncs(session_factory: sessionmaker = SessionLocal) -> None:
    settings = get_settings()
    now = utcnow()
    with session_factory() as db:
        n = enqueue_due_syncs(db, interval_seconds=settings.ingest_interval_seconds, now=now)
        enqueue(db, JobKind.MIRROR, now=now)
        if PAGE_RUNNERS:
            _enqueue_backfills(db, now)
        db.commit()
    if n:
        logger.info("queued %d sync jobs", n)


def _enqueue_backfills(db: Session, now) -> None:
    """Paged venues get one full-history backfill until it completes."""
    rows = db.execute(
        select(VenueAccount, AccountSchedule.backfill_done_at)
        .outerjoin(AccountSchedule, AccountSchedule.venue_account_id == VenueAccount.id)
        .where(VenueAccount.status != VenueAccountStatus.DISCONNECTED)
    ).all()
    for account, done_at in rows:
        if done_at is None and _is_paged(account):
            enqueue(db, JobKind.BACKFILL, account_id=account.id, now=now)


def tick_prices(session_factory: sessionmaker = SessionLocal) -> None:
    with session_factory() as db:
        enqueue(db, JobKind.PRICES)
        db.commit()


def tick_nightly(session_factory: sessionmaker = SessionLocal) -> None:
    """Keys carry the date and once=True, so two worker processes ticking the
    same night still queue each job once."""
    now = utcnow()
    day = now.date().isoformat()
    with session_factory() as db:
        accounts = db.scalars(
            select(VenueAccount).where(VenueAccount.status != VenueAccountStatus.DISCONNECTED)
        ).all()
        for account in accounts:
            if reconcilable(account):
                enqueue(
                    db,
                    JobKind.RECONCILE,
                    account_id=account.id,
                    dedupe_key=f"reconcile:{account.id}:{day}",
                    once=True,
                    now=now,
                )
        enqueue(db, JobKind.PRICES, now=now)
        enqueue(db, JobKind.STATS, dedupe_key=f"stats:{day}", once=True, now=now)
        db.commit()
        pruned = prune_finished(db, now=now)
    logger.info("nightly jobs queued; pruned %d finished jobs", pruned)


def sync_now(db: Session, account: VenueAccount) -> int | None:
    """Queue an immediate sync (e.g. from the dashboard) at top sync priority."""
    job_id = enqueue(db, JobKind.SYNC, account_id=account.id, priority=PRIORITY_SYNC_FOLLOWED)
    db.commit()
    return job_id


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    settings = get_settings()
    runner = JobRunner(SessionLocal, build_limiter(settings, engine))
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        tick_syncs, "interval", seconds=settings.worker_sync_tick_seconds, max_instances=1
    )
    scheduler.add_job(
        tick_prices, "interval", seconds=settings.price_refresh_interval_seconds, max_instances=1
    )
    scheduler.add_job(tick_nightly, "cron", hour=4, minute=15, max_instances=1)
    tick_syncs()  # queue due work immediately at startup
    scheduler.start()
    stopping = False

    def _stop(*_args) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info(
        "worker %s started: claiming %d jobs at a time, sync interval %ss",
        runner.worker_id,
        settings.worker_claim_size,
        settings.ingest_interval_seconds,
    )
    try:
        runner.loop(lambda: stopping)
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    main()
