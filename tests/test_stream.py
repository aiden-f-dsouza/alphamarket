"""P7 stream process: who is streamed, leases, reconnects, ingestion through
the poller's idempotent path, coalesced signals jobs, secrets kept out of
logs. Sockets are a fake transport (invariant 6: no network); the database is
a file SQLite so the stream's worker threads each get a connection."""

import asyncio
import dataclasses
import json
import logging
import random
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from alphamarket import jobs, stream, worker  # noqa: F401  (worker registers handlers)
from alphamarket.connectors import hyperliquid as hl
from alphamarket.connectors import registry
from alphamarket.connectors.kraken import KrakenConnector
from alphamarket.db import Base
from alphamarket.ingest import STREAM_ASSET_FILLS
from alphamarket.models import (
    AccountSchedule,
    Allocation,
    AllocationStatus,
    AssetFill,
    AuditLog,
    Job,
    JobStatus,
    LedgerEntry,
    LedgerQuarantine,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.stream import (
    StreamSupervisor,
    account_lease_key,
    backoff_delay,
    streamable_accounts,
)
from alphamarket.venue_limits import MemoryBuckets, VenueLimiter
from streamutil import (
    ADDRESS,
    CLOSE,
    HL_CURSOR,
    KRAKEN_SECRET,
    OTHER,
    WS_TOKEN,
    FakeServer,
    Sleeps,
    active_jobs_of,
    count,
    frame,
    hl_client,
    kraken_factory,
    seed,
    settings,
    supervisor,
    until,
)

# ---- fixtures and fakes ------------------------------------------------------------


@pytest.fixture()
def sf(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stream.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def _fast(dbapi_conn, _record):  # a throwaway test file: no fsync per statement
        dbapi_conn.execute("PRAGMA synchronous=OFF")

    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    engine.dispose()


# ---- pure pieces ---------------------------------------------------------------------


def test_backoff_is_full_jitter_and_capped():
    rng = random.Random(1)
    for attempt in range(12):
        bound = min(120.0, 1.0 * 2**attempt)
        for _ in range(50):
            assert 0.0 <= backoff_delay(attempt, 1.0, 120.0, rng) <= bound
    # spread, not a fixed delay: 100 draws at attempt 3 are not all equal
    assert len({round(backoff_delay(3, 1.0, 120.0, rng), 6) for _ in range(100)}) > 50


# ---- who is streamed ---------------------------------------------------------------------


def test_only_creators_with_active_allocations_on_streaming_venues_are_candidates(sf):
    top = seed(sf, allocations=3)
    tied_more_follows = seed(sf, key_id=OTHER, allocations=1, follows=2)
    tied = seed(sf, key_id="0x" + "11" * 20, allocations=1)
    seed(sf, key_id="0x" + "22" * 20, allocations=0, follows=5)  # followers, no paper copies
    seed(sf, key_id="0x" + "33" * 20, allocations=0, stopped=2)  # only stopped allocations
    seed(sf, venue="kalshi", key_id="k", allocations=4)  # polled-only venue
    seed(sf, key_id="0x" + "44" * 20, allocations=2, backfilled=False)
    seed(sf, key_id="0x" + "55" * 20, allocations=2, cursor=None)  # no poller cursor yet
    seed(sf, key_id="0x" + "66" * 20, allocations=2, status=VenueAccountStatus.SUSPENDED)
    seed(sf, key_id="0x" + "77" * 20, allocations=2, status=VenueAccountStatus.DISCONNECTED)
    kraken = seed(
        sf, venue="kraken", key_id="kk", allocations=2, cursor=None
    )  # NUDGE: no cursor needed
    with sf() as db:
        got = [(c.account_id, c.allocations, c.follows) for c in streamable_accounts(db)]
    assert got == [(top, 3, 0), (kraken, 2, 0), (tied_more_follows, 1, 2), (tied, 1, 0)]


def test_creators_always_outrank_watched_addresses(sf):
    heavy_watch = seed(sf, key_id="0x" + "a1" * 20, allocations=9, follows=9, is_watch=True)
    creator = seed(sf, key_id="0x" + "b2" * 20, allocations=1)
    light_watch = seed(sf, key_id="0x" + "c3" * 20, allocations=2, is_watch=True)
    creator_2 = seed(sf, key_id="0x" + "d4" * 20, allocations=3)
    with sf() as db:
        got = [(c.account_id, c.is_watch) for c in streamable_accounts(db)]
    assert got == [
        (creator_2, False),
        (creator, False),  # one allocation still beats a watch with nine
        (heavy_watch, True),
        (light_watch, True),
    ]


def test_no_stream_is_opened_for_an_account_without_active_followers(sf):
    seed(sf, allocations=0, follows=3)
    seed(sf, key_id=OTHER, allocations=0, stopped=1)
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await asyncio.sleep(0.05)
        assert sup.streams == {}
        await sup.shutdown()

    asyncio.run(scenario())
    assert server.dials == 0
    assert count(sf, Job, Job.kind == "lease") == 0


# ---- leases --------------------------------------------------------------------------------


def test_lease_is_exclusive_renewable_releasable_and_expires(sf):
    t0 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    with sf() as db:
        assert jobs.acquire_lease(db, "stream:account:1", "a", 60, now=t0)
        assert not jobs.acquire_lease(db, "stream:account:1", "b", 60, now=t0)
        assert jobs.acquire_lease(db, "stream:account:1", "a", 60, now=t0)  # re-acquire = extend
        assert jobs.renew_lease(db, "stream:account:1", "a", 60, now=t0 + timedelta(seconds=30))
        assert not jobs.renew_lease(db, "stream:account:1", "b", 60, now=t0)
        assert jobs.live_leases(db, "stream:account:", now=t0) == {"stream:account:1": "a"}
        # a holds until t0+90; b takes over only after that
        assert not jobs.acquire_lease(
            db, "stream:account:1", "b", 60, now=t0 + timedelta(seconds=89)
        )
        assert jobs.acquire_lease(db, "stream:account:1", "b", 60, now=t0 + timedelta(seconds=91))
        assert not jobs.renew_lease(db, "stream:account:1", "a", 60, now=t0 + timedelta(seconds=92))
        assert jobs.release_lease(db, "stream:account:1", "b", now=t0 + timedelta(seconds=93))
        assert jobs.acquire_lease(db, "stream:account:1", "a", 60, now=t0 + timedelta(seconds=94))
        # a lease row never belongs to an account (never blocks its jobs)
        assert db.scalar(select(Job.venue_account_id).where(Job.kind == "lease")) is None


def test_a_reaped_lease_is_taken_over_and_never_errors_the_account(sf):
    account_id = seed(sf)
    t0 = datetime.now(UTC)
    with sf() as db:
        assert jobs.acquire_lease(db, account_lease_key(account_id), "dead", 10, now=t0)
        # The worker's reaper treats the expired lease like a dead job.
        assert jobs.reap_expired(db, now=t0 + timedelta(seconds=20)) == 1
        assert db.scalar(select(Job.status).where(Job.kind == "lease")) == "pending"
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.ACTIVE
        assert jobs.acquire_lease(db, account_lease_key(account_id), "new", 60)
        assert db.scalar(select(Job.locked_by).where(Job.kind == "lease")) == "new"


def test_two_stream_processes_never_subscribe_the_same_account(sf):
    account_id = seed(sf)
    server_a, server_b = FakeServer(), FakeServer()

    async def scenario():
        a = supervisor(sf, server_a, holder="A")
        b = supervisor(sf, server_b, holder="B")
        await a.refresh()
        await b.refresh()
        await until(lambda: server_a.conns)
        assert list(a.streams) == [account_id]
        assert b.streams == {}
        await b.refresh()  # still held by A
        assert b.streams == {}
        await a.shutdown()  # releases its leases at once
        await b.refresh()
        await until(lambda: server_b.conns)
        assert list(b.streams) == [account_id]
        await b.shutdown()

    asyncio.run(scenario())
    assert server_b.dials == 1


# ---- ingestion, idempotency, signals ------------------------------------------------------


def test_streamed_fills_are_ingested_once_and_signals_are_queued_coalesced(sf):
    account_id = seed(sf, allocations=2)
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await until(lambda: server.conns and server.conns[0].sent)
        conn = server.conns[0]
        assert json.loads(conn.sent[0])["subscription"] == {"type": "userFills", "user": ADDRESS}
        conn.inbox.put_nowait(frame("hyperliquid_ws.json", "subscription_response"))
        conn.inbox.put_nowait(frame("hyperliquid_ws.json", "snapshot"))
        await until(lambda: count(sf, AssetFill) == 2)
        # A burst: the new fill three times, another user's fill, the snapshot again.
        for _ in range(3):
            conn.inbox.put_nowait(frame("hyperliquid_ws.json", "update"))
        conn.inbox.put_nowait(frame("hyperliquid_ws.json", "other_user"))
        conn.inbox.put_nowait(frame("hyperliquid_ws.json", "snapshot"))
        stream_ = sup.streams[account_id][0]
        await until(lambda: stream_.events >= 5)  # other_user and the ack are no events
        await sup.shutdown()
        assert conn.closed

    asyncio.run(scenario())
    assert count(sf, AssetFill) == 3
    assert count(sf, LedgerEntry, LedgerEntry.group_ref == "fill:229662172402780") == 3
    assert count(sf, LedgerQuarantine) == 0
    # One pending signals job for the whole burst; nothing ran inline.
    assert active_jobs_of(sf, "signals") == [(f"signals:{account_id}", "pending")]
    assert count(sf, Job, Job.kind == "sync") == 0
    with sf() as db:
        signals_job = db.scalar(select(Job).where(Job.kind == "signals"))
        assert signals_job.priority == jobs.PRIORITY_SYNC_FOLLOWED
        assert signals_job.venue_account_id == account_id
        # The stream never moves the poller's cursor.
        assert db.get(AccountSchedule, account_id).resume_cursors == {STREAM_ASSET_FILLS: HL_CURSOR}
    # Leases released on shutdown.
    assert count(sf, Job, Job.kind == "lease", Job.status == "running") == 0


def _poll(sf, account_id):
    """One poller sync of the account through the real job runner."""
    with sf() as db:
        jobs.enqueue(db, jobs.JobKind.SYNC, account_id=account_id)
        db.commit()
    runner = jobs.JobRunner(
        sf,
        None,
        worker_id="poller",
        connector_factory=lambda account: hl.HyperliquidConnector(
            account.key_id, client=hl_client()
        ),
        kinds=[jobs.JobKind.SYNC],
    )
    while runner.run_once():
        pass


def test_a_fill_seen_by_stream_then_poller_is_stored_once(sf):
    account_id = seed(sf)
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await until(lambda: server.conns and server.conns[0].sent)
        server.conns[0].inbox.put_nowait(frame("hyperliquid_ws.json", "snapshot"))
        server.conns[0].inbox.put_nowait(frame("hyperliquid_ws.json", "update"))
        await until(lambda: count(sf, AssetFill) == 3)
        await sup.shutdown()

    asyncio.run(scenario())
    _poll(sf, account_id)  # the poller fetches all 60 captured fills, 3 already streamed
    assert count(sf, AssetFill) == 60
    assert count(sf, LedgerQuarantine) == 0
    with sf() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.ACTIVE
        streamed = db.scalar(select(AssetFill).where(AssetFill.venue_fill_id == "229662172402780"))
        assert streamed is not None


def test_a_fill_seen_by_poller_then_stream_is_stored_once(sf):
    account_id = seed(sf)
    _poll(sf, account_id)
    assert count(sf, AssetFill) == 60
    with sf() as db:
        for job in db.scalars(select(Job).where(Job.kind == "signals")):
            job.status = JobStatus.DONE  # the poller's own signals run, done
        db.commit()
    entries = count(sf, LedgerEntry)
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await until(lambda: server.conns and server.conns[0].sent)
        server.conns[0].inbox.put_nowait(frame("hyperliquid_ws.json", "snapshot"))
        server.conns[0].inbox.put_nowait(frame("hyperliquid_ws.json", "update"))
        stream_ = sup.streams[account_id][0]
        await until(lambda: stream_.events >= 2)
        await sup.shutdown()

    asyncio.run(scenario())
    assert count(sf, AssetFill) == 60
    assert count(sf, LedgerEntry) == entries
    assert count(sf, LedgerQuarantine) == 0
    assert active_jobs_of(sf, "signals") == []  # nothing new, nothing queued


def test_enqueue_fresh_coalesces_pending_and_follows_up_a_running_job(sf):
    account_id = seed(sf)
    with sf() as db:
        first, outcome = jobs.enqueue_fresh(db, "signals", account_id=account_id)
        assert outcome == "queued"
        assert jobs.enqueue_fresh(db, "signals", account_id=account_id) == (None, "coalesced")
        db.commit()
        db.get(Job, first).status = JobStatus.RUNNING  # it may have read before our commit
        db.commit()
        follow_up, outcome = jobs.enqueue_fresh(db, "signals", account_id=account_id)
        assert outcome == "queued" and follow_up != first
        assert jobs.enqueue_fresh(db, "signals", account_id=account_id) == (None, "coalesced")
        db.get(Job, follow_up).status = JobStatus.RUNNING
        db.commit()
        assert jobs.enqueue_fresh(db, "signals", account_id=account_id) == (None, "busy")
        keys = set(db.scalars(select(Job.dedupe_key)))
    assert keys == {f"signals:{account_id}", f"signals:{account_id}:next"}


def test_a_streamed_fill_while_the_poller_syncs_becomes_a_follow_up_sync(sf):
    account_id = seed(sf)
    with sf() as db:
        jobs.enqueue(db, jobs.JobKind.SYNC, account_id=account_id)
        db.commit()
        db.scalar(select(Job).where(Job.kind == "sync")).status = JobStatus.RUNNING
        db.commit()
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await until(lambda: server.conns and server.conns[0].sent)
        server.conns[0].inbox.put_nowait(frame("hyperliquid_ws.json", "update"))
        await until(lambda: (f"sync:{account_id}:next", "pending") in active_jobs_of(sf, "sync"))
        await sup.shutdown()

    asyncio.run(scenario())
    assert count(sf, AssetFill) == 0  # left to the poller, not raced


# ---- reconnects, circuit breaker, unavailable ------------------------------------------


def test_reconnects_with_jittered_backoff_and_resets_after_a_good_session(sf):
    seed(sf)
    server = FakeServer(refuse=3)
    sleeps = Sleeps()

    async def scenario():
        sup = supervisor(sf, server, sleep=sleeps)
        await sup.refresh()
        await until(lambda: len(server.conns) == 1)
        # attempts 0, 1, 2 failed: delays within [0, 1], [0, 2], [0, 4]
        assert len(sleeps.delays) == 3
        for attempt, delay in enumerate(sleeps.delays):
            assert 0 <= delay <= 2**attempt
        conn = server.conns[0]
        await until(lambda: conn.sent)
        conn.inbox.put_nowait(frame("hyperliquid_ws.json", "pong"))
        conn.inbox.put_nowait(CLOSE)  # the server drops a healthy connection
        await until(lambda: len(server.conns) == 2)
        assert 0 <= sleeps.delays[3] <= 1  # a frame arrived: back to attempt 0
        await until(lambda: server.conns[1].sent)  # subscribed again
        assert server.conns[1].sent[0] == conn.sent[0]
        await sup.shutdown()

    asyncio.run(scenario())


def test_idle_connection_is_dropped_and_pings_are_sent(sf, monkeypatch):
    seed(sf)
    monkeypatch.setattr(hl.HyperliquidFillStream, "ping_interval_seconds", 0.05)
    monkeypatch.setattr(hl.HyperliquidFillStream, "idle_timeout_seconds", 0.3)
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await until(lambda: len(server.conns) == 2)  # silent server: reconnected
        first = server.conns[0]
        assert first.closed
        assert first.sent.count(json.dumps({"method": "ping"})) >= 2
        await sup.shutdown()

    asyncio.run(scenario())


def test_open_circuit_holds_the_connection_until_it_closes(sf):
    seed(sf)
    limiter = VenueLimiter(MemoryBuckets(), breaker_threshold=1, breaker_open_ms=60_000)
    limiter.record_failure("hyperliquid")
    assert limiter.breaker_open_until("hyperliquid") is not None
    server = FakeServer()
    sleeps = Sleeps()

    async def scenario():
        sup = supervisor(sf, server, limiter=limiter, sleep=sleeps)
        await sup.refresh()
        await until(lambda: len(sleeps.delays) >= 3)
        assert server.dials == 0
        assert all(50 <= d <= 66 for d in sleeps.delays[:3])  # ~60 s + up to 5 s spread
        await sup.shutdown()

    asyncio.run(scenario())


def test_kraken_trade_queues_a_sync_and_a_reconnect_queues_a_catch_up(sf):
    account_id = seed(sf, venue="kraken", key_id="kraken-key", cursor=None)
    server = FakeServer()
    factory = kraken_factory({"error": [], "result": {"token": WS_TOKEN, "expires": 900}})

    async def scenario():
        sup = supervisor(sf, server, factory=factory)
        await sup.refresh()
        await until(lambda: server.conns and server.conns[0].sent)
        conn = server.conns[0]
        assert json.loads(conn.sent[0])["params"]["token"] == WS_TOKEN
        assert conn.url == "wss://ws-auth.kraken.com/v2"
        conn.inbox.put_nowait(frame("kraken_ws_v2.json", "subscribe_ack"))
        conn.inbox.put_nowait(frame("kraken_ws_v2.json", "pending_new"))
        conn.inbox.put_nowait(frame("kraken_ws_v2.json", "heartbeat"))
        await until(lambda: conn.inbox.empty())
        await asyncio.sleep(0.1)
        assert active_jobs_of(sf, "sync") == []  # acks, order lifecycle, heartbeats: quiet
        conn.inbox.put_nowait(frame("kraken_ws_v2.json", "trade"))
        conn.inbox.put_nowait(frame("kraken_ws_v2.json", "trade"))  # coalesces
        await until(lambda: active_jobs_of(sf, "sync") == [(f"sync:{account_id}", "pending")])
        stream_ = sup.streams[account_id][0]
        await until(lambda: stream_.events >= 2)
        with sf() as db:  # the sync ran meanwhile
            db.scalar(select(Job).where(Job.kind == "sync")).status = JobStatus.DONE
            db.commit()
        conn.inbox.put_nowait(CLOSE)
        await until(lambda: len(server.conns) == 2 and server.conns[1].sent)
        # Fills during the gap were never pushed: a catch-up sync is queued.
        await until(lambda: active_jobs_of(sf, "sync") == [(f"sync:{account_id}", "pending")])
        await sup.shutdown()

    asyncio.run(scenario())
    assert count(sf, AssetFill) == 0  # NUDGE mode never writes fills itself
    with sf() as db:
        job = db.scalar(select(Job).where(Job.kind == "sync", Job.status == "pending"))
        assert job.priority == jobs.PRIORITY_SYNC_FOLLOWED


def test_key_without_websocket_permission_is_left_to_the_poller(sf):
    account_id = seed(sf, venue="kraken", key_id="kraken-key", cursor=None)
    server = FakeServer()
    factory = kraken_factory({"error": ["EGeneral:Permission denied"]})

    async def scenario():
        sup = supervisor(sf, server, factory=factory)
        await sup.refresh()
        stream_ = sup.streams[account_id][0]
        await until(lambda: stream_.unavailable is not None)
        await sup.refresh()  # the finished stream releases its lease, cool-off starts
        assert sup.streams == {}
        assert account_id in sup.cooloff
        await sup.refresh()  # not retried during the cool-off
        assert sup.streams == {}
        await sup.shutdown()

    asyncio.run(scenario())
    assert server.dials == 0
    assert count(sf, Job, Job.kind == "lease", Job.status == "running") == 0
    with sf() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.ACTIVE


def test_a_stream_stops_when_the_creator_loses_its_last_follower(sf):
    account_id = seed(sf)
    server = FakeServer()

    async def scenario():
        sup = supervisor(sf, server)
        await sup.refresh()
        await until(lambda: server.conns)
        with sf() as db:
            for alloc in db.scalars(select(Allocation)):
                alloc.status = AllocationStatus.STOPPED
            db.commit()
        await sup.refresh()
        assert sup.streams == {}
        assert server.conns[0].closed
        await sup.shutdown()

    asyncio.run(scenario())
    assert count(sf, Job, Job.kind == "lease", Job.status == "running") == 0
    assert account_id


# ---- the Hyperliquid per-IP user cap -------------------------------------------------------


def test_hyperliquid_cap_streams_the_most_followed_and_hands_slots_over(sf, monkeypatch):
    profile = registry.PROFILES["hyperliquid"]
    monkeypatch.setitem(
        registry.PROFILES, "hyperliquid", dataclasses.replace(profile, stream_users_per_ip=2)
    )
    a = seed(sf, key_id="0x" + "a1" * 20, allocations=3)
    b = seed(sf, key_id="0x" + "b2" * 20, allocations=2)
    c = seed(sf, key_id="0x" + "c3" * 20, allocations=1)
    server_1, server_2 = FakeServer(), FakeServer()

    async def scenario():
        p1 = supervisor(sf, server_1, holder="P1", stream_max_accounts=1)
        p2 = supervisor(sf, server_2, holder="P2")
        await p1.refresh()
        await p2.refresh()
        assert set(p1.streams) == {a}
        assert set(p2.streams) == {b}  # c: no slot left on this IP
        # c becomes the most-followed creator.
        with sf() as db:
            strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == c))
            for i in range(4):
                user = User(email=f"late{i}@example.com", password_hash="x", display_name="f")
                db.add(user)
                db.flush()
                db.add(
                    Allocation(
                        user_id=user.id,
                        strategy_id=strategy.id,
                        allocation_micros=1,
                        drawdown_limit_bps=1500,
                        high_water_micros=1,
                    )
                )
            db.commit()
        await p2.refresh()  # c is starved and outranks b: P2 gives b's slot up
        assert set(p2.streams) == set()
        await p2.refresh()  # and takes it for c
        assert set(p2.streams) == {c}
        with sf() as db:
            slots = jobs.live_leases(db, "stream:slot:hyperliquid:")
        assert len(slots) == 2
        await p1.shutdown()
        await p2.shutdown()

    asyncio.run(scenario())


def test_a_claimed_creator_takes_a_stream_slot_from_a_watch(sf, monkeypatch):
    profile = registry.PROFILES["hyperliquid"]
    monkeypatch.setitem(
        registry.PROFILES, "hyperliquid", dataclasses.replace(profile, stream_users_per_ip=2)
    )
    watch_a = seed(sf, key_id="0x" + "a1" * 20, allocations=5, is_watch=True)
    watch_b = seed(sf, key_id="0x" + "b2" * 20, allocations=4, is_watch=True)
    server = FakeServer()

    async def scenario():
        p = supervisor(sf, server, holder="P1")
        await p.refresh()
        assert set(p.streams) == {watch_a, watch_b}  # both IP slots held by watches
        # A creator with a single paper copy appears (it finished its backfill).
        creator = seed(sf, key_id="0x" + "c3" * 20, allocations=1)
        await p.refresh()  # the creator is starved and outranks the worst watch
        assert set(p.streams) == {watch_a}
        await p.refresh()  # and takes the freed slot
        assert set(p.streams) == {watch_a, creator}
        with sf() as db:
            assert len(jobs.live_leases(db, "stream:slot:hyperliquid:")) == 2
        await p.shutdown()

    asyncio.run(scenario())


# ---- the per-process cap (stream_max_accounts) ----------------------------------------------


def test_a_creator_takes_room_from_a_watch_when_the_process_is_full(sf):
    watch_a = seed(sf, key_id="0x" + "a1" * 20, allocations=5, is_watch=True)
    watch_b = seed(sf, key_id="0x" + "b2" * 20, allocations=4, is_watch=True)
    server = FakeServer()

    async def scenario():
        p = supervisor(sf, server, holder="P1", stream_max_accounts=2)
        await p.refresh()
        assert set(p.streams) == {watch_a, watch_b}  # full, IP slots to spare
        creator = seed(sf, key_id="0x" + "c3" * 20, allocations=1)
        await p.refresh()  # the cap keeps the creator out: the worst watch goes
        assert set(p.streams) == {watch_a}
        await p.refresh()  # and the creator takes the room
        assert set(p.streams) == {watch_a, creator}
        await p.refresh()  # stable: the remaining watch outranks nothing waiting
        assert set(p.streams) == {watch_a, creator}
        with sf() as db:
            assert set(jobs.live_leases(db, stream.ACCOUNT_LEASE_PREFIX)) == {
                account_lease_key(watch_a),
                account_lease_key(creator),
            }
        await p.shutdown()

    asyncio.run(scenario())


def test_the_process_cap_never_evicts_for_a_watch_or_between_creators(sf):
    creator_a = seed(sf, key_id="0x" + "a1" * 20, allocations=1)
    creator_b = seed(sf, key_id="0x" + "b2" * 20, allocations=1)
    server = FakeServer()

    async def scenario():
        p = supervisor(sf, server, holder="P1", stream_max_accounts=2)
        await p.refresh()
        assert set(p.streams) == {creator_a, creator_b}
        seed(sf, key_id="0x" + "c3" * 20, allocations=9, is_watch=True)  # a heavy watch
        seed(sf, key_id="0x" + "d4" * 20, allocations=5)  # a better-followed creator
        await p.refresh()
        await p.refresh()
        assert set(p.streams) == {creator_a, creator_b}  # no churn among creators
        await p.shutdown()

    asyncio.run(scenario())


def test_a_creator_streamed_by_another_process_evicts_nothing(sf):
    creator = seed(sf, key_id="0x" + "a1" * 20, allocations=1)
    watch = seed(sf, key_id="0x" + "b2" * 20, allocations=1, is_watch=True)
    server_1, server_2 = FakeServer(), FakeServer()

    async def scenario():
        p1 = supervisor(sf, server_1, holder="P1", stream_max_accounts=1)
        p2 = supervisor(sf, server_2, holder="P2", stream_max_accounts=1)
        await p1.refresh()
        assert set(p1.streams) == {creator}
        await p2.refresh()
        await p2.refresh()
        assert set(p2.streams) == {watch}  # P1 has the creator: P2 keeps its watch
        await p1.shutdown()
        await p2.shutdown()

    asyncio.run(scenario())


def test_a_full_process_and_full_ip_slots_free_a_watch_slot_for_a_creator(sf, monkeypatch):
    profile = registry.PROFILES["hyperliquid"]
    monkeypatch.setitem(
        registry.PROFILES, "hyperliquid", dataclasses.replace(profile, stream_users_per_ip=2)
    )
    watch_a = seed(sf, key_id="0x" + "a1" * 20, allocations=5, is_watch=True)
    watch_b = seed(sf, key_id="0x" + "b2" * 20, allocations=4, is_watch=True)
    server = FakeServer()

    async def scenario():
        p = supervisor(sf, server, holder="P1", stream_max_accounts=2)
        await p.refresh()
        assert set(p.streams) == {watch_a, watch_b}  # process and IP both full
        creator = seed(sf, key_id="0x" + "c3" * 20, allocations=1)
        await p.refresh()
        assert set(p.streams) == {watch_a}
        await p.refresh()
        assert set(p.streams) == {watch_a, creator}
        with sf() as db:
            assert len(jobs.live_leases(db, "stream:slot:hyperliquid:")) == 2
        await p.shutdown()

    asyncio.run(scenario())


# ---- secrets ------------------------------------------------------------------------------


def test_no_token_or_secret_reaches_the_logs(sf, caplog):
    seed(sf, venue="kraken", key_id="kraken-key", cursor=None)
    server = FakeServer()
    factory = kraken_factory({"error": [], "result": {"token": WS_TOKEN, "expires": 900}})
    caplog.set_level(logging.DEBUG)

    async def scenario():
        sup = supervisor(sf, server, factory=factory)
        await sup.refresh()
        await until(lambda: server.conns and server.conns[0].sent)
        conn = server.conns[0]
        conn.inbox.put_nowait(frame("kraken_ws_v2.json", "trade"))
        # An error frame that echoes the token back, then a drop.
        conn.inbox.put_nowait(json.dumps({"error": f"EAPI:Invalid token {WS_TOKEN}"}))
        await until(lambda: len(server.conns) == 2)
        await sup.shutdown()

    asyncio.run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "stream dropped" in text  # the error was logged...
    assert WS_TOKEN not in text  # ...scrubbed
    assert KRAKEN_SECRET not in text


def test_the_default_connector_decrypts_through_credentials_with_an_audit(sf):
    from alphamarket import credentials

    account_id = seed(sf, venue="kraken", key_id="kraken-key", cursor=None)
    with sf() as db:
        account = db.get(VenueAccount, account_id)
        credentials.store(db, account, KRAKEN_SECRET, kind="key_secret")
        db.commit()
    sup = StreamSupervisor(sf, None, transport=FakeServer(), settings=settings())
    with sf() as db:
        connector = sup.connector_factory(db, db.get(VenueAccount, account_id))
    assert isinstance(connector, KrakenConnector)
    with sf() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.action == "credential.decrypt")).all()
    assert [r.details.get("purpose") for r in rows] == ["stream"]
