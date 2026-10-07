"""P7 leases and coalesced enqueues under real Postgres concurrency: many
threads race for one lease (fresh and expired), and two stream processes
refreshing at once split the streamable accounts without overlap."""

import asyncio
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import jobs
from alphamarket.models import Job
from streamutil import (
    WS_TOKEN,
    FakeServer,
    kraken_factory,
    seed,
    supervisor,
    until,
)

pytestmark = pytest.mark.pg


def _race(pg_session_factory, key: str, now: datetime, n: int = 8) -> list[str]:
    barrier = threading.Barrier(n)
    winners: list[str] = []
    lock = threading.Lock()

    def contender(i: int) -> None:
        with pg_session_factory() as db:
            barrier.wait()
            if jobs.acquire_lease(db, key, f"h{i}", 60, now=now):
                with lock:
                    winners.append(f"h{i}")

    threads = [threading.Thread(target=contender, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return winners


def test_exactly_one_contender_wins_a_fresh_lease(pg_session_factory):
    key = f"stream:account:test-{uuid.uuid4().hex}"
    winners = _race(pg_session_factory, key, datetime.now(UTC))
    assert len(winners) == 1
    with pg_session_factory() as db:
        rows = db.execute(select(Job.status, Job.locked_by).where(Job.dedupe_key == key)).all()
    assert rows == [("running", winners[0])]


def test_exactly_one_contender_takes_over_an_expired_lease(pg_session_factory):
    key = f"stream:account:test-{uuid.uuid4().hex}"
    t0 = datetime.now(UTC)
    with pg_session_factory() as db:
        assert jobs.acquire_lease(db, key, "dead", 10, now=t0)
    winners = _race(pg_session_factory, key, t0 + timedelta(seconds=30))
    assert len(winners) == 1
    with pg_session_factory() as db:
        assert db.scalar(select(Job.locked_by).where(Job.dedupe_key == key)) == winners[0]


def test_enqueue_fresh_coalesces_on_postgres(pg_session_factory):
    account_id = seed(pg_session_factory, venue="kraken", key_id="k", cursor=None)
    with pg_session_factory() as db:
        assert jobs.enqueue_fresh(db, "signals", account_id=account_id)[1] == "queued"
        assert jobs.enqueue_fresh(db, "signals", account_id=account_id) == (None, "coalesced")
        db.commit()
        db.scalar(select(Job).where(Job.dedupe_key == f"signals:{account_id}")).status = "running"
        db.commit()
        assert jobs.enqueue_fresh(db, "signals", account_id=account_id)[1] == "queued"
        db.commit()
        keys = set(db.scalars(select(Job.dedupe_key).where(Job.venue_account_id == account_id)))
    assert keys == {f"signals:{account_id}", f"signals:{account_id}:next"}


def test_two_stream_processes_split_accounts_without_overlap(pg_session_factory):
    mine = {
        seed(pg_session_factory, venue="kraken", key_id=f"k-{i}", cursor=None) for i in range(6)
    }
    factory = kraken_factory({"error": [], "result": {"token": WS_TOKEN, "expires": 900}})
    servers = [FakeServer(), FakeServer()]

    async def scenario():
        sups = [
            supervisor(
                pg_session_factory,
                servers[i],
                holder=f"P{i}-{uuid.uuid4().hex[:6]}",
                factory=factory,
            )
            for i in range(2)
        ]
        await asyncio.gather(*(s.refresh() for s in sups))
        held = [set(s.streams) & mine for s in sups]
        assert held[0].isdisjoint(held[1])
        assert held[0] | held[1] == mine
        await until(lambda: sum(len(s.conns) for s in servers) >= len(mine))
        for s in sups:
            await s.shutdown()

    asyncio.run(scenario())
