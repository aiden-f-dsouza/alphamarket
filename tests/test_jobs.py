"""Job queue (P2): dedupe, priorities, leases, backoff, dead-lettering,
deferrals that never count an attempt, jitter, resumable backfills, and the
circuit breaker deferring a venue's jobs. SQLite; the SKIP LOCKED claim race
is in test_jobs_pg.py."""

import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from alphamarket import jobs, worker
from alphamarket.connectors import registry
from alphamarket.connectors.base import (
    AssetVenueConnector,
    CredentialKind,
    InstrumentKind,
    NormalizedBalances,
    OwnershipProof,
    Page,
    RateBudget,
    RateLimited,
    RateScope,
    VenueProfile,
)
from alphamarket.jobs import (
    PRIORITY_BACKFILL,
    PRIORITY_MAINTENANCE,
    PRIORITY_SYNC,
    PRIORITY_SYNC_FOLLOWED,
    JobKind,
    JobRunner,
    backoff_seconds,
    claim,
    complete,
    enqueue,
    enqueue_due_syncs,
    fail,
    jitter_offset_seconds,
    next_sync_slot,
    reap_expired,
    reschedule,
)
from alphamarket.models import (
    AccountSchedule,
    Follow,
    Job,
    JobStatus,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.venue_limits import MemoryBuckets, VenueLimiter

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

KRAKEN_BUDGET = RateBudget(
    scope=RateScope.CREDENTIAL, capacity=15, refill_per_minute=20, costs=(("TradesHistory", 2),)
)


class Clock:
    """One clock for the queue (datetimes) and the limiter (epoch ms)."""

    def __init__(self, at: datetime = T0) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def ms(self) -> int:
        return int(self.at.timestamp() * 1000)

    def advance(self, seconds: float) -> None:
        self.at += timedelta(seconds=seconds)


@pytest.fixture()
def db(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def fake_venue(monkeypatch):
    """A paged (non-binary) venue with a Kraken-shaped budget, registered
    the way a real venue is (registry.PROFILES)."""
    profile = VenueProfile(
        venue="fakex",
        display_name="FakeX",
        envs=("prod",),
        credential=CredentialKind.KEY_SECRET,
        key_label="key",
        secret_label="secret",
        key_help="",
        balance_observable=True,
        instrument=InstrumentKind.ASSET,
        tick_micros=1,
        contract_step_micros=1,
        fee_rate_bps=0,
        fee_rounding_micros=1,
        ownership=OwnershipProof.KEY,
        rate_budgets=(KRAKEN_BUDGET,),
    )
    monkeypatch.setitem(registry.PROFILES, "fakex", profile)
    return profile


@pytest.fixture()
def handlers(monkeypatch):
    """Isolate handler and page-runner registrations per test."""
    monkeypatch.setattr(jobs, "HANDLERS", dict(jobs.HANDLERS))
    monkeypatch.setattr(jobs, "PAGE_RUNNERS", {})
    return jobs.HANDLERS


def make_account(db, venue="kalshi", key_id="key-1", followed=False) -> VenueAccount:
    user = User(email=f"{key_id}@example.com", password_hash="x", display_name=key_id)
    db.add(user)
    db.flush()
    account = VenueAccount(user_id=user.id, venue=venue, venue_env="prod", key_id=key_id)
    db.add(account)
    db.flush()
    strategy = Strategy(
        user_id=user.id, venue_account_id=account.id, name=key_id, slug=f"s-{key_id}"
    )
    db.add(strategy)
    db.flush()
    if followed:
        db.add(Follow(user_id=user.id, strategy_id=strategy.id))
    db.commit()
    return account


def runner(session_factory, clock, limiter=None, **kw) -> JobRunner:
    return JobRunner(session_factory, limiter, worker_id="w1", clock=clock, **kw)


def get_job(session_factory, job_id) -> Job:
    with session_factory() as s:
        return s.get(Job, job_id)


# ---- formulas ---------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped():
    # base 30 s: 30 x 2^(n-1) -> 30, 60, 120, 240, 480; capped at 3600.
    assert [backoff_seconds(n, 30, 3600) for n in (1, 2, 3, 4, 5)] == [30, 60, 120, 240, 480]
    assert backoff_seconds(8, 30, 3600) == 3600  # 30 x 128 = 3840 -> cap 3600


def test_jitter_is_deterministic_and_spread():
    # Same value on every process and restart: sha256, never Python's salted hash().
    expected = int.from_bytes(hashlib.sha256(b"koine-sync:42").digest()[:8], "big") % 300
    assert jitter_offset_seconds(42, 300) == expected == jitter_offset_seconds(42, 300)
    offsets = [jitter_offset_seconds(i, 300) for i in range(1, 1001)]
    assert all(0 <= o < 300 for o in offsets)
    # 1000 accounts over ten 30-second windows: none empty, none holding a stampede.
    per_window = [sum(1 for o in offsets if w * 30 <= o < (w + 1) * 30) for w in range(10)]
    assert min(per_window) > 50 and max(per_window) < 150


def test_next_sync_slot_by_hand():
    # interval 300, offset 17, now = epoch 1,000,000:
    # k = (1,000,000 - 17) // 300 = 999,983 // 300 = 3333 (3333 x 300 = 999,900)
    # slot = (3333 + 1) x 300 + 17 = 1,000,217.
    now = datetime.fromtimestamp(1_000_000, UTC)
    assert next_sync_slot(7, now, 300, offset=17).timestamp() == 1_000_217
    # Exactly on the slot: the next one is a full interval later (strictly after now).
    on_slot = datetime.fromtimestamp(1_000_217, UTC)
    assert next_sync_slot(7, on_slot, 300, offset=17).timestamp() == 1_000_517
    # Default offset is the account's jitter.
    slot = next_sync_slot(42, now, 300)
    assert slot > now and (int(slot.timestamp()) - jitter_offset_seconds(42, 300)) % 300 == 0


# ---- enqueue / claim ------------------------------------------------------------------------


def test_enqueue_dedupes_while_active(db, session_factory, clock):
    acct = make_account(db)
    first = enqueue(db, JobKind.SYNC, account_id=acct.id, now=clock())
    db.commit()
    assert first is not None
    assert enqueue(db, JobKind.SYNC, account_id=acct.id, now=clock()) is None  # pending
    other_kind = enqueue(db, JobKind.RECONCILE, account_id=acct.id, now=clock())
    assert other_kind is not None  # the key is kind + account
    db.commit()
    [claimed] = claim(db, "w1", limit=1, lease_seconds=60, kinds=["sync"], now=clock())
    assert enqueue(db, JobKind.SYNC, account_id=acct.id, now=clock()) is None  # running
    db.commit()
    assert complete(db, claimed.id, "w1", now=clock())
    again = enqueue(db, JobKind.SYNC, account_id=acct.id, now=clock())  # done: free again
    db.commit()
    assert again is not None and again != first
    assert db.scalar(select(Job).where(Job.id == again)).dedupe_key == f"sync:{acct.id}"


def test_enqueue_raises_priority_but_never_pulls_a_deferral_forward(db, clock):
    acct = make_account(db)
    later = clock() + timedelta(minutes=10)
    job_id = enqueue(db, JobKind.SYNC, account_id=acct.id, run_after=later, now=clock())
    db.commit()
    assert enqueue(db, JobKind.SYNC, account_id=acct.id, priority=10, now=clock()) is None
    db.commit()
    job = db.get(Job, job_id, populate_existing=True)
    assert job.priority == 10
    assert jobs.aware(job.run_after) == later


def test_enqueue_rejects_unknown_kinds(db):
    with pytest.raises(ValueError):
        enqueue(db, "teleport")


def test_claim_orders_by_priority_then_run_after(db, clock):
    creator = make_account(db, key_id="creator", followed=True)
    plain = make_account(db, key_id="plain")
    # One running job per account (claim), so each job gets its own account.
    fresh = make_account(db, key_id="fresh")
    nightly = make_account(db, key_id="nightly")
    enqueue(db, JobKind.BACKFILL, account_id=fresh.id, now=clock())
    enqueue(db, JobKind.RECONCILE, account_id=nightly.id, now=clock())
    for acct in (plain, creator):
        prio = jobs.sync_priority(db, acct.id)
        enqueue(db, JobKind.SYNC, account_id=acct.id, priority=prio, now=clock())
    enqueue(db, JobKind.STATS, run_after=clock() - timedelta(minutes=5), now=clock())
    enqueue(db, JobKind.PRICES, run_after=clock() + timedelta(seconds=1), now=clock())  # not due
    db.commit()
    got = claim(db, "w1", limit=10, lease_seconds=60, now=clock() + timedelta(seconds=0.5))
    assert [(j.kind, j.priority) for j in got] == [
        ("sync", PRIORITY_SYNC_FOLLOWED),  # creator with followers first
        ("sync", PRIORITY_SYNC),
        ("stats", PRIORITY_MAINTENANCE),  # older run_after wins within a priority
        ("reconcile", PRIORITY_MAINTENANCE),
        ("backfill", PRIORITY_BACKFILL),
    ]
    assert all(j.status == JobStatus.RUNNING and j.locked_by == "w1" for j in got)


def test_claim_only_takes_kinds_the_worker_handles(db, clock):
    enqueue(db, JobKind.KEY_ROTATE, now=clock())
    enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    got = claim(db, "w1", limit=10, lease_seconds=60, kinds=["prices"], now=clock())
    assert [j.kind for j in got] == ["prices"]


def test_reschedule_does_not_count_an_attempt(db, session_factory, clock):
    job_id = enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    claim(db, "w1", limit=1, lease_seconds=60, now=clock())
    assert reschedule(db, job_id, "w1", clock() + timedelta(seconds=30), now=clock())
    job = get_job(session_factory, job_id)
    assert (job.status, job.attempts, job.deferrals) == ("pending", 0, 1)
    assert jobs.aware(job.run_after) == clock() + timedelta(seconds=30)
    assert job.locked_by is None and job.lease_until is None


def test_state_changes_need_the_lease(db, session_factory, clock):
    job_id = enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    claim(db, "w1", limit=1, lease_seconds=60, now=clock())
    assert not complete(db, job_id, "w2", now=clock())
    assert fail(db, job_id, "w2", "nope", now=clock()) is None
    assert get_job(session_factory, job_id).status == "running"


def test_fail_backs_off_exponentially(db, session_factory, clock):
    job_id = enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    for attempt, delay in ((1, 30), (2, 60), (3, 120)):
        [job] = claim(db, "w1", limit=1, lease_seconds=60, now=clock())
        assert fail(db, job.id, "w1", ValueError("venue said no"), now=clock()) == "pending"
        job = get_job(session_factory, job_id)
        assert job.attempts == attempt
        assert jobs.aware(job.run_after) == clock() + timedelta(seconds=delay)
        assert job.last_error == "ValueError: venue said no"
        clock.advance(delay)


def test_expired_lease_is_reaped_as_one_failed_attempt(db, session_factory, clock):
    job_id = enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    claim(db, "w1", limit=1, lease_seconds=300, now=clock())
    clock.advance(299)
    assert reap_expired(db, now=clock()) == 0
    clock.advance(2)
    assert reap_expired(db, now=clock()) == 1
    job = get_job(session_factory, job_id)
    assert (job.status, job.attempts, job.locked_by) == ("pending", 1, None)
    assert "lease expired" in job.last_error


def test_heartbeat_extends_the_lease(db, session_factory, clock):
    job_id = enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    claim(db, "w1", limit=1, lease_seconds=300, now=clock())
    clock.advance(200)
    assert jobs.heartbeat(db, job_id, "w1", 300, now=clock())
    clock.advance(200)  # 400 s after the claim, 200 s after the heartbeat
    assert reap_expired(db, now=clock()) == 0
    assert not jobs.heartbeat(db, job_id, "w2", 300, now=clock())


# ---- dead letter ------------------------------------------------------------------------------


def test_dead_letter_sets_the_account_error_with_a_reason(
    db, session_factory, clock, handlers, monkeypatch
):
    acct = make_account(db)

    def always_fails(ctx):
        raise PermissionError("key revoked")

    handlers[JobKind.SCOPE_RECHECK] = always_fails
    job_id = enqueue(db, JobKind.SCOPE_RECHECK, account_id=acct.id, max_attempts=3, now=clock())
    db.commit()
    r = runner(session_factory, clock)
    assert r.run_once() == 1  # attempt 1 -> retry in 30 s
    clock.advance(30)
    assert r.run_once() == 1  # attempt 2 -> retry in 60 s
    assert r.run_once() == 0  # not due yet
    clock.advance(60)
    assert r.run_once() == 1  # attempt 3 = max_attempts -> dead
    job = get_job(session_factory, job_id)
    assert (job.status, job.attempts) == ("dead", 3)
    with session_factory() as s:
        account = s.get(VenueAccount, acct.id)
        assert account.status == VenueAccountStatus.ERROR
        assert account.last_sync_error.startswith(
            "Re-checking that your key is read-only failed 3 times in a row"
        )
        assert "PermissionError: key revoked" in account.last_sync_error
        sched = s.get(AccountSchedule, acct.id)
        # Cool-off: the next scheduled sync is an hour out, not the next tick.
        assert jobs.aware(sched.next_sync_at) == clock() + timedelta(seconds=3600)
    assert r.run_once() == 0  # dead jobs are never claimed


# ---- scheduling -------------------------------------------------------------------------------


def test_enqueue_due_syncs_uses_followers_and_jitter(db, session_factory, clock):
    creator = make_account(db, key_id="creator", followed=True)
    plain = make_account(db, key_id="plain")
    gone = make_account(db, key_id="gone")
    gone.status = VenueAccountStatus.DISCONNECTED
    db.commit()
    assert enqueue_due_syncs(db, interval_seconds=300, now=clock()) == 2
    rows = {j.venue_account_id: j for j in db.scalars(select(Job)).all()}
    assert rows[creator.id].priority == PRIORITY_SYNC_FOLLOWED
    assert rows[plain.id].priority == PRIORITY_SYNC
    assert gone.id not in rows
    sched = db.get(AccountSchedule, plain.id)
    assert jobs.aware(sched.next_sync_at) == next_sync_slot(plain.id, clock(), 300)
    # A second tick before the slot queues nothing (and a pending job would dedupe anyway).
    assert enqueue_due_syncs(db, interval_seconds=300, now=clock() + timedelta(seconds=1)) == 0


def test_ticks_enqueue_idempotently(session_factory, db, monkeypatch):
    acct = make_account(db)  # kalshi: reconcilable
    worker.tick_syncs(session_factory)
    worker.tick_syncs(session_factory)
    worker.tick_nightly(session_factory)
    worker.tick_nightly(session_factory)  # a second process ticking the same night
    with session_factory() as s:
        kinds = sorted(j.kind for j in s.scalars(select(Job)).all())
    assert kinds == [
        "housekeeping",  # wave2
        "mirror",
        "prices",
        "reconcile",
        "scope_recheck",
        "stats",
        "sync",
    ]
    with session_factory() as s:
        [rec] = s.scalars(select(Job).where(Job.kind == "reconcile")).all()
        assert rec.venue_account_id == acct.id


# ---- handlers and the runner --------------------------------------------------------------------


class FakePagedConnector(AssetVenueConnector):
    """Pages c0 -> c1 -> ... -> c{n-1}; each page is one TradesHistory call.
    `rate_limit_at` makes the venue answer 429 once for that cursor."""

    venue = "fakex"

    def __init__(self, n_pages: int, rate_limit_at: str | None = None) -> None:
        self.n_pages = n_pages
        self.rate_limit_at = rate_limit_at
        self.calls: list[str | None] = []

    def check_write_scope(self):
        return False

    def fetch_balances(self) -> NormalizedBalances:
        raise NotImplementedError

    def fetch_asset_fills_page(self, cursor=None, since=None) -> Page:
        self.throttle("TradesHistory")
        self.calls.append(cursor)
        if cursor is not None and cursor == self.rate_limit_at:
            self.rate_limit_at = None
            raise RateLimited(30.0, "TradesHistory")
        i = 0 if cursor is None else int(cursor[1:])
        last = i == self.n_pages - 1
        return Page(
            items=[f"fill-{i}"],
            next_cursor=None if last else f"c{i + 1}",
            resume_cursor="resume-after-last" if last else None,
        )


def page_runner(ingested: list):
    def run(db, account, connector, cursor):
        page = connector.fetch_asset_fills_page(cursor)
        ingested.extend(page.items)
        return page.next_cursor, page.resume_cursor

    return run


def test_429_backs_off_and_resumes_from_the_cursor(
    db, session_factory, clock, fake_venue, handlers
):
    acct = make_account(db, venue="fakex")
    conn = FakePagedConnector(n_pages=3, rate_limit_at="c1")
    ingested: list[str] = []
    jobs.register_page_runner("asset_fills", page_runner(ingested))
    limiter = VenueLimiter(MemoryBuckets(), clock_ms=clock.ms)
    r = runner(session_factory, clock, limiter, connector_factory=lambda a: conn)
    job_id = enqueue(db, JobKind.BACKFILL, account_id=acct.id, now=clock())
    db.commit()

    assert r.run_once() == 1
    job = get_job(session_factory, job_id)
    # Page c0 landed and its cursor was committed; c1 hit the 429.
    assert (job.status, job.attempts, job.deferrals) == ("pending", 0, 1)
    assert job.payload["cursor"] == "c1" and job.payload["streams"] == ["asset_fills"]
    assert ingested == ["fill-0"]
    # Retry-After 30 s honoured (+ up to 6 s of per-job spread).
    wait = (jobs.aware(job.run_after) - clock()).total_seconds()
    assert 30 <= wait < 37
    # The bucket is now penalised: refill halved for 10 minutes.
    spec = limiter.buckets("fakex", jobs.credential_ref("fakex", acct.key_id))[0][1]
    assert limiter.backend.state(spec, clock.ms()).penalty_until_ms == clock.ms() + 600_000

    assert r.run_once() == 0  # not due yet
    clock.advance(40)
    assert r.run_once() == 1
    job = get_job(session_factory, job_id)
    assert job.status == "done" and job.attempts == 0
    assert conn.calls == [None, "c1", "c1", "c2"]  # resumed at c1, never restarted
    assert ingested == ["fill-0", "fill-1", "fill-2"]
    with session_factory() as s:
        sched = s.get(AccountSchedule, acct.id)
        assert sched.resume_cursors == {"asset_fills": "resume-after-last"}
        assert sched.backfill_done_at is not None


def test_backfill_out_of_budget_requeues_until_done(
    db, session_factory, clock, fake_venue, handlers
):
    acct = make_account(db, venue="fakex")
    conn = FakePagedConnector(n_pages=10)
    ingested: list[str] = []
    jobs.register_page_runner("asset_fills", page_runner(ingested))
    limiter = VenueLimiter(MemoryBuckets(), clock_ms=clock.ms)
    r = runner(session_factory, clock, limiter, connector_factory=lambda a: conn)
    job_id = enqueue(db, JobKind.BACKFILL, account_id=acct.id, now=clock())
    db.commit()
    # 15 tokens / 2 per page = 7 pages, then 1000 milli left: the 8th page's
    # before() raises BudgetExhausted(3.0 s) and nothing is sent.
    r.run_once()
    job = get_job(session_factory, job_id)
    assert len(ingested) == 7 and job.payload["cursor"] == "c7"
    assert (job.status, job.attempts) == ("pending", 0)
    assert jobs.aware(job.run_after) == clock() + timedelta(seconds=3)
    # Every 9 s restores 3 tokens (9000 * 20 // 60 = 3000 milli): +1 page per round
    # after the first (1000 + 3000 = 4000 -> 2 pages, then 0 + 3000 -> 1 page, ...).
    rounds = 0
    while get_job(session_factory, job_id).status != "done":
        clock.advance(9)
        r.run_once()
        rounds += 1
        assert rounds < 10
    assert ingested == [f"fill-{i}" for i in range(10)]
    assert get_job(session_factory, job_id).attempts == 0  # never an error


def test_backfill_yields_after_the_page_limit(
    db, session_factory, clock, fake_venue, handlers, monkeypatch
):
    acct = make_account(db, venue="fakex")
    conn = FakePagedConnector(n_pages=5)
    ingested: list[str] = []
    jobs.register_page_runner("asset_fills", page_runner(ingested))
    r = runner(session_factory, clock, None, connector_factory=lambda a: conn)
    monkeypatch.setattr(r.settings, "worker_backfill_pages_per_run", 2)
    job_id = enqueue(db, JobKind.BACKFILL, account_id=acct.id, now=clock())
    db.commit()
    r.run_once()
    assert ingested == ["fill-0", "fill-1"]
    assert get_job(session_factory, job_id).status == "pending"
    r.run_once()
    r.run_once()
    assert ingested == [f"fill-{i}" for i in range(5)]
    assert get_job(session_factory, job_id).status == "done"


def test_incremental_sync_starts_from_the_resume_cursor(
    db, session_factory, clock, fake_venue, handlers
):
    acct = make_account(db, venue="fakex")
    db.add(AccountSchedule(venue_account_id=acct.id, resume_cursors={"asset_fills": "c2"}))
    db.commit()
    conn = FakePagedConnector(n_pages=4)
    ingested: list[str] = []
    jobs.register_page_runner("asset_fills", page_runner(ingested))
    r = runner(session_factory, clock, None, connector_factory=lambda a: conn)
    enqueue(db, JobKind.SYNC, account_id=acct.id, now=clock())
    db.commit()
    r.run_once()
    assert conn.calls == ["c2", "c3"]
    with session_factory() as s:
        account = s.get(VenueAccount, acct.id)
        assert account.status == "active" and account.last_synced_at is not None
        kinds = sorted(j.kind for j in s.scalars(select(Job)).all())
    assert kinds == ["signals", "sync"]  # a sync queues its signals job


def test_circuit_breaker_defers_a_venues_jobs_instead_of_failing(
    db, session_factory, clock, handlers
):
    accounts = [make_account(db, key_id=f"k{i}") for i in range(6)]
    state = {"up": False, "calls": 0}

    def venue_call(ctx):
        state["calls"] += 1
        if not state["up"]:
            raise httpx.ConnectTimeout("venue down")

    handlers[JobKind.SCOPE_RECHECK] = venue_call
    ids = [
        enqueue(db, JobKind.SCOPE_RECHECK, account_id=a.id, max_attempts=3, now=clock())
        for a in accounts
    ]
    db.commit()
    limiter = VenueLimiter(MemoryBuckets(), clock_ms=clock.ms)
    r = runner(session_factory, clock, limiter)
    outcomes = [r.run_job(j.id) for j in claim_all(session_factory, clock)]
    # Four failures count attempts; the fifth opens the circuit and is deferred;
    # the sixth never calls the venue.
    assert outcomes == ["failed"] * 4 + ["deferred", "deferred"]
    assert state["calls"] == 5
    states = [get_job(session_factory, i) for i in ids]
    assert [j.attempts for j in states] == [1, 1, 1, 1, 0, 0]
    opened = limiter.breaker_open_until("kalshi")
    assert opened == clock() + timedelta(minutes=2)
    # Still open a minute later: the four retries (backoff 30 s) are due, and
    # every one defers without calling the venue; none fails or dead-letters.
    clock.advance(60)
    assert [r.run_job(j.id) for j in claim_all(session_factory, clock)] == ["deferred"] * 4
    assert state["calls"] == 5
    assert [get_job(session_factory, i).attempts for i in ids] == [1, 1, 1, 1, 0, 0]
    # After 2 minutes (+ spread) the venue is back: jobs run and the circuit resets.
    clock.advance(3600)
    state["up"] = True
    assert set(r.run_job(j.id) for j in claim_all(session_factory, clock)) == {"done"}
    assert all(get_job(session_factory, i).status == "done" for i in ids)
    assert limiter.breaker_open_until("kalshi") is None
    assert [get_job(session_factory, i).attempts for i in ids] == [1, 1, 1, 1, 0, 0]


def claim_all(session_factory, clock):
    with session_factory() as s:
        return claim(s, "w1", limit=100, lease_seconds=300, now=clock())


def test_binary_sync_deferral_leaves_the_account_healthy(
    db, session_factory, clock, handlers, monkeypatch
):
    acct = make_account(db)  # kalshi: the M7 sync_account path

    def limited(db_, account):
        account.status = VenueAccountStatus.ERROR  # what sync_account records on failure
        account.last_sync_error = "RateLimited: ..."
        db_.commit()
        raise RateLimited(12.0, "portfolio/fills")

    monkeypatch.setattr(worker, "sync_account", limited)
    limiter = VenueLimiter(MemoryBuckets(), clock_ms=clock.ms)
    job_id = enqueue(db, JobKind.SYNC, account_id=acct.id, now=clock())
    db.commit()
    runner(session_factory, clock, limiter).run_once()
    job = get_job(session_factory, job_id)
    assert (job.status, job.attempts, job.deferrals) == ("pending", 0, 1)
    with session_factory() as s:
        account = s.get(VenueAccount, acct.id)
        assert (account.status, account.last_sync_error) == ("active", None)


def test_stats_waits_for_reconciles(db, session_factory, clock, handlers, monkeypatch):
    acct = make_account(db)
    rec = enqueue(db, JobKind.RECONCILE, account_id=acct.id, now=clock())
    stats = enqueue(db, JobKind.STATS, now=clock())
    db.commit()
    monkeypatch.setattr(worker, "compute_strategy_stats", lambda db_, s: None)
    r = runner(session_factory, clock, None, kinds=["stats"])
    r.run_once()
    assert get_job(session_factory, stats).status == "pending"  # deferred behind reconcile
    with session_factory() as s:
        s.get(Job, rec).status = JobStatus.DONE
        s.commit()
    clock.advance(60 + 7)  # the barrier's poll interval + the per-job spread
    r.run_once()
    assert get_job(session_factory, stats).status == "done"


def test_disconnected_account_jobs_are_dropped(db, session_factory, clock, handlers):
    acct = make_account(db)
    called = []
    handlers[JobKind.SCOPE_RECHECK] = lambda ctx: called.append(ctx)
    job_id = enqueue(db, JobKind.SCOPE_RECHECK, account_id=acct.id, now=clock())
    acct.status = VenueAccountStatus.DISCONNECTED
    db.commit()
    runner(session_factory, clock).run_once()
    assert called == [] and get_job(session_factory, job_id).status == "done"


def test_register_handler_decorator(handlers):
    @jobs.register_handler(JobKind.KEY_ROTATE)
    def rotate(ctx):
        return None

    assert handlers[JobKind.KEY_ROTATE] is rotate


# ---- review fixes: lease ownership, prices budget handling --------------------------------------


def test_lease_lost_mid_page_commits_neither_the_page_nor_the_cursor(
    db, session_factory, clock, fake_venue, handlers
):
    acct = make_account(db, venue="fakex")
    conn = FakePagedConnector(n_pages=3)
    job_id = enqueue(db, JobKind.BACKFILL, account_id=acct.id, now=clock())
    db.commit()

    def run(db_, account, connector, cursor):
        page = connector.fetch_asset_fills_page(cursor)
        account.last_sync_error = f"rows of page {cursor}"  # this page's (uncommitted) writes
        if cursor == "c1":
            # Meanwhile the lease ran out and another worker took the job over.
            with session_factory() as other:
                other.get(Job, job_id).locked_by = "w2"
                other.commit()
        return page.next_cursor, page.resume_cursor

    jobs.register_page_runner("asset_fills", run)
    r = runner(session_factory, clock, None, connector_factory=lambda a: conn)
    assert r.run_once() == 1
    job = get_job(session_factory, job_id)
    # Page c0 and its cursor were committed while w1 owned the job; page c1
    # was fetched after the takeover, so neither its rows nor cursor c2 landed.
    assert (job.status, job.locked_by, job.attempts) == ("running", "w2", 0)
    assert job.payload["cursor"] == "c1"
    with session_factory() as s:
        assert s.get(VenueAccount, acct.id).last_sync_error == "rows of page None"


def test_each_job_renews_its_lease_when_it_starts(db, session_factory, clock, handlers):
    ran = []
    handlers[JobKind.SCOPE_RECHECK] = lambda ctx: ran.append(ctx.job.id)
    a, b = (make_account(db, key_id=k) for k in ("a", "b"))
    first = enqueue(db, JobKind.SCOPE_RECHECK, account_id=a.id, now=clock())
    second = enqueue(db, JobKind.SCOPE_RECHECK, account_id=b.id, now=clock())
    db.commit()
    claim_all(session_factory, clock)  # lease until T0 + 300 s for both
    r = runner(session_factory, clock)
    # The batch waited behind a slow job: the second job starts 400 s after the
    # claim, with its lease already expired but not yet reaped. Starting it
    # renews the lease to now + 300 s, so a reaper cannot count it as failed.
    clock.advance(400)
    seen = {}

    def check_lease(ctx):
        seen["lease"] = jobs.aware(get_job(session_factory, ctx.job.id).lease_until)
        ran.append(ctx.job.id)

    handlers[JobKind.SCOPE_RECHECK] = check_lease
    assert r.run_job(second) == "done"
    assert seen["lease"] == clock() + timedelta(seconds=300)
    # A job reaped before its turn is not run at all (it is back to pending).
    with session_factory() as s:
        assert reap_expired(s, now=clock()) == 1  # `first`: lease T0 + 300 < T0 + 400
    assert r.run_job(first) == "lost"
    assert ran == [second]
    assert get_job(session_factory, first).status == "pending"


def test_prices_stop_at_budget_keep_quotes_and_wait_for_the_next_tick(
    db, session_factory, clock, handlers, monkeypatch
):
    from alphamarket.connectors.base import MarketDataSource, NormalizedMarket
    from alphamarket.models import MarketPrice

    class FakeQuotes(MarketDataSource):
        venue = "fakex"

        def __init__(self) -> None:
            self.limited_at: str | None = None
            self.sent: list[str] = []

        def fetch_market(self, ticker):
            self.throttle("market")
            self.sent.append(ticker)
            if ticker == self.limited_at:
                raise RateLimited(20.0, "market")
            return NormalizedMarket(
                ticker=ticker,
                status="active",
                yes_bid_micros=400_000,
                yes_ask_micros=420_000,
                last_price_micros=410_000,
                as_of=clock(),
            )

    tickers = {("fakex", "prod"): {"A", "B", "C", "D"}}
    monkeypatch.setattr(worker, "open_tickers_by_venue", lambda db_: tickers)
    source = FakeQuotes()
    monkeypatch.setattr(worker, "build_market_data", lambda v, e: source)
    ip_budget = RateBudget(scope=RateScope.IP, capacity=2, refill_per_minute=60)
    limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda v: (ip_budget,), clock_ms=clock.ms)
    job_id = enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    runner(session_factory, clock, limiter).run_once()
    # 2 tokens: A and B are fetched and stored; C's before() refuses, nothing is
    # sent for C or D, and the job completes (no early re-run on the shared bucket).
    assert source.sent == ["A", "B"]
    job = get_job(session_factory, job_id)
    assert (job.status, job.attempts, job.deferrals) == ("done", 0, 0)
    with session_factory() as s:
        assert sorted(s.scalars(select(MarketPrice.ticker)).all()) == ["A", "B"]

    # A 429 stops the venue at once (no requests for the remaining tickers) and
    # penalises the IP bucket: blocked until Retry-After, 20 s = 20,000 ms.
    clock.advance(60)  # 60 s x 60/min = 60 tokens gained, capped at capacity 2
    source.sent, source.limited_at = [], "A"
    enqueue(db, JobKind.PRICES, now=clock())
    db.commit()
    runner(session_factory, clock, limiter).run_once()
    assert source.sent == ["A"]
    spec = limiter.buckets("fakex", None)[0][1]
    assert limiter.backend.state(spec, clock.ms()).blocked_until_ms == clock.ms() + 20_000


def test_one_running_job_per_account(session_factory, db):
    # A sync and a backfill of the same account never run together; a job for
    # another account (and an account-less job) is still claimed.
    a = make_account(db, key_id="a")
    b = make_account(db, key_id="b")
    jobs.enqueue(db, JobKind.SYNC, account_id=a.id)
    jobs.enqueue(db, JobKind.BACKFILL, account_id=a.id)
    jobs.enqueue(db, JobKind.SYNC, account_id=b.id)
    jobs.enqueue(db, JobKind.PRICES)
    db.commit()
    first = jobs.claim(db, "w1", limit=10, lease_seconds=60)
    assert sorted((j.kind, j.venue_account_id) for j in first) == sorted(
        [("sync", a.id), ("sync", b.id), ("prices", None)]
    )
    # While a's sync runs, its backfill is not claimable.
    assert jobs.claim(db, "w2", limit=10, lease_seconds=60) == []
