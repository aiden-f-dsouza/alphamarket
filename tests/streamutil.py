"""Shared fakes and seeding for the P7 stream tests (test_stream.py and
test_stream_pg.py): a fake websocket transport, recorded sleeps, connector
factories over the captured fixtures, and a creator-with-followers seeder.
No network anywhere (invariant 6)."""

import asyncio
import json
import random
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import func, select

from alphamarket import worker  # noqa: F401  (registers the job handlers)
from alphamarket.config import get_settings
from alphamarket.connectors import hyperliquid as hl
from alphamarket.connectors.kraken import KrakenConnector
from alphamarket.ingest import STREAM_ASSET_FILLS
from alphamarket.models import (
    AccountSchedule,
    Allocation,
    AllocationStatus,
    Follow,
    Job,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
)
from alphamarket.stream import StreamSupervisor

STREAMS = Path(__file__).parent / "fixtures" / "streams"
HL_FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"
ADDRESS = "0x1a13afd75418fd72dfa80da33cbca489e231caec"
OTHER = "0x178d245216beb3d7e3e64dce8494b1efe04a9266"
HL_CURSOR = '{"f":{"s":0,"x":[]}}'  # a stored poller cursor (FILLS-mode prerequisite)
KRAKEN_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
)
WS_TOKEN = "WW91ciBhdXRoZW50aWNhdGlvbiB0b2tlbiBnb2VzIGhlcmUu"


def frame(name: str, key: str) -> str:
    return json.dumps(json.loads((STREAMS / name).read_text())[key])


def hl_fx(name: str):
    return json.loads((HL_FIXTURES / f"{name}_2026-09-26.json").read_text())


CLOSE = object()


class FakeConn:
    def __init__(self, url: str) -> None:
        self.url = url
        self.sent: list[str] = []
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    async def recv(self):
        item = await self.inbox.get()
        if item is CLOSE:
            raise ConnectionError("server closed the connection")
        return item

    async def close(self) -> None:
        self.closed = True


class FakeServer:
    """The Transport: hands out FakeConns; refuses the first `refuse` dials."""

    def __init__(self, refuse: int = 0) -> None:
        self.conns: list[FakeConn] = []
        self.refuse = refuse
        self.dials = 0

    async def connect(self, url: str) -> FakeConn:
        self.dials += 1
        if self.refuse:
            self.refuse -= 1
            raise OSError("connection refused")
        conn = FakeConn(url)
        self.conns.append(conn)
        return conn


class Sleeps:
    """Injected sleep: records each backoff and only yields to the loop."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


async def until(cond, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def hl_client() -> httpx.Client:
    answers = {
        "meta": hl_fx("meta"),
        "meta:xyz": hl_fx("meta_xyz"),
        "spotMeta": hl_fx("spotMeta"),
        "userFillsByTime": hl_fx("userFillsByTime"),
        "userFunding": [],
        "userNonFundingLedgerUpdates": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        key = body["type"] + (f":{body['dex']}" if body.get("dex") else "")
        return httpx.Response(200, json=answers[key])

    return httpx.Client(transport=httpx.MockTransport(handler))


def hl_factory(db, account):
    return hl.HyperliquidConnector(account.key_id, client=hl_client())


def kraken_factory(answer: dict):
    def factory(db, account):
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=answer))
        )
        return KrakenConnector(account.key_id, KRAKEN_SECRET, client=client)

    return factory


def settings(**overrides):
    return get_settings().model_copy(update=overrides)


def supervisor(sf, server, *, holder="s1", factory=hl_factory, sleep=None, limiter=None, **cfg):
    return StreamSupervisor(
        sf,
        limiter,
        transport=server,
        settings=settings(**cfg),
        holder=holder,
        connector_factory=factory,
        rng=random.Random(7),
        sleep=sleep or Sleeps(),
    )


def seed(
    sf,
    *,
    venue="hyperliquid",
    key_id=ADDRESS,
    allocations=1,
    stopped=0,
    follows=0,
    backfilled=True,
    cursor=HL_CURSOR,
    status=VenueAccountStatus.ACTIVE,
) -> int:
    tag = uuid.uuid4().hex[:10]
    with sf() as db:
        creator = User(email=f"c{tag}@example.com", password_hash="x", display_name="c")
        db.add(creator)
        db.flush()
        account = VenueAccount(
            user_id=creator.id, venue=venue, venue_env="prod", key_id=key_id, status=status
        )
        db.add(account)
        db.flush()
        strategy = Strategy(
            user_id=creator.id, venue_account_id=account.id, name="s", slug=f"s-{tag}"
        )
        db.add(strategy)
        db.flush()
        for i in range(allocations + stopped + follows):
            follower = User(email=f"f{tag}-{i}@example.com", password_hash="x", display_name="f")
            db.add(follower)
            db.flush()
            if i < allocations + stopped:
                db.add(
                    Allocation(
                        user_id=follower.id,
                        strategy_id=strategy.id,
                        allocation_micros=1_000_000_000,
                        drawdown_limit_bps=1500,
                        high_water_micros=1_000_000_000,
                        status=AllocationStatus.ACTIVE
                        if i < allocations
                        else AllocationStatus.STOPPED,
                    )
                )
            else:
                db.add(Follow(user_id=follower.id, strategy_id=strategy.id))
        db.add(
            AccountSchedule(
                venue_account_id=account.id,
                resume_cursors={STREAM_ASSET_FILLS: cursor} if cursor else {},
                backfill_done_at=datetime.now(UTC) if backfilled else None,
            )
        )
        db.commit()
        return account.id


def count(sf, model, *where) -> int:
    with sf() as db:
        return db.scalar(select(func.count()).select_from(model).where(*where))


def active_jobs_of(sf, kind: str) -> list[tuple[str, str]]:
    with sf() as db:
        return [
            (key, status)
            for key, status in db.execute(
                select(Job.dedupe_key, Job.status).where(
                    Job.kind == kind, Job.status.in_(["pending", "running"])
                )
            ).all()
        ]
