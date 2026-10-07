"""SnapTrade from web requests: the picker page's inbound limit, and the
outbound limiter (the worker's buckets, 429 backoff and circuit breaker) with
fail-fast behaviour. Venue traffic is respx-mocked (invariant 6)."""

import httpx
import pytest
import respx
from sqlalchemy import func, select

from alphamarket import ratelimit
from alphamarket.config import get_settings
from alphamarket.connectors.base import RateBudget, RateLimited, RateScope
from alphamarket.models import AuditLog, VenueAccount, VenueAccountStatus
from alphamarket.venue_limits import (
    WEB_MAX_WAIT_SECONDS,
    MemoryBuckets,
    VenueBusy,
    VenueLimiter,
    call_venue,
)
from authutil import consent, secure_account
from test_connect_wave2 import _mock_snaptrade, _start_snaptrade

BUSY = "SnapTrade is busy, try again in a minute."


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
        json={"email": "web@example.com", "password": "hunter2hunter2", "display_name": "Web"},
    )
    assert resp.status_code == 201
    secure_account(client, "web@example.com")
    return resp.json()


def _limiter(capacity: int, refill_per_minute: int, **kw) -> VenueLimiter:
    """One IP-scoped bucket for every venue, like SnapTrade's shared pool."""
    return VenueLimiter(
        MemoryBuckets(),
        budgets_for=lambda venue: (RateBudget(RateScope.IP, capacity, refill_per_minute),),
        **kw,
    )


def _drained() -> VenueLimiter:
    """One token refilling at 1 a minute, already spent: the next call needs 60 s."""
    limiter = _limiter(1, 1)
    limiter.acquire("snaptrade", None, "ListAccounts")
    return limiter


def _count_audit(session_factory, action: str) -> int:
    with session_factory() as db:
        return db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.action == action)
        )


def _assert_nothing_sent(routes) -> None:
    for name in ("accounts", "login", "register", "delete"):
        assert not routes[name].called, name


# ---- inbound: the picker page has its own limit ---------------------------------------


def test_the_rule_numbers():
    rule = ratelimit.RULES["snaptrade_picker_user"]
    assert (rule.limit, rule.window_seconds) == (30, 600)
    assert "snaptrade_picker_user" != "venues_user"  # its own budget, not the connect one


@respx.mock
def test_picker_get_is_429_after_thirty_loads_and_spends_nothing_more(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    url = f"/dashboard/connect/snaptrade/{account_id}"
    for _ in range(30):
        assert client.get(url).status_code == 200
    # Each load asks SnapTrade twice (accounts, then a fresh portal link).
    assert routes["accounts"].call_count == 30 and routes["login"].call_count == 30
    decrypts = _count_audit(session_factory, "credential.decrypt")

    resp = client.get(url)
    assert resp.status_code == 429
    assert "reloaded this page too many times" in resp.text  # the page, not JSON
    assert int(resp.headers["Retry-After"]) >= 1
    assert "snaptrade.com/connect" not in resp.text  # no portal link, no account list
    assert "Try again" not in resp.text  # and no retry link inviting more reloads
    # Not one more SnapTrade call, and the vault was not opened (no audited decrypt).
    assert routes["accounts"].call_count == 30 and routes["login"].call_count == 30
    assert _count_audit(session_factory, "credential.decrypt") == decrypts

    # The connect budget (venues_user, 10 an hour) is untouched by 31 picker loads:
    # a new connect still starts.
    resp = client.post(
        "/dashboard/connect/snaptrade/start",
        data={"consent_sha256": consent("snaptrade")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert routes["register"].call_count == 2


@respx.mock
def test_a_released_account_redirects_without_spending_the_limit(
    client, user, session_factory, snaptrade_keys
):
    _mock_snaptrade()
    account_id = _start_snaptrade(client)
    with session_factory() as db:
        db.get(VenueAccount, account_id).status = VenueAccountStatus.DISCONNECTED
        db.commit()
    for _ in range(40):  # a redirect calls nothing, so it is not counted
        resp = client.get(f"/dashboard/connect/snaptrade/{account_id}", follow_redirects=False)
        assert resp.status_code == 303


# ---- outbound: the picker fails fast when the limiter says wait -----------------------


@respx.mock
def test_a_refusing_limiter_renders_busy_and_calls_snaptrade_not_at_all(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    for route in routes.values():
        route.reset()  # forget the registerUser call; count from here
    client.app.state.venue_limiter = _drained()

    resp = client.get(f"/dashboard/connect/snaptrade/{account_id}")
    assert resp.status_code == 503
    assert BUSY in resp.text
    assert int(resp.headers["Retry-After"]) >= 1
    _assert_nothing_sent(routes)

    # Once the limiter has room again the same page works.
    client.app.state.venue_limiter = _limiter(250, 250)
    resp = client.get(f"/dashboard/connect/snaptrade/{account_id}")
    assert resp.status_code == 200 and BUSY not in resp.text
    assert routes["accounts"].called and routes["login"].called


@respx.mock
def test_an_open_circuit_renders_busy_without_calling_snaptrade(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    for route in routes.values():
        route.reset()
    limiter = _limiter(250, 250)
    for _ in range(5):  # BREAKER_THRESHOLD consecutive 5xx
        limiter.record_failure("snaptrade")
    assert limiter.breaker_open_until("snaptrade") is not None
    client.app.state.venue_limiter = limiter

    resp = client.get(f"/dashboard/connect/snaptrade/{account_id}")
    assert resp.status_code == 503 and BUSY in resp.text
    _assert_nothing_sent(routes)


@respx.mock
def test_a_429_from_snaptrade_penalises_the_bucket_and_the_next_load_sends_nothing(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _start_snaptrade(client)
    routes["accounts"].mock(return_value=httpx.Response(429, headers={"Retry-After": "30"}))
    limiter = _limiter(250, 250)
    client.app.state.venue_limiter = limiter
    url = f"/dashboard/connect/snaptrade/{account_id}"

    resp = client.get(url)
    assert resp.status_code == 503 and BUSY in resp.text
    assert routes["accounts"].call_count == 1
    assert not routes["login"].called  # the page stopped at the first refusal
    spec = limiter.buckets("snaptrade", None)[0][1]
    state = limiter.backend.state(spec, limiter.clock_ms())
    assert state.blocked_until_ms > limiter.clock_ms() + 25_000  # blocked until Retry-After (30 s)
    assert state.penalty_until_ms > limiter.clock_ms()  # and refill halved for 10 minutes

    resp = client.get(url)  # the bucket is blocked: fail fast, SnapTrade is left alone
    assert resp.status_code == 503 and BUSY in resp.text
    assert routes["accounts"].call_count == 1


@respx.mock
def test_start_renders_busy_and_registers_no_user(client, user, session_factory, snaptrade_keys):
    routes = _mock_snaptrade()
    client.app.state.venue_limiter = _drained()
    resp = client.post(
        "/dashboard/connect/snaptrade/start", data={"consent_sha256": consent("snaptrade")}
    )
    assert resp.status_code == 503 and BUSY in resp.text
    assert int(resp.headers["Retry-After"]) >= 1
    _assert_nothing_sent(routes)  # no billable SnapTrade user was registered
    with session_factory() as db:
        assert db.scalars(select(VenueAccount)).all() == []


@respx.mock
def test_start_picker_and_delete_spend_the_shared_bucket(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    limiter = _limiter(10, 1)  # 10 tokens, refilling at 1 a minute (17 milli-tokens a second)
    client.app.state.venue_limiter = limiter
    account_id = _start_snaptrade(client)  # RegisterUser: 1
    assert client.get(f"/dashboard/connect/snaptrade/{account_id}").status_code == 200  # 2
    assert client.delete(f"/venues/{account_id}").status_code == 204  # DeleteUser: 1
    assert routes["delete"].called
    spec = limiter.buckets("snaptrade", None)[0][1]
    level = limiter.backend.state(spec, limiter.clock_ms()).level_milli
    assert 6_000 <= level < 6_500  # 10 - 4 tokens, plus a second or two of refill


# ---- call_venue by hand -----------------------------------------------------------------


def _fake_clock_limiter(capacity: int, refill_per_minute: int):
    now = [1_000_000]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += int(seconds * 1000)

    limiter = _limiter(capacity, refill_per_minute, clock_ms=lambda: now[0])
    return limiter, now, sleeps, sleep


def _http_error(status: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.snaptrade.com/api/v1/accounts")
    return httpx.HTTPStatusError(
        str(status), request=request, response=httpx.Response(status, headers=headers)
    )


def test_a_short_wait_is_slept_and_the_call_goes_ahead():
    # Capacity 1, 60 a minute (1 token a second), spent: the next token is
    # ceil(1000 milli-tokens x 60 / 60) = 1000 ms away, under the 2 s limit.
    limiter, now, sleeps, sleep = _fake_clock_limiter(1, 60)
    limiter.acquire("snaptrade", None, "x")

    def call(throttle):
        throttle.before("ListAccounts")
        return "sent"

    assert call_venue(limiter, "snaptrade", None, call, sleep=sleep) == "sent"
    assert sleeps == [1.0]


def test_a_long_wait_fails_fast_without_sleeping_or_sending():
    # Capacity 1, 20 a minute: the next token is ceil(1000 x 60 / 20) = 3000 ms
    # away, over WEB_MAX_WAIT_SECONDS (2.0).
    assert WEB_MAX_WAIT_SECONDS == 2.0
    limiter, now, sleeps, sleep = _fake_clock_limiter(1, 20)
    limiter.acquire("snaptrade", None, "x")
    sent = []

    def call(throttle):
        throttle.before("ListAccounts")
        sent.append(1)  # the HTTP request
        return "sent"

    with pytest.raises(VenueBusy) as exc:
        call_venue(limiter, "snaptrade", None, call, sleep=sleep)
    assert exc.value.retry_after_seconds == 3.0 and exc.value.reason == "budget"
    assert sleeps == [] and sent == []


def test_five_outage_failures_open_the_circuit_and_a_success_resets_the_count():
    limiter, now, sleeps, sleep = _fake_clock_limiter(250, 250)

    def outage(throttle):
        throttle.before("ListAccounts")
        raise _http_error(503)

    def fine(throttle):
        throttle.before("ListAccounts")
        return "ok"

    for _ in range(4):
        with pytest.raises(httpx.HTTPStatusError):
            call_venue(limiter, "snaptrade", None, outage, sleep=sleep)
    assert limiter.breaker_open_until("snaptrade") is None
    assert call_venue(limiter, "snaptrade", None, fine, sleep=sleep) == "ok"  # count back to 0
    for _ in range(4):
        with pytest.raises(httpx.HTTPStatusError):
            call_venue(limiter, "snaptrade", None, outage, sleep=sleep)
    assert limiter.breaker_open_until("snaptrade") is None  # 4 since the success, not 5
    with pytest.raises(httpx.HTTPStatusError):
        call_venue(limiter, "snaptrade", None, outage, sleep=sleep)  # the 5th opens it
    sent = []
    with pytest.raises(VenueBusy) as exc:
        call_venue(limiter, "snaptrade", None, lambda t: sent.append(1), sleep=sleep)
    assert exc.value.reason == "circuit open" and sent == []
    # BREAKER_OPEN_MS is 2 minutes from the failure that opened it: 120 s left.
    assert exc.value.retry_after_seconds == 120.0


def test_a_client_error_is_not_an_outage_and_propagates():
    limiter, now, sleeps, sleep = _fake_clock_limiter(250, 250)

    def forbidden(throttle):
        throttle.before("ListAccounts")
        raise _http_error(401)

    for _ in range(6):
        with pytest.raises(httpx.HTTPStatusError):
            call_venue(limiter, "snaptrade", None, forbidden, sleep=sleep)
    assert limiter.breaker_open_until("snaptrade") is None  # 4xx never trips the breaker


@pytest.mark.parametrize(
    "make",
    [lambda: RateLimited(30.0, "ListAccounts"), lambda: _http_error(429, {"Retry-After": "30"})],
)
def test_a_429_blocks_the_buckets_for_retry_after_and_raises_busy(make):
    limiter, now, sleeps, sleep = _fake_clock_limiter(250, 250)

    def limited(throttle):
        throttle.before("ListAccounts")
        raise make()

    with pytest.raises(VenueBusy) as exc:
        call_venue(limiter, "snaptrade", None, limited, sleep=sleep)
    assert exc.value.reason == "rate limited" and exc.value.retry_after_seconds == 30.0
    spec = limiter.buckets("snaptrade", None)[0][1]
    state = limiter.backend.state(spec, now[0])
    assert state.blocked_until_ms == 1_000_000 + 30_000
    assert state.penalty_until_ms == 1_000_000 + 10 * 60 * 1000


def test_without_a_limiter_the_call_is_made_with_no_throttle():
    assert call_venue(None, "snaptrade", None, lambda throttle: throttle) is None
