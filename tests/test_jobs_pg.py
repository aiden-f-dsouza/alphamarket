"""P2 exit test on real Postgres: two workers never claim the same job
(FOR UPDATE SKIP LOCKED), enqueue dedupe holds under a race (ON CONFLICT on
the partial unique index), the shared token bucket never over-grants, and
migration 0010 downgrades and re-applies cleanly."""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, func, select, text

from alphamarket.connectors.base import BudgetExhausted, RateBudget, RateScope
from alphamarket.jobs import JobKind, claim, complete, enqueue
from alphamarket.models import Job
from alphamarket.venue_limits import PostgresBuckets, VenueLimiter

pytestmark = pytest.mark.pg

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture()
def pg(pg_session_factory):
    with pg_session_factory() as s:
        s.execute(delete(Job))
        s.commit()
    yield pg_session_factory
    with pg_session_factory() as s:
        s.execute(delete(Job))
        s.commit()


def _enqueue_many(factory, n: int) -> set[int]:
    with factory() as s:
        ids = {
            enqueue(s, JobKind.PRICES, dedupe_key=f"pg-test:{i}", now=T0 - timedelta(seconds=1))
            for i in range(n)
        }
        s.commit()
    return ids


def test_two_sessions_never_claim_the_same_job(pg):
    ids = _enqueue_many(pg, 10)
    a, b = pg(), pg()
    try:
        # Worker A claims and is still inside its transaction (rows locked).
        mine_a = claim(a, "worker-a", limit=3, lease_seconds=60, now=T0, commit=False)
        # Worker B, concurrently: SKIP LOCKED passes over A's rows.
        mine_b = claim(b, "worker-b", limit=10, lease_seconds=60, now=T0)
        a.commit()
        got_a, got_b = {j.id for j in mine_a}, {j.id for j in mine_b}
        assert len(got_a) == 3 and len(got_b) == 7
        assert got_a.isdisjoint(got_b) and got_a | got_b == ids
        # A job's state changes need its own worker's lease.
        some_a = next(iter(got_a))
        assert not complete(b, some_a, "worker-b", now=T0)
        assert complete(a, some_a, "worker-a", now=T0)
    finally:
        a.close()
        b.close()


def test_concurrent_claim_loops_run_each_job_exactly_once(pg):
    ids = _enqueue_many(pg, 60)
    claimed: dict[str, list[int]] = {"w1": [], "w2": [], "w3": []}
    barrier = threading.Barrier(len(claimed))

    def work(worker_id: str) -> None:
        barrier.wait()
        with pg() as s:
            while True:
                got = claim(s, worker_id, limit=2, lease_seconds=60, now=T0)
                if not got:
                    return
                for job in got:
                    claimed[worker_id].append(job.id)
                    complete(s, job.id, worker_id, now=T0)

    threads = [threading.Thread(target=work, args=(w,)) for w in claimed]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    everything = [i for got in claimed.values() for i in got]
    assert sorted(everything) == sorted(ids)  # every job once, none twice
    with pg() as s:
        done = s.scalar(select(func.count()).select_from(Job).where(Job.status == "done"))
    assert done == 60


def test_racing_enqueues_keep_one_active_job(pg):
    barrier = threading.Barrier(8)
    results: list[int | None] = []

    def tick() -> None:
        barrier.wait()
        with pg() as s:
            results.append(enqueue(s, JobKind.STATS, dedupe_key="pg-race", now=T0))
            s.commit()

    threads = [threading.Thread(target=tick) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert sum(r is not None for r in results) == 1
    with pg() as s:
        n = s.scalar(select(func.count()).select_from(Job).where(Job.dedupe_key == "pg-race"))
    assert n == 1


def test_shared_bucket_never_over_grants(pg_url):
    """Eight threads (think: worker processes) hammer one 15-token bucket at
    a frozen clock: the single-statement refill+take grants exactly 15."""
    engine = create_engine(pg_url, pool_size=10)
    budget = RateBudget(scope=RateScope.IP, capacity=15, refill_per_minute=20)
    venue = f"v{uuid.uuid4().hex[:8]}"
    limiter = VenueLimiter(
        PostgresBuckets(engine), budgets_for=lambda v: (budget,), clock_ms=lambda: 1_000
    )
    granted: list[int] = []
    barrier = threading.Barrier(8)

    def hammer() -> None:
        barrier.wait()
        for _ in range(5):
            try:
                limiter.acquire(venue, None, "x")
                granted.append(1)
            except BudgetExhausted:
                pass

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    engine.dispose()
    assert len(granted) == 15


def test_migration_0010_downgrades_and_reapplies(pg_url):
    from alembic import command
    from alembic.config import Config

    from alphamarket.config import get_settings

    admin = create_engine(pg_url.rsplit("/", 1)[0] + "/postgres", isolation_level="AUTOCOMMIT")
    name = f"koine_m_{uuid.uuid4().hex[:10]}"
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = pg_url.rsplit("/", 1)[0] + "/" + name
    settings = get_settings()
    original = settings.database_url
    settings.database_url = url
    try:
        cfg = Config()
        cfg.set_main_option("script_location", "alembic")
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "0008")
        eng = create_engine(url)
        with eng.connect() as conn:
            tables = set(
                conn.scalars(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
            )
        eng.dispose()
        assert not {"jobs", "rate_buckets", "venue_circuits", "account_schedules"} & tables
        command.upgrade(cfg, "head")
    finally:
        settings.database_url = original
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_concurrent_mirror_passes_skip_a_locked_allocation(pg, monkeypatch):
    """The global mirror sweep and a strategy's mirror job may run at once on
    two workers; a pass must skip an allocation another pass holds, or a
    signal would be mirrored twice into append-only mirror_fills."""
    from alphamarket import worker
    from alphamarket.config import get_settings
    from alphamarket.jobs import JobContext
    from alphamarket.models import Allocation, Strategy, User, VenueAccount

    tag = uuid.uuid4().hex[:8]
    with pg() as s:
        user = User(email=f"m-{tag}@example.com", password_hash="x", display_name=tag)
        s.add(user)
        s.flush()
        acct = VenueAccount(user_id=user.id, venue="kalshi", venue_env="prod", key_id=tag)
        s.add(acct)
        s.flush()
        strat = Strategy(user_id=user.id, venue_account_id=acct.id, name=tag, slug=f"m-{tag}")
        s.add(strat)
        s.flush()
        allocs = [
            Allocation(
                user_id=user.id,
                strategy_id=strat.id,
                allocation_micros=1_000_000,
                drawdown_limit_bps=1500,
                high_water_micros=1_000_000,
            )
            for _ in range(2)
        ]
        s.add_all(allocs)
        s.commit()
        strategy_id, (held, free) = strat.id, [a.id for a in allocs]

    processed: list[int] = []

    def fake_process(db, allocation):
        processed.append(allocation.id)
        return 0, 0

    monkeypatch.setattr(worker, "process_allocation", fake_process)
    holder = pg()
    try:
        # Worker A is mid-pass on `held` (row locked, transaction open).
        holder.scalars(select(Allocation).where(Allocation.id == held).with_for_update()).one()
        with pg() as b:
            job = Job(kind="mirror", payload={"strategy_id": strategy_id})
            ctx = JobContext(
                db=b, job=job, account=None, worker_id="w-b", settings=get_settings(), limiter=None
            )
            worker.handle_mirror(ctx)  # must neither block on nor re-mirror `held`
    finally:
        holder.rollback()
        holder.close()
    assert processed == [free]
