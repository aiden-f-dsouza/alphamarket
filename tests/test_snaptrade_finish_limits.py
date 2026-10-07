"""The SnapTrade finish POST (check connection, read-only proof, anchor
balances) goes through the outbound limiter like the picker and the worker:
it spends the buckets, fails fast as "busy" and leaves the PENDING account
exactly as it was, so the user can submit the same form again. Venue traffic is
respx-mocked (invariant 6)."""

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import credentials
from alphamarket.config import get_settings
from alphamarket.connectors.base import BudgetExhausted, RateBudget, RateScope
from alphamarket.models import AnchorBalance, Strategy, VenueAccount, VenueAccountStatus
from alphamarket.venue_limits import MemoryBuckets, VenueBusy, VenueLimiter, _FailFastThrottle
from authutil import secure_account
from test_connect_wave2 import SNAP_ACCOUNT_REF, _mock_snaptrade, _snap_fixture, _start_snaptrade

BUSY = "SnapTrade is busy, try again in a minute."
FINISH_CALLS = ("accounts", "auth", "balances", "holdings", "activities")


@pytest.fixture()
def snaptrade_keys():
    settings = get_settings()
    saved = (settings.snaptrade_client_id, settings.snaptrade_consumer_key)
    settings.snaptrade_client_id = "KOINE-TEST"
    settings.snaptrade_consumer_key = "consumer-secret-key"
    yield
    settings.snaptrade_client_id, settings.snaptrade_consumer_key = saved


@pytest.fixture()
def user(client):
    resp = client.post(
        "/auth/signup",
        json={"email": "fin@example.com", "password": "hunter2hunter2", "display_name": "Fin"},
    )
    assert resp.status_code == 201
    secure_account(client, "fin@example.com")
    return resp.json()


def _limiter(capacity: int, refill_per_minute: int) -> VenueLimiter:
    """One IP-scoped bucket for every call, like SnapTrade's shared pool."""
    return VenueLimiter(
        MemoryBuckets(),
        budgets_for=lambda venue: (RateBudget(RateScope.IP, capacity, refill_per_minute),),
    )


def _level(limiter: VenueLimiter) -> int:
    spec = limiter.buckets("snaptrade", None)[0][1]
    return limiter.backend.state(spec, limiter.clock_ms()).level_milli


def _finish(client, account_id: int):
    return client.post(
        f"/dashboard/connect/snaptrade/{account_id}",
        data={"account_ref": SNAP_ACCOUNT_REF, "strategy_name": "Finish limits"},
        follow_redirects=False,
    )


def _reset(routes) -> None:
    for route in routes.values():
        route.reset()


def _sent(routes) -> int:
    return sum(routes[name].call_count for name in FINISH_CALLS)


def _assert_still_pending(session_factory, account_id: int) -> None:
    """A refused finish wrote nothing: no activation, anchor, strategy or scope
    evidence, and the user's SnapTrade credential is still in the vault."""
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.PENDING
        assert account.anchor_at is None and account.anchor_balance_micros is None
        assert account.scope_evidence is None and account.scope_checked_at is None
        assert db.scalars(select(Strategy)).all() == []
        assert db.scalars(select(AnchorBalance)).all() == []
        assert credentials.active_credential(db, account) is not None


def _assert_active(session_factory, account_id: int) -> None:
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.ACTIVE
        assert account.anchor_balance_micros == 300_710_000
        assert db.scalars(select(Strategy)).one().name == "Finish limits"


@respx.mock
def test_the_finish_spends_the_outbound_bucket(client, user, session_factory, snaptrade_keys):
    routes = _mock_snaptrade()
    limiter = _limiter(250, 1)  # a trickle of refill: the level only moves by what is spent
    client.app.state.venue_limiter = limiter
    account_id = _start_snaptrade(client)  # RegisterUser: 1 token
    before = _level(limiter)
    _reset(routes)

    resp = _finish(client, account_id)
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
    assert routes["auth"].called and routes["balances"].called  # proof and anchor ran
    sent = _sent(routes)
    assert sent >= 3
    # Every SnapTrade request of the finish took one token (a second or two of
    # refill at 1 a minute is under one token).
    spent = (before - _level(limiter)) / 1000
    assert sent - 1 < spent <= sent
    _assert_active(session_factory, account_id)


@respx.mock
def test_a_saturated_limiter_returns_busy_and_the_pending_account_can_retry(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    drained = _limiter(1, 1)  # one token at 1 a minute, spent: the next call needs 60 s
    drained.acquire("snaptrade", None, "x")
    client.app.state.venue_limiter = drained
    _reset(routes)

    resp = _finish(client, account_id)
    assert resp.status_code == 503
    assert BUSY in resp.text
    assert int(resp.headers["Retry-After"]) >= 1
    for name in FINISH_CALLS:  # nothing reached SnapTrade, not even for the proof
        assert not routes[name].called, name
    _assert_still_pending(session_factory, account_id)

    # Later, with room in the bucket, the very same submit finishes the connect.
    client.app.state.venue_limiter = _limiter(250, 250)
    resp = _finish(client, account_id)
    assert resp.status_code == 303 and resp.headers["location"] == "/dashboard"
    assert routes["auth"].called and routes["balances"].called
    _assert_active(session_factory, account_id)


@respx.mock
def test_running_out_of_tokens_part_way_writes_nothing_and_a_retry_succeeds(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    client.app.state.venue_limiter = _limiter(2, 1)  # two requests, then a 60 s wait
    _reset(routes)

    resp = _finish(client, account_id)
    assert resp.status_code == 503 and BUSY in resp.text
    assert _sent(routes) == 2  # the third request was refused, not sent
    assert not routes["activities"].called  # the anchor's fills check was never reached
    _assert_still_pending(session_factory, account_id)

    client.app.state.venue_limiter = _limiter(250, 250)
    _reset(routes)
    assert _finish(client, account_id).status_code == 303
    _assert_active(session_factory, account_id)


@respx.mock
def test_a_429_from_snaptrade_during_the_finish_is_busy_and_blocks_the_bucket(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    routes["auth"].mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    limiter = _limiter(250, 250)
    client.app.state.venue_limiter = limiter

    resp = _finish(client, account_id)
    # Not "could not authenticate" and not an undetermined read-only probe: busy.
    assert resp.status_code == 503 and BUSY in resp.text
    assert int(resp.headers["Retry-After"]) >= 1
    assert "could not authenticate" not in resp.text.lower()
    spec = limiter.buckets("snaptrade", None)[0][1]
    state = limiter.backend.state(spec, limiter.clock_ms())
    assert state.blocked_until_ms > limiter.clock_ms() + 25_000  # Retry-After honoured
    _assert_still_pending(session_factory, account_id)

    # SnapTrade recovers and the limiter has cleared: the retry finishes.
    client.app.state.venue_limiter = _limiter(250, 250)
    ok = _snap_fixture("snaptrade_authorization_read.json")
    routes["auth"].mock(return_value=httpx.Response(200, json=ok))
    assert _finish(client, account_id).status_code == 303
    _assert_active(session_factory, account_id)


@respx.mock
def test_an_open_circuit_refuses_the_finish_without_a_call(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    limiter = _limiter(250, 250)
    for _ in range(5):  # BREAKER_THRESHOLD consecutive 5xx
        limiter.record_failure("snaptrade")
    client.app.state.venue_limiter = limiter
    _reset(routes)

    resp = _finish(client, account_id)
    assert resp.status_code == 503 and BUSY in resp.text
    for name in FINISH_CALLS:
        assert not routes[name].called, name
    _assert_still_pending(session_factory, account_id)


# ---- the fail-fast throttle's rounding slack ---------------------------------------------


class _ScriptedThrottle:
    """An inner throttle that refuses with the scripted waits, then grants."""

    venue = "snaptrade"

    def __init__(self, waits: list[float]) -> None:
        self.waits = list(waits)

    def before(self, endpoint: str) -> None:
        if self.waits:
            raise BudgetExhausted(self.waits.pop(0), endpoint)

    def after(self, endpoint: str, extra_cost: int = 0, used: int | None = None) -> None:
        pass


def test_a_retry_a_few_milliseconds_short_is_slept_off_not_busy():
    # Integer refill rounding can leave the bucket 5-10 ms short after the sleep.
    sleeps: list[float] = []
    throttle = _FailFastThrottle(_ScriptedThrottle([1.0, 0.008]), 2.0, sleeps.append)
    throttle.before("Balances")
    assert sleeps == [1.0, 0.008]


def test_a_retry_that_is_really_short_is_still_busy():
    sleeps: list[float] = []
    throttle = _FailFastThrottle(_ScriptedThrottle([1.0, 0.5]), 2.0, sleeps.append)
    with pytest.raises(VenueBusy):
        throttle.before("Balances")
    assert sleeps == [1.0]
