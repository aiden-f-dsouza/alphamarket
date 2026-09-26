"""Job queue (platform_plan 2.5, implementation_plan P2).

Postgres is the queue (decision E3: no Redis). A job row moves

    pending --claim--> running --complete--> done
       ^                  |--fail (attempts < max)--> pending, run_after = now + backoff
       |                  |--fail (attempts = max)--> dead (account -> error, user-facing reason)
       +--reschedule------+   (budget / rate limit / open circuit: NO attempt counted)

- enqueue(): one active (pending or running) job per dedupe key; the key
  defaults to kind + account, so a tick can never queue the same account's
  sync twice. Enforced by a partial unique index and INSERT ... ON CONFLICT
  DO NOTHING, so two ticking processes cannot race past it.
- claim(): SELECT ... FOR UPDATE SKIP LOCKED ordered by priority (lower runs
  first) then run_after, so any number of worker processes can claim from
  one table and never get the same job. A claim takes a lease; a job whose
  lease expires (worker died) is reaped as one failed attempt.
- Handlers are registered per kind (register_handler); a worker only claims
  kinds it has a handler for.
- Backfills and syncs of paged venues run PageRunners (register_page_runner)
  and keep the connector cursor in the job payload, committed with each
  page's rows, so running out of budget re-queues the job from where it
  stopped instead of failing it.

docs/worker.md is the operator's view of all of this.
"""

import hashlib
import logging
import os
import socket
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.attributes import set_committed_value

from alphamarket.config import Settings, get_settings
from alphamarket.connectors.base import BudgetExhausted, RateLimited, VenueConnector
from alphamarket.models import (
    ACTIVE_JOB_STATUSES,
    AccountSchedule,
    Allocation,
    AllocationStatus,
    Follow,
    Job,
    JobStatus,
    Strategy,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.venue_limits import (
    VenueLimiter,
    credential_ref,
    is_venue_outage,
    rate_limit_from_http,
)

logger = logging.getLogger(__name__)


class JobKind(StrEnum):
    SYNC = "sync"  # incremental ledger sync of one account
    BACKFILL = "backfill"  # full history of one account, resumable by cursor
    RECONCILE = "reconcile"  # venue balance vs ledger (nightly)
    PRICES = "prices"  # public quotes / marks (global)
    STATS = "stats"  # strategy stats, incubation promotion, allocation valuations
    MIRROR = "mirror"  # paper copying: apply signals and settlements to allocations
    SIGNALS = "signals"  # creator fills -> signals
    OAUTH_REFRESH = "oauth_refresh"  # refresh an OAuth access token (P3/P5)
    KEY_ROTATE = "key_rotate"  # re-encrypt credentials under a new data key (P3)
    SCOPE_RECHECK = "scope_recheck"  # re-prove a credential is read-only (P3)


# Lower runs first. Copy lag matters for creators with followers, so their
# syncs (and the signal/mirror jobs that follow a sync) go first; an expiring
# OAuth token would fail every sync of that account, so it goes before them.
PRIORITY_URGENT = 5
PRIORITY_SYNC_FOLLOWED = 10
PRIORITY_SYNC = 20
PRIORITY_MAINTENANCE = 30
PRIORITY_BACKFILL = 40

DEFAULT_PRIORITY: dict[str, int] = {
    JobKind.OAUTH_REFRESH: PRIORITY_URGENT,
    JobKind.SIGNALS: PRIORITY_SYNC_FOLLOWED,
    JobKind.MIRROR: PRIORITY_SYNC_FOLLOWED,
    JobKind.SYNC: PRIORITY_SYNC,
    JobKind.RECONCILE: PRIORITY_MAINTENANCE,
    JobKind.STATS: PRIORITY_MAINTENANCE,
    JobKind.PRICES: PRIORITY_MAINTENANCE,
    JobKind.SCOPE_RECHECK: PRIORITY_MAINTENANCE,
    JobKind.KEY_ROTATE: PRIORITY_MAINTENANCE,
    JobKind.BACKFILL: PRIORITY_BACKFILL,
}

KIND_LABELS: dict[str, str] = {
    JobKind.SYNC: "Syncing your venue history",
    JobKind.BACKFILL: "Importing your full venue history",
    JobKind.RECONCILE: "Reconciling your balance",
    JobKind.OAUTH_REFRESH: "Refreshing your venue connection",
    JobKind.SCOPE_RECHECK: "Re-checking that your key is read-only",
    JobKind.KEY_ROTATE: "Rotating your stored key's encryption",
}

MAX_ERROR_CHARS = 1000


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; every stored timestamp is UTC (invariant 2)."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class LeaseLost(Exception):  # noqa: N818
    """This worker no longer owns the job (its lease expired and another
    worker reclaimed it); stop without touching the job row."""


class Defer(Exception):  # noqa: N818  (a control-flow signal, not an error)
    """Raised by a handler to re-queue its job after `retry_after_seconds`
    without counting an attempt (barriers, yielding after N pages)."""

    def __init__(self, retry_after_seconds: float, reason: str = "") -> None:
        super().__init__(reason or f"deferred {retry_after_seconds}s")
        self.retry_after_seconds = retry_after_seconds
        self.reason = reason


# ---- registries ---------------------------------------------------------------


@dataclass
class JobContext:
    """What a handler gets. `db` is the handler's own session; the job row
    and the account (if any) are loaded in it."""

    db: Session
    job: Job
    account: VenueAccount | None
    worker_id: str
    settings: Settings
    limiter: VenueLimiter | None
    clock: Callable[[], datetime] = utcnow
    connector_factory: Callable[[VenueAccount], VenueConnector] | None = None
    _connector: VenueConnector | None = field(default=None, repr=False)

    @property
    def credential_id(self) -> str | None:
        if self.account is None:
            return None
        return credential_ref(self.account.venue, self.account.key_id)

    def connector(self) -> VenueConnector:
        """The account's connector with the outbound throttle attached."""
        if self.account is None:
            raise ValueError(f"job {self.job.id} ({self.job.kind}) has no venue account")
        if self._connector is None:
            factory = self.connector_factory or _default_connector_factory
            conn = factory(self.account)
            if self.limiter is not None:
                conn.set_throttle(self.limiter.throttle(self.account.venue, self.credential_id))
            self._connector = conn
        return self._connector

    def heartbeat(self) -> bool:
        """Extend the lease (commits the session). False if the lease was lost."""
        return heartbeat(
            self.db,
            self.job.id,
            self.worker_id,
            self.settings.worker_lease_seconds,
            now=self.clock(),
        )


def _default_connector_factory(account: VenueAccount) -> VenueConnector:
    from alphamarket.ingest import connector_for

    return connector_for(account)


Handler = Callable[[JobContext], None]
HANDLERS: dict[str, Handler] = {}


def register_handler(kind: str, handler: Handler | None = None):
    """Register `handler` for `kind`; usable as a decorator. Re-registering
    replaces (the last module imported wins)."""

    def _register(fn: Handler) -> Handler:
        HANDLERS[str(kind)] = fn
        return fn

    return _register(handler) if handler is not None else _register


# fn(db, account, connector, cursor) -> (next_cursor, resume_cursor). One call
# = one connector page (connectors.base.Page rules), ingested but NOT
# committed: run_page_streams commits the rows together with the job's cursor.
# next_cursor None means the stream is done; resume_cursor (final page only)
# is stored per account and passed back as `cursor` on the next incremental
# sync. A cursor of None means "start from the ledger's own watermark".
PageRunner = Callable[
    [Session, VenueAccount, VenueConnector, str | None], tuple[str | None, str | None]
]
PAGE_RUNNERS: dict[str, PageRunner] = {}


def register_page_runner(stream: str, runner: PageRunner) -> None:
    """Streams run in registration order (e.g. "asset_fills", then "events")."""
    PAGE_RUNNERS[stream] = runner


# ---- scheduling -----------------------------------------------------------------


def jitter_offset_seconds(account_id: int, interval_seconds: int) -> int:
    """Deterministic phase of an account within the sync interval: the same
    on every process and every restart (sha256, not Python's salted hash),
    and uniformly spread, so 5-minute ticks never stampede a venue."""
    digest = hashlib.sha256(f"koine-sync:{account_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % interval_seconds


def next_sync_slot(
    account_id: int, now: datetime, interval_seconds: int, offset: int | None = None
) -> datetime:
    """The first instant strictly after `now` that sits at the account's
    phase: slot = k * interval + offset (epoch seconds)."""
    if offset is None:
        offset = jitter_offset_seconds(account_id, interval_seconds)
    epoch = int(now.timestamp())
    k = (epoch - offset) // interval_seconds
    return datetime.fromtimestamp((k + 1) * interval_seconds + offset, UTC)


def backoff_seconds(attempts: int, base: int, cap: int) -> int:
    """Delay before retry number `attempts` + 1: base x 2^(attempts-1), capped."""
    return min(cap, base * 2 ** max(0, attempts - 1))


def get_schedule(db: Session, account_id: int) -> AccountSchedule:
    sched = db.get(AccountSchedule, account_id)
    if sched is None:
        sched = AccountSchedule(venue_account_id=account_id, resume_cursors={})
        db.add(sched)
        db.flush()
    return sched


def has_active_followers(db: Session, account_id: int) -> bool:
    """A creator whose strategy has followers or an active paper allocation."""
    strategy_id = db.scalar(select(Strategy.id).where(Strategy.venue_account_id == account_id))
    if strategy_id is None:
        return False
    return bool(
        db.scalar(
            select(
                or_(
                    exists().where(Follow.strategy_id == strategy_id),
                    exists().where(
                        Allocation.strategy_id == strategy_id,
                        Allocation.status == AllocationStatus.ACTIVE,
                    ),
                )
            )
        )
    )


def sync_priority(db: Session, account_id: int) -> int:
    return PRIORITY_SYNC_FOLLOWED if has_active_followers(db, account_id) else PRIORITY_SYNC


# ---- queue operations -------------------------------------------------------------


def default_dedupe_key(kind: str, account_id: int | None) -> str:
    return f"{kind}:{account_id}" if account_id is not None else str(kind)


def _insert(db: Session):
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return insert(Job)


_KIND_VALUES = frozenset(k.value for k in JobKind)
_ACTIVE_WHERE = text("status IN ('pending', 'running')")


def enqueue(
    db: Session,
    kind: str,
    *,
    account_id: int | None = None,
    payload: dict | None = None,
    priority: int | None = None,
    run_after: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
    once: bool = False,
    now: datetime | None = None,
) -> int | None:
    """Queue a job; returns its id, or None when an active job with the same
    dedupe key already exists (its priority is raised if this one is more
    urgent; its run_after is never pulled forward, so a deferral holds).
    `once=True` also skips when ANY job ever had this key (nightly keys carry
    the date). Flushes, does not commit: the caller owns the transaction."""
    if kind not in _KIND_VALUES and kind not in HANDLERS:
        raise ValueError(f"unknown job kind {kind!r}")
    now = now or utcnow()
    key = dedupe_key or default_dedupe_key(kind, account_id)
    if priority is None:
        priority = DEFAULT_PRIORITY.get(kind, PRIORITY_MAINTENANCE)
    if once and db.scalar(select(exists().where(Job.dedupe_key == key))):
        return None
    stmt = (
        _insert(db)
        .values(
            kind=str(kind),
            venue_account_id=account_id,
            dedupe_key=key,
            payload=payload or {},
            priority=priority,
            status=JobStatus.PENDING,
            run_after=run_after or now,
            attempts=0,
            max_attempts=max_attempts or get_settings().worker_max_attempts,
            deferrals=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=_ACTIVE_WHERE)
        .returning(Job.id)
    )
    job_id = db.execute(stmt).scalar()
    if job_id is None:
        db.execute(
            _update_jobs()
            .where(
                Job.dedupe_key == key,
                Job.status == JobStatus.PENDING,
                Job.priority > priority,
            )
            .values(priority=priority, updated_at=now)
        )
    return job_id


def claim(
    db: Session,
    worker_id: str,
    *,
    limit: int,
    lease_seconds: int,
    kinds: Iterable[str] | None = None,
    now: datetime | None = None,
    commit: bool = True,
) -> list[Job]:
    """Claim up to `limit` due jobs for `worker_id`. Row locks from
    FOR UPDATE SKIP LOCKED make concurrent claimers skip each other's rows;
    the status flip is committed before any handler runs. (SQLite ignores
    the lock clause; it serialises writers anyway.)"""
    now = now or utcnow()
    q = select(Job).where(Job.status == JobStatus.PENDING, Job.run_after <= now)
    if kinds is not None:
        q = q.where(Job.kind.in_(list(kinds)))
    q = q.order_by(Job.priority, Job.run_after, Job.id).limit(limit)
    jobs = list(db.scalars(q.with_for_update(skip_locked=True)).all())
    lease_until = now + timedelta(seconds=lease_seconds)
    for job in jobs:
        job.status = JobStatus.RUNNING
        job.locked_by = worker_id
        job.lease_until = lease_until
        job.updated_at = now
    if commit:
        db.commit()
    return jobs


def _update_jobs():
    # Every state change is a conditional UPDATE; never evaluate it against
    # in-session objects (SQLite hands back naive datetimes).
    return update(Job).execution_options(synchronize_session=False)


def _owned(job_id: int, worker_id: str):
    return (Job.id == job_id, Job.status == JobStatus.RUNNING, Job.locked_by == worker_id)


def heartbeat(
    db: Session, job_id: int, worker_id: str, lease_seconds: int, *, now: datetime | None = None
) -> bool:
    now = now or utcnow()
    n = db.execute(
        _update_jobs()
        .where(*_owned(job_id, worker_id))
        .values(lease_until=now + timedelta(seconds=lease_seconds), updated_at=now)
    ).rowcount
    db.commit()
    return n == 1


def complete(db: Session, job_id: int, worker_id: str, *, now: datetime | None = None) -> bool:
    now = now or utcnow()
    n = db.execute(
        _update_jobs()
        .where(*_owned(job_id, worker_id))
        .values(
            status=JobStatus.DONE, done_at=now, lease_until=None, locked_by=None, updated_at=now
        )
    ).rowcount
    db.commit()
    return n == 1


def reschedule(
    db: Session,
    job_id: int,
    worker_id: str,
    run_after: datetime,
    *,
    payload: dict | None = None,
    now: datetime | None = None,
) -> bool:
    """Back to pending at `run_after` WITHOUT counting an attempt: budget and
    rate-limit deferrals, open circuits, a backfill yielding its turn."""
    now = now or utcnow()
    values = {
        "status": JobStatus.PENDING,
        "run_after": run_after,
        "lease_until": None,
        "locked_by": None,
        "deferrals": Job.deferrals + 1,
        "updated_at": now,
    }
    if payload is not None:
        values["payload"] = payload
    n = db.execute(_update_jobs().where(*_owned(job_id, worker_id)).values(**values)).rowcount
    db.commit()
    return n == 1


def _error_text(error: BaseException | str) -> str:
    # Exception text only, never credentials (invariant 4).
    msg = error if isinstance(error, str) else f"{type(error).__name__}: {error}"
    return msg[:MAX_ERROR_CHARS]


def dead_letter_reason(kind: str, attempts: int, last_error: str) -> str:
    label = KIND_LABELS.get(kind, "A background task for this account")
    return (
        f"{label} failed {attempts} times in a row, so Koine paused it. "
        f"Last error: {last_error}. Koine will try again automatically; if you "
        "revoked or replaced the key, reconnect it."
    )[:MAX_ERROR_CHARS]


def fail(
    db: Session,
    job_id: int,
    worker_id: str,
    error: BaseException | str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    _owner_clause: tuple | None = None,
) -> str | None:
    """Count one failed attempt: back off exponentially, or dead-letter at
    max_attempts (the account goes to `error` with a user-facing reason and
    its next sync is pushed out by the cool-off). Returns the new status, or
    None if this worker no longer owns the job."""
    settings = settings or get_settings()
    now = now or utcnow()
    job = db.get(Job, job_id, populate_existing=True)
    if job is None:
        return None
    msg = _error_text(error)
    attempts = job.attempts + 1
    owner = _owner_clause if _owner_clause is not None else _owned(job_id, worker_id)
    if attempts >= job.max_attempts:
        values = {
            "status": JobStatus.DEAD,
            "attempts": attempts,
            "last_error": msg,
            "done_at": now,
            "lease_until": None,
            "locked_by": None,
            "updated_at": now,
        }
    else:
        delay = backoff_seconds(
            attempts, settings.worker_backoff_base_seconds, settings.worker_backoff_cap_seconds
        )
        values = {
            "status": JobStatus.PENDING,
            "attempts": attempts,
            "last_error": msg,
            "run_after": now + timedelta(seconds=delay),
            "lease_until": None,
            "locked_by": None,
            "updated_at": now,
        }
    if db.execute(_update_jobs().where(*owner).values(**values)).rowcount != 1:
        db.rollback()
        return None
    account = db.get(VenueAccount, job.venue_account_id) if job.venue_account_id else None
    if account is not None and account.status != VenueAccountStatus.DISCONNECTED:
        if values["status"] == JobStatus.DEAD:
            account.status = VenueAccountStatus.ERROR
            account.last_sync_error = dead_letter_reason(job.kind, attempts, msg)
            sched = get_schedule(db, account.id)
            sched.next_sync_at = now + timedelta(
                seconds=settings.worker_dead_letter_cooloff_seconds
            )
            sched.updated_at = now
            logger.error("job %s (%s) dead-lettered: %s", job_id, job.kind, msg)
        elif job.kind in (JobKind.SYNC, JobKind.BACKFILL):
            # Same as ingest.sync_account today: a failed sync shows on the account.
            account.status = VenueAccountStatus.ERROR
            account.last_sync_error = msg
    db.commit()
    return values["status"]


def reap_expired(
    db: Session, *, settings: Settings | None = None, now: datetime | None = None
) -> int:
    """Running jobs whose lease ran out (their worker died or hung) count one
    failed attempt and go back through the normal retry/dead-letter path."""
    now = now or utcnow()
    expired = db.scalars(
        select(Job.id)
        .where(Job.status == JobStatus.RUNNING, Job.lease_until < now)
        .with_for_update(skip_locked=True)
    ).all()
    for job_id in expired:
        fail(
            db,
            job_id,
            "",
            "lease expired: the worker running this job stopped",
            settings=settings,
            now=now,
            _owner_clause=(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING,
                Job.lease_until < now,  # a heartbeat since the SELECT wins
            ),
        )
    db.commit()
    return len(expired)


def prune_finished(db: Session, *, done_days: int = 7, dead_days: int = 30, now=None) -> int:
    """Housekeeping: finished job rows are coordination state, not ledger
    facts, so old ones are deleted."""
    now = now or utcnow()
    n = db.execute(
        delete(Job).where(
            or_(
                (Job.status == JobStatus.DONE) & (Job.done_at < now - timedelta(days=done_days)),
                (Job.status == JobStatus.DEAD) & (Job.done_at < now - timedelta(days=dead_days)),
            )
        )
    ).rowcount
    db.commit()
    return n


def active_jobs(db: Session, kind: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.kind == kind, Job.status.in_(ACTIVE_JOB_STATUSES))
    )


# ---- periodic enqueueing ------------------------------------------------------------


def enqueue_due_syncs(db: Session, *, interval_seconds: int, now: datetime | None = None) -> int:
    """One sync job per connected account whose next_sync_at has come, at
    follower-aware priority; next_sync_at moves to the account's next slot.
    Idempotent across processes (dedupe key sync:<account>)."""
    now = now or utcnow()
    rows = db.execute(
        select(VenueAccount.id, AccountSchedule.next_sync_at)
        .outerjoin(AccountSchedule, AccountSchedule.venue_account_id == VenueAccount.id)
        .where(VenueAccount.status != VenueAccountStatus.DISCONNECTED)
    ).all()
    queued = 0
    for account_id, next_at in rows:
        next_at = aware(next_at)
        if next_at is not None and next_at > now:
            continue
        if enqueue(
            db, JobKind.SYNC, account_id=account_id, priority=sync_priority(db, account_id), now=now
        ):
            queued += 1
        sched = get_schedule(db, account_id)
        sched.next_sync_at = next_sync_slot(account_id, now, interval_seconds)
        sched.updated_at = now
    db.commit()
    return queued


# ---- resumable page streams -----------------------------------------------------------


def _start_cursor(sched: AccountSchedule, stream: str, incremental: bool) -> str | None:
    return (sched.resume_cursors or {}).get(stream) if incremental else None


def _commit_owned_payload(ctx: JobContext, payload: dict) -> None:
    """Write the job's payload and extend its lease in one conditional UPDATE,
    then commit it with whatever else the session holds (a page's rows, the
    account's resume cursors). Rolls back and raises LeaseLost if another
    worker has taken the job over."""
    now = ctx.clock()
    n = ctx.db.execute(
        _update_jobs()
        .where(*_owned(ctx.job.id, ctx.worker_id))
        .values(
            payload=payload,
            lease_until=now + timedelta(seconds=ctx.settings.worker_lease_seconds),
            updated_at=now,
        )
    ).rowcount
    if n != 1:
        ctx.db.rollback()
        raise LeaseLost(f"job {ctx.job.id} was reclaimed by another worker")
    ctx.db.commit()
    set_committed_value(ctx.job, "payload", payload)


def run_page_streams(
    ctx: JobContext, *, incremental: bool, streams: Sequence[str] | None = None
) -> None:
    """Run each registered PageRunner over the account until its stream is
    done. The job payload holds {"streams": [remaining], "cursor": ...}; it
    is committed together with every page's ledger rows, so an exception
    (BudgetExhausted, RateLimited, a crash) loses at most the page in flight
    and the next run resumes at the committed cursor. After
    worker_backfill_pages_per_run pages it yields (Defer(0)) so other jobs
    get a turn. Incremental syncs start each stream from its resume cursor;
    backfills from None (full history)."""
    db, job, account = ctx.db, ctx.job, ctx.account
    if account is None:
        raise ValueError(f"job {job.id} ({job.kind}) needs a venue account")
    sched = get_schedule(db, account.id)
    payload = dict(job.payload or {})
    if "streams" not in payload:
        names = list(streams if streams is not None else PAGE_RUNNERS)
        payload["streams"] = names
        payload["cursor"] = _start_cursor(sched, names[0], incremental) if names else None
        payload["pages"] = 0
        _commit_owned_payload(ctx, payload)
    connector = ctx.connector()
    pages_this_run = 0
    while payload["streams"]:
        stream = payload["streams"][0]
        runner = PAGE_RUNNERS.get(stream)
        if runner is None:
            raise ValueError(f"no page runner registered for stream {stream!r}")
        next_cursor, resume_cursor = runner(db, account, connector, payload["cursor"])
        pages_this_run += 1
        payload = {**payload, "pages": payload.get("pages", 0) + 1}
        if next_cursor is None:
            cursors = dict(sched.resume_cursors or {})
            cursors[stream] = resume_cursor
            sched.resume_cursors = cursors
            sched.updated_at = ctx.clock()
            rest = payload["streams"][1:]
            payload["streams"] = rest
            payload["cursor"] = _start_cursor(sched, rest[0], incremental) if rest else None
        else:
            payload["cursor"] = next_cursor
        # This page's rows + the advanced cursor + a renewed lease, atomically,
        # and only while this worker still owns the job: a worker whose lease
        # was reaped must not commit a page or move the new owner's cursor.
        _commit_owned_payload(ctx, payload)
        if payload["streams"] and pages_this_run >= ctx.settings.worker_backfill_pages_per_run:
            raise Defer(0, f"yielding after {pages_this_run} pages")


# ---- the runner -------------------------------------------------------------------------


def make_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class JobRunner:
    """Claims jobs and runs them through HANDLERS. Safe to run in several
    processes against one Postgres (claims use SKIP LOCKED; every state
    change is conditional on this worker still owning the lease)."""

    def __init__(
        self,
        session_factory: sessionmaker,
        limiter: VenueLimiter | None,
        *,
        worker_id: str | None = None,
        settings: Settings | None = None,
        clock: Callable[[], datetime] = utcnow,
        connector_factory: Callable[[VenueAccount], VenueConnector] | None = None,
        kinds: Iterable[str] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.limiter = limiter
        self.worker_id = worker_id or make_worker_id()
        self.settings = settings or get_settings()
        self.clock = clock
        self.connector_factory = connector_factory
        self.kinds = list(kinds) if kinds is not None else None

    def _kinds(self) -> list[str]:
        return self.kinds if self.kinds is not None else list(HANDLERS)

    def run_once(self) -> int:
        """Reap expired leases, claim a batch, run it. Returns jobs run."""
        with self.session_factory() as db:
            reap_expired(db, settings=self.settings, now=self.clock())
            jobs = claim(
                db,
                self.worker_id,
                limit=self.settings.worker_claim_size,
                lease_seconds=self.settings.worker_lease_seconds,
                kinds=self._kinds(),
                now=self.clock(),
            )
            ids = [j.id for j in jobs]
        for job_id in ids:
            self.run_job(job_id)
        return len(ids)

    def loop(self, should_stop: Callable[[], bool] = lambda: False) -> None:
        while not should_stop():
            try:
                ran = self.run_once()
            except Exception:
                logger.exception("claim loop error")
                ran = 0
            if not ran:
                time.sleep(self.settings.worker_poll_seconds)

    def _defer(self, db: Session, job_id: int, seconds: float, why: str) -> str:
        db.rollback()
        # Jitter by job id so a whole venue's deferred jobs do not wake at once.
        spread = job_id % 7 if seconds >= 10 else 0
        run_after = self.clock() + timedelta(seconds=max(0.0, seconds) + spread)
        reschedule(db, job_id, self.worker_id, run_after, now=self.clock())
        logger.info("job %s deferred %.1fs: %s", job_id, seconds, why)
        return "deferred"

    def run_job(self, job_id: int) -> str:
        """Run one claimed job; returns "done" | "deferred" | "failed" |
        "dead" | "lost" (the lease was taken over)."""
        with self.session_factory() as db:
            job = db.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING or job.locked_by != self.worker_id:
                return "lost"
            handler = HANDLERS.get(job.kind)
            account = db.get(VenueAccount, job.venue_account_id) if job.venue_account_id else None
            venue = account.venue if account is not None else None
            if handler is None:
                fail(db, job_id, self.worker_id, f"no handler registered for {job.kind!r}")
                return "failed"
            # The batch's lease started at claim time; jobs later in the batch
            # may have waited behind slow ones. Renew it before starting so a
            # reaper never counts a failed attempt against a job that has not
            # run yet (or runs it twice); if it was already reaped, stop here.
            if not heartbeat(
                db, job_id, self.worker_id, self.settings.worker_lease_seconds, now=self.clock()
            ):
                return "lost"
            if account is not None and account.status == VenueAccountStatus.DISCONNECTED:
                complete(db, job_id, self.worker_id, now=self.clock())
                return "done"
            if venue and self.limiter is not None:
                open_until = self.limiter.breaker_open_until(venue)
                if open_until is not None:
                    wait = (open_until - self.clock()).total_seconds()
                    return self._defer(db, job_id, wait, f"{venue} circuit open")
            ctx = JobContext(
                db=db,
                job=job,
                account=account,
                worker_id=self.worker_id,
                settings=self.settings,
                limiter=self.limiter,
                clock=self.clock,
                connector_factory=self.connector_factory,
            )
            try:
                handler(ctx)
            except LeaseLost:
                db.rollback()
                logger.warning("job %s: lease lost mid-run; the new owner continues", job_id)
                return "lost"
            except (Defer, BudgetExhausted) as exc:
                return self._defer(db, job_id, exc.retry_after_seconds, str(exc))
            except RateLimited as exc:
                return self._rate_limited(db, job_id, venue, ctx.credential_id, exc)
            except Exception as exc:
                limited = rate_limit_from_http(exc)
                if limited is not None:
                    return self._rate_limited(db, job_id, venue, ctx.credential_id, limited)
                db.rollback()
                if venue and self.limiter is not None and is_venue_outage(exc):
                    open_until = self.limiter.record_failure(venue)
                    if open_until is not None:
                        wait = (open_until - self.clock()).total_seconds()
                        return self._defer(db, job_id, wait, f"{venue} circuit opened: {exc}")
                logger.warning("job %s (%s) failed: %s", job_id, job.kind, _error_text(exc))
                status = fail(
                    db, job_id, self.worker_id, exc, settings=self.settings, now=self.clock()
                )
                return "dead" if status == JobStatus.DEAD else "failed"
            complete(db, job_id, self.worker_id, now=self.clock())
            if venue and self.limiter is not None:
                self.limiter.record_success(venue)
            return "done"

    def _rate_limited(
        self,
        db: Session,
        job_id: int,
        venue: str | None,
        credential_id: str | None,
        exc: RateLimited,
    ) -> str:
        if venue and self.limiter is not None:
            self.limiter.on_rate_limited(
                venue, credential_id, exc.retry_after_seconds, exc.endpoint
            )
        return self._defer(db, job_id, exc.retry_after_seconds, str(exc))
