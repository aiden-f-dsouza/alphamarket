"""Streaming ingestion (implementation_plan P7): the optional `stream` process.

    uv run python -m alphamarket.stream

Polling every few minutes means paper copies lag their creators by minutes.
This process holds a private websocket per creator WITH ACTIVE PAPER
ALLOCATIONS on a venue that can stream (VenueProfile.stream_mode), so a fill
reaches the ledger in seconds and the copy lag (signal observed_at -
filled_at) drops accordingly.

The poller stays the source of truth. A stream only shortens lag; a dropped,
late or wrong stream changes nothing about the ledger:

- StreamMode.FILLS (Hyperliquid userFills): frames carry the REST fill
  objects, normalized by the same connector code and written through
  ingest.ingest_asset_fills, so a fill seen by both the stream and the poller
  is stored once (unique venue ids, event-granularity duplicate check). The
  stream never touches the poller's resume cursors, so its watermark is the
  poller's own and a missed frame is fetched on the next poll.
- StreamMode.NUDGE (Kraken executions, Coinbase user): the frame only says
  "this account traded"; the account's `sync` job is queued at once and the
  REST path ingests the fill.

After new fills are ingested the account's `signals` job is ENQUEUED (never
run inline) through jobs.enqueue_fresh: a burst coalesces into one pending
job, and a fill committed while a signals job is already running gets a
follow-up job instead of being missed. The worker's claim loop picks it up
within WORKER_POLL_SECONDS at copy-lag priority; signals then queue mirror.

Exclusivity: each streamed account is a Postgres lease (jobs.acquire_lease,
key "stream:account:<id>"), renewed every STREAM_REFRESH_SECONDS, so any
number of stream processes never double-subscribe an account; a dead
process's accounts free up after STREAM_LEASE_SECONDS. Venues with a per-IP
cap on streamed users (Hyperliquid: 10) also take one of `cap` slot leases
per egress IP ("stream:slot:<venue>:<egress_ip>:<n>"), most-followed
creators first; a better-ranked creator left without a slot makes the
holder of the worst-ranked one give its slot up at the next refresh.

Safety: credentials are decrypted only through ingest.connector_for ->
credentials.open_credential (audited, purpose "stream") on every (re)connect;
nothing sent or received is logged, and every error string goes through
redaction.redact. Reconnects use full-jitter exponential backoff and wait
out the venue's open circuit (venue_limits); REST calls a stream makes (a
Kraken websocket token, Hyperliquid metadata) spend the same outbound
budgets as the worker. A key the venue will not let stream (Kraken without
the WebSocket permission) is left to the poller for a cool-off.
"""

import asyncio
import contextlib
import logging
import random
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from alphamarket import ingest, redaction
from alphamarket.audit import job_context
from alphamarket.config import Settings, get_settings
from alphamarket.connectors.base import (
    BudgetExhausted,
    NormalizedAssetFill,
    RateLimited,
    StreamEvent,
    StreamingSource,
    StreamMode,
    StreamProtocolError,
    StreamUnavailable,
    VenueConnector,
)
from alphamarket.connectors.registry import get_profile, streaming_venues
from alphamarket.jobs import (
    INACTIVE_ACCOUNT_STATUSES,
    PRIORITY_SYNC_FOLLOWED,
    JobKind,
    acquire_lease,
    enqueue_fresh,
    live_leases,
    make_worker_id,
    release_lease,
    renew_lease,
)
from alphamarket.models import (
    AccountSchedule,
    Allocation,
    AllocationStatus,
    AssetFill,
    Follow,
    Job,
    JobStatus,
    Strategy,
    VenueAccount,
)
from alphamarket.redaction import redact
from alphamarket.venue_limits import VenueLimiter, credential_ref, is_venue_outage

logger = logging.getLogger(__name__)

ACCOUNT_LEASE_PREFIX = "stream:account:"
SLOT_LEASE_PREFIX = "stream:slot:"
CONNECT_TIMEOUT_SECONDS = 15.0


def account_lease_key(account_id: int) -> str:
    return f"{ACCOUNT_LEASE_PREFIX}{account_id}"


def slot_lease_prefix(venue: str, egress_ip: str) -> str:
    return f"{SLOT_LEASE_PREFIX}{venue}:{egress_ip}:"


def backoff_delay(attempt: int, base: float, cap: float, rng: random.Random) -> float:
    """Full jitter: uniform in [0, min(cap, base x 2^attempt)], so a venue
    outage does not make every stream reconnect in the same second."""
    return rng.uniform(0.0, min(cap, base * 2 ** max(0, attempt)))


# ---- who to stream ---------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    account_id: int
    venue: str
    allocations: int  # active paper allocations of the creator's strategy
    follows: int


def streamable_accounts(db: Session) -> list[Candidate]:
    """Creators on a streaming venue with at least one ACTIVE paper
    allocation, most-followed first (allocations, then follows, then id).
    Accounts that are disconnected, suspended or pending are left out, and
    so are accounts whose history backfill has not finished.

    Watermark rule for StreamMode.FILLS: a streamed row must never move the
    point the poller resumes from. The poller resumes from its stored
    resume cursor; only without one does it fall back to the ledger's newest
    fill (ingest.sync_since), which a streamed fill WOULD advance past fills
    the poller has not fetched yet. So FILLS accounts stream only once the
    poller has stored an asset-fills resume cursor."""
    venues = streaming_venues()
    if not venues:
        return []
    allocs = (
        select(Strategy.venue_account_id.label("account_id"), func.count(Allocation.id).label("n"))
        .join(Allocation, Allocation.strategy_id == Strategy.id)
        .where(Allocation.status == AllocationStatus.ACTIVE)
        .group_by(Strategy.venue_account_id)
        .subquery()
    )
    follows = (
        select(Strategy.venue_account_id.label("account_id"), func.count(Follow.id).label("n"))
        .join(Follow, Follow.strategy_id == Strategy.id)
        .group_by(Strategy.venue_account_id)
        .subquery()
    )
    follow_n = func.coalesce(follows.c.n, 0)
    rows = db.execute(
        select(
            VenueAccount.id,
            VenueAccount.venue,
            allocs.c.n,
            follow_n,
            AccountSchedule.resume_cursors,
        )
        .join(allocs, allocs.c.account_id == VenueAccount.id)
        .outerjoin(follows, follows.c.account_id == VenueAccount.id)
        .join(AccountSchedule, AccountSchedule.venue_account_id == VenueAccount.id)
        .where(
            VenueAccount.venue.in_(list(venues)),
            VenueAccount.status.not_in(INACTIVE_ACCOUNT_STATUSES),
            AccountSchedule.backfill_done_at.is_not(None),
        )
        .order_by(allocs.c.n.desc(), follow_n.desc(), VenueAccount.id)
    ).all()
    return [
        Candidate(int(a), str(v), int(n), int(f))
        for a, v, n, f, cursors in rows
        if venues[v].stream_mode is not StreamMode.FILLS
        or (cursors or {}).get(ingest.STREAM_ASSET_FILLS)
    ]


# ---- what a frame does --------------------------------------------------------------


def sync_running(db: Session, account_id: int) -> bool:
    return bool(
        db.scalar(
            select(Job.id)
            .where(
                Job.venue_account_id == account_id,
                Job.kind.in_([JobKind.SYNC, JobKind.BACKFILL]),
                Job.status == JobStatus.RUNNING,
            )
            .limit(1)
        )
    )


def nudge_sync(db: Session, account_id: int, *, now: datetime | None = None) -> str:
    """Queue the account's sync at copy-lag priority so the poller ingests
    what the stream just saw. Coalesces with a pending sync. Commits."""
    _, outcome = enqueue_fresh(
        db, JobKind.SYNC, account_id=account_id, priority=PRIORITY_SYNC_FOLLOWED, now=now
    )
    db.commit()
    return outcome


def ingest_streamed_fills(
    db: Session, account_id: int, fills: list[NormalizedAssetFill], *, now: datetime | None = None
) -> ingest.LedgerWriteResult:
    """Write streamed fills through the poller's idempotent path, then queue
    (never run) the account's signals job when anything was new. Commits.

    A fill already stored is skipped WITHOUT the exact-duplicate comparison:
    the poller is the source of truth and the only side that may quarantine
    an account over a differing copy, so a wrong frame can never quarantine
    a creator by itself."""
    account = db.get(VenueAccount, account_id)
    if account is None or account.status in INACTIVE_ACCOUNT_STATUSES:
        return ingest.LedgerWriteResult()

    def write() -> ingest.LedgerWriteResult:
        stored = set(
            db.scalars(
                select(AssetFill.venue_fill_id).where(
                    AssetFill.venue_account_id == account_id,
                    AssetFill.venue_fill_id.in_([f.venue_fill_id for f in fills]),
                )
            )
        )
        result = ingest.ingest_asset_fills(
            db, account, [f for f in fills if f.venue_fill_id not in stored]
        )
        result.duplicates += sum(1 for f in fills if f.venue_fill_id in stored)
        db.commit()
        return result

    try:
        result = write()
    except IntegrityError:
        # The poller committed the same fill between our check and our
        # insert; the second pass sees it stored and skips it.
        db.rollback()
        account = db.get(VenueAccount, account_id)
        result = write()
    if result.new:
        enqueue_fresh(
            db, JobKind.SIGNALS, account_id=account_id, priority=PRIORITY_SYNC_FOLLOWED, now=now
        )
        db.commit()
    return result


# ---- transport ----------------------------------------------------------------------


class Connection(Protocol):
    async def send(self, frame: str) -> None: ...

    async def recv(self) -> str | bytes:
        """The next frame; raises when the connection is closed."""
        ...

    async def close(self) -> None: ...


class Transport(Protocol):
    async def connect(self, url: str) -> Connection: ...


class WebsocketsTransport:
    """The real transport (the `websockets` library). Protocol-level pings
    keep NAT paths open; venues that want application pings get them from
    StreamingSource.ping_frame."""

    def __init__(self, open_timeout: float = CONNECT_TIMEOUT_SECONDS) -> None:
        self.open_timeout = open_timeout

    async def connect(self, url: str) -> Connection:
        from websockets.asyncio.client import connect

        ws = await connect(
            url,
            open_timeout=self.open_timeout,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 2**20,  # a Hyperliquid snapshot of recent fills can be large
            user_agent_header="koine-stream",
        )
        return _WebsocketConnection(ws)


class _WebsocketConnection:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def send(self, frame: str) -> None:
        await self._ws.send(frame)

    async def recv(self) -> str | bytes:
        return await self._ws.recv()

    async def close(self) -> None:
        await self._ws.close()


# ---- one account's stream -------------------------------------------------------------


def _default_connector_factory(db: Session, account: VenueAccount) -> VenueConnector:
    return ingest.connector_for(account, db, purpose="stream")


ConnectorFactory = Callable[[Session, VenueAccount], VenueConnector]


class AccountStream:
    """Connect, subscribe, read, reconnect: until cancelled, or until the
    venue says this account cannot stream (then `unavailable` says why)."""

    def __init__(self, sup: "StreamSupervisor", account_id: int, venue: str) -> None:
        self.sup = sup
        self.account_id = account_id
        self.venue = venue
        self.mode = get_profile(venue).stream_mode
        self.connects = 0  # successful subscriptions so far
        self.unavailable: str | None = None
        self.events = 0  # events handled (tests, logs)
        self._got_frame = False

    # -- the loop --

    async def run(self) -> None:
        attempt = 0
        while True:
            wait = await asyncio.to_thread(self.sup.breaker_wait, self.venue)
            if wait > 0:
                await self.sup.sleep(wait + self.sup.rng.uniform(0.0, 5.0))
                continue
            delay: float | None = None
            self._got_frame = False
            try:
                source = await asyncio.to_thread(self._open_source)
                await self._session(source)
            except asyncio.CancelledError:
                raise
            except StreamUnavailable as exc:
                self.unavailable = redact(str(exc))
                logger.info("account %s: not streamed: %s", self.account_id, self.unavailable)
                return
            except (BudgetExhausted, RateLimited) as exc:
                if isinstance(exc, RateLimited):
                    await asyncio.to_thread(self.sup.rate_limited, self.venue, exc)
                delay = exc.retry_after_seconds
                logger.info("account %s stream waits %.1fs: %s", self.account_id, delay, exc)
            except Exception as exc:  # noqa: BLE001  (any failure: back off and reconnect)
                if is_venue_outage(exc):
                    await asyncio.to_thread(self.sup.record_failure, self.venue)
                logger.warning(
                    "account %s %s stream dropped: %s",
                    self.account_id,
                    self.venue,
                    redact(f"{type(exc).__name__}: {exc}")[:300],
                )
            if self._got_frame:
                attempt = 0
            if delay is None:
                delay = backoff_delay(
                    attempt,
                    self.sup.settings.stream_backoff_base_seconds,
                    self.sup.settings.stream_backoff_cap_seconds,
                    self.sup.rng,
                )
                attempt += 1
            await self.sup.sleep(delay)

    def _open_source(self) -> StreamingSource:
        """Build the connector from the stored credential (audited decrypt,
        purpose "stream"), attach the outbound throttle, and prepare the
        source (e.g. fetch a websocket token). Runs in a thread."""
        with self.sup.session_factory() as db, job_context("stream"):
            account = db.get(VenueAccount, self.account_id)
            if account is None or account.status in INACTIVE_ACCOUNT_STATUSES:
                raise StreamUnavailable(f"venue account {self.account_id} is not active")
            connector = self.sup.connector_factory(db, account)
            if self.sup.limiter is not None:
                cred = credential_ref(account.venue, account.key_id)
                connector.set_throttle(self.sup.limiter.throttle(account.venue, cred))
            source = connector.stream_source()
            if source is None:
                raise StreamUnavailable(f"{account.venue} offers no stream for this account")
            source.prepare()
            return source

    async def _session(self, source: StreamingSource) -> None:
        conn = await asyncio.wait_for(
            self.sup.transport.connect(source.url()), timeout=CONNECT_TIMEOUT_SECONDS
        )
        try:
            for frame in source.subscribe_frames():
                await conn.send(frame)  # may hold a token: never logged
            self.connects += 1
            if self.connects > 1 and self.mode is StreamMode.NUDGE:
                # Fills during the gap were never pushed: one catch-up sync.
                await asyncio.to_thread(self._nudge, "reconnect")
            logger.info("account %s: %s stream subscribed", self.account_id, self.venue)
            await self._read(conn, source)
        finally:
            with contextlib.suppress(Exception):  # closing a broken socket may fail too
                await conn.close()

    async def _read(self, conn: Connection, source: StreamingSource) -> None:
        loop = asyncio.get_running_loop()
        last_frame = last_ping = loop.time()
        while True:
            now = loop.time()
            ping_at = last_ping + source.ping_interval_seconds
            idle_at = last_frame + source.idle_timeout_seconds
            if now >= idle_at:
                raise StreamProtocolError(f"no frame for {source.idle_timeout_seconds:.0f}s")
            if now >= ping_at:
                ping = source.ping_frame()
                if ping is not None:
                    await conn.send(ping)
                last_ping = now
                continue
            try:
                frame = await asyncio.wait_for(conn.recv(), timeout=min(ping_at, idle_at) - now)
            except TimeoutError:
                continue
            last_frame = loop.time()
            events = await asyncio.to_thread(self._parse, source, frame)
            self._got_frame = True
            for event in events:
                await asyncio.to_thread(self._handle, event)

    # -- frames --

    def _parse(self, source: StreamingSource, frame: str | bytes) -> list[StreamEvent]:
        """source.parse, except that a fill the source cannot normalize right
        now (out of budget for metadata, an unknown coin) becomes a nudge:
        the poller will fetch and normalize it, nothing is dropped."""
        try:
            return source.parse(frame)
        except (StreamProtocolError, StreamUnavailable):
            raise
        except (BudgetExhausted, RateLimited) as exc:
            logger.info("account %s: frame left to the poller: %s", self.account_id, exc)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "account %s: unparsed %s frame left to the poller: %s",
                self.account_id,
                self.venue,
                redact(f"{type(exc).__name__}: {exc}")[:300],
            )
        return [StreamEvent(nudge=True, ref="unparsed")]

    def _handle(self, event: StreamEvent) -> None:
        self.events += 1
        if self.mode is StreamMode.FILLS and event.fills:
            self._ingest(event.fills)
        elif event.nudge or event.fills:
            self._nudge(event.ref)

    def _ingest(self, fills: list[NormalizedAssetFill]) -> None:
        with self.sup.session_factory() as db:
            if sync_running(db, self.account_id):
                # The poller is ingesting this account right now; racing its
                # insert would fail one side. Let a follow-up sync take it.
                nudge_sync(db, self.account_id)
                return
            try:
                result = ingest_streamed_fills(db, self.account_id, fills)
            except Exception as exc:  # noqa: BLE001  (the poller still has it)
                db.rollback()
                logger.warning(
                    "account %s: streamed fills left to the poller: %s",
                    self.account_id,
                    redact(f"{type(exc).__name__}: {exc}")[:300],
                )
                nudge_sync(db, self.account_id)
                return
        if result.new:
            now = datetime.now(UTC)
            lags = [(now - f.filled_at).total_seconds() for f in fills]
            logger.info(
                "account %s: +%d streamed fills (%d already stored), lag %.1fs",
                self.account_id,
                result.new,
                result.duplicates,
                min(lags),
            )

    def _nudge(self, ref: str | None) -> None:
        with self.sup.session_factory() as db:
            outcome = nudge_sync(db, self.account_id)
        logger.info(
            "account %s: %s activity (%s): sync %s", self.account_id, self.venue, ref, outcome
        )


# ---- the supervisor -----------------------------------------------------------------


class StreamSupervisor:
    """Decides which accounts this process streams (leases), and runs one
    AccountStream task per account."""

    def __init__(
        self,
        session_factory: sessionmaker,
        limiter: VenueLimiter | None = None,
        *,
        transport: Transport | None = None,
        settings: Settings | None = None,
        holder: str | None = None,
        connector_factory: ConnectorFactory | None = None,
        rng: random.Random | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session_factory = session_factory
        self.limiter = limiter
        self.transport: Transport = transport or WebsocketsTransport()
        self.settings = settings or get_settings()
        self.holder = holder or make_worker_id()
        self.connector_factory = connector_factory or _default_connector_factory
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.monotonic = monotonic
        self.streams: dict[int, tuple[AccountStream, asyncio.Task]] = {}
        self.slots: dict[int, str] = {}  # account id -> slot lease key
        self.cooloff: dict[int, float] = {}  # account id -> monotonic time it may retry

    # -- limiter hooks (run in threads) --

    def breaker_wait(self, venue: str) -> float:
        if self.limiter is None:
            return 0.0
        until = self.limiter.breaker_open_until(venue)
        return 0.0 if until is None else max(0.0, (until - datetime.now(UTC)).total_seconds())

    def rate_limited(self, venue: str, exc: RateLimited) -> None:
        if self.limiter is not None:
            self.limiter.on_rate_limited(venue, None, exc.retry_after_seconds, exc.endpoint)

    def record_failure(self, venue: str) -> None:
        if self.limiter is not None:
            self.limiter.record_failure(venue)

    # -- leases --

    def _ttl(self) -> int:
        return self.settings.stream_lease_seconds

    def _take_slot(self, db: Session, venue: str, cap: int, account_id: int) -> str | None:
        prefix = slot_lease_prefix(venue, self.settings.egress_ip)
        taken = live_leases(db, prefix)
        mine = set(self.slots.values())
        for n in range(cap):
            key = f"{prefix}{n}"
            if key in taken or key in mine:
                continue
            if acquire_lease(db, key, self.holder, self._ttl(), payload={"account_id": account_id}):
                return key
        return None

    def _release(self, db: Session, account_id: int) -> None:
        slot = self.slots.pop(account_id, None)
        if slot is not None:
            release_lease(db, slot, self.holder)
        release_lease(db, account_lease_key(account_id), self.holder)

    def plan(self, finished: dict[int, str | None]) -> tuple[list[Candidate], list[int]]:
        """One pass over the leases (runs in a thread): release the accounts
        whose streams ended (`finished`: id -> unavailable reason), renew
        the rest, drop the ones no longer wanted, take new ones. Returns
        (candidates to start, account ids to stop)."""
        mono = self.monotonic()
        for account_id, reason in finished.items():
            if reason is not None:
                self.cooloff[account_id] = mono + self.settings.stream_unavailable_cooloff_seconds
        self.cooloff = {a: t for a, t in self.cooloff.items() if t > mono}
        ttl = self._ttl()
        start: list[Candidate] = []
        stop: list[int] = []
        with self.session_factory() as db:
            for account_id in finished:
                self._release(db, account_id)
            candidates = streamable_accounts(db)
            rank = {c.account_id: i for i, c in enumerate(candidates)}
            running = [a for a in self.streams if a not in finished]
            for account_id in running:
                slot = self.slots.get(account_id)
                kept = (
                    account_id in rank
                    and account_id not in self.cooloff
                    and renew_lease(db, account_lease_key(account_id), self.holder, ttl)
                    and (slot is None or renew_lease(db, slot, self.holder, ttl))
                )
                if not kept:
                    self._release(db, account_id)
                    stop.append(account_id)
            held = set(running) - set(stop)
            starved: dict[str, int] = {}  # venue -> best rank left without a slot
            for c in candidates:
                if len(held) >= self.settings.stream_max_accounts:
                    break
                if c.account_id in held or c.account_id in self.cooloff:
                    continue
                key = account_lease_key(c.account_id)
                payload = {"venue": c.venue, "egress_ip": self.settings.egress_ip}
                if not acquire_lease(db, key, self.holder, ttl, payload=payload):
                    continue  # another process streams it
                cap = get_profile(c.venue).stream_users_per_ip
                if cap is not None:
                    slot = self._take_slot(db, c.venue, cap, c.account_id)
                    if slot is None:
                        release_lease(db, key, self.holder)
                        starved.setdefault(c.venue, rank[c.account_id])
                        continue
                    self.slots[c.account_id] = slot
                held.add(c.account_id)
                start.append(c)
            # A better-ranked creator has no slot on this IP: give up the
            # worst-ranked capped account this process holds (one per pass);
            # the next pass, here or elsewhere, takes the freed slot for it.
            for venue, best_starved in starved.items():
                mine = [a for a in held if a in self.slots and _venue_of(candidates, a) == venue]
                worst = max(mine, key=lambda a: rank[a], default=None)
                if worst is not None and rank[worst] > best_starved:
                    logger.info(
                        "releasing %s account %s: a more-followed creator needs its slot",
                        venue,
                        worst,
                    )
                    self._release(db, worst)
                    held.discard(worst)
                    if worst in self.streams:
                        stop.append(worst)
                    start = [c for c in start if c.account_id != worst]
        return start, stop

    # -- tasks --

    async def refresh(self) -> None:
        finished: dict[int, str | None] = {}
        for account_id, (stream, task) in list(self.streams.items()):
            if task.done():
                if not task.cancelled() and task.exception() is not None:
                    logger.error(
                        "account %s stream task crashed: %s",
                        account_id,
                        redact(repr(task.exception()))[:300],
                    )
                finished[account_id] = stream.unavailable or (
                    "crashed" if not task.cancelled() and task.exception() else None
                )
                del self.streams[account_id]
        start, stop = await asyncio.to_thread(self.plan, finished)
        for account_id in stop:
            entry = self.streams.pop(account_id, None)
            if entry is not None:
                entry[1].cancel()
                await asyncio.gather(entry[1], return_exceptions=True)
        for c in start:
            stream = AccountStream(self, c.account_id, c.venue)
            task = asyncio.create_task(stream.run(), name=f"stream-{c.account_id}")
            self.streams[c.account_id] = (stream, task)
        if start or stop:
            logger.info(
                "streaming %d accounts (+%d, -%d)", len(self.streams), len(start), len(stop)
            )

    async def shutdown(self) -> None:
        """Stop every stream and release every lease, so another process can
        take the accounts at once instead of after the lease runs out."""
        tasks = [task for _, task in self.streams.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        ids = list(self.streams)
        self.streams.clear()

        def _release_all() -> None:
            with self.session_factory() as db:
                for account_id in ids:
                    self._release(db, account_id)

        await asyncio.to_thread(_release_all)

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                try:
                    await self.refresh()
                except Exception:
                    logger.exception("stream refresh failed")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop.wait(), timeout=self.settings.stream_refresh_seconds
                    )
        finally:
            await self.shutdown()


def _venue_of(candidates: list[Candidate], account_id: int) -> str | None:
    for c in candidates:
        if c.account_id == account_id:
            return c.venue
    return None


# ---- entry point ----------------------------------------------------------------------


async def _serve(supervisor: StreamSupervisor) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await supervisor.run(stop)


def main() -> None:
    from alphamarket.db import SessionLocal, engine
    from alphamarket.venue_limits import build_limiter

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    redaction.install()
    # The websockets library logs frames at DEBUG; a subscribe frame holds a
    # token. Never let that level through, whatever the root level is.
    logging.getLogger("websockets").setLevel(logging.INFO)
    settings = get_settings()
    supervisor = StreamSupervisor(SessionLocal, build_limiter(settings, engine), settings=settings)
    logger.info(
        "stream %s started: venues %s, refresh %ss, egress %s",
        supervisor.holder,
        ", ".join(sorted(streaming_venues())),
        settings.stream_refresh_seconds,
        settings.egress_ip,
    )
    asyncio.run(_serve(supervisor))


if __name__ == "__main__":
    main()
