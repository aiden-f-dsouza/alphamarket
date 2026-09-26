"""PostgresLimiter against real Postgres: the ON CONFLICT upsert counts
correctly, including under concurrent hits from several threads (the way
several web processes share one counter)."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from alphamarket.ratelimit import PostgresLimiter, prune_counters

pytestmark = pytest.mark.pg


def _at(seconds: float) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


def test_upsert_counts_and_resets(pg_session_factory):
    limiter = PostgresLimiter(pg_session_factory)
    # 2_000_000_050 / 60 = 33_333_334.17 -> window [2_000_000_040, 2_000_000_100)
    t = 2_000_000_050
    counts = [limiter.hit("pg:k", 2, 60, _at(t)).count for _ in range(3)]
    assert counts == [1, 2, 3]
    over = limiter.hit("pg:k", 2, 60, _at(t))
    assert not over.allowed and over.retry_after == 50  # 2_000_000_100 - 2_000_000_050
    assert limiter.hit("pg:k", 2, 60, _at(2_000_000_100)).count == 1


def test_concurrent_hits_are_all_counted(pg_session_factory):
    limiter = PostgresLimiter(pg_session_factory)
    now = _at(2_000_001_000)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: limiter.hit("pg:race", 10, 60, now).count, range(40)))
    assert sorted(results) == list(range(1, 41))  # no lost update, no duplicate count
    with pg_session_factory() as db:
        assert db.scalar(text("SELECT count FROM rate_limit_counters WHERE key='pg:race'")) == 40
        assert prune_counters(db, _at(2_000_002_000)) >= 1
