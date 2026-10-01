"""Outbound rate limiting (P2): token-bucket math by hand, adaptive 429
handling, the circuit breaker, and error classification.

Every bucket scenario runs on three backends: MemoryBuckets, the SQL backend
on SQLite, and (pg-marked) the same SQL on real Postgres, so the two
implementations can never drift apart."""

import uuid

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from alphamarket.connectors.base import BudgetExhausted, RateBudget, RateLimited, RateScope
from alphamarket.db import Base
from alphamarket.venue_limits import (
    MemoryBuckets,
    PostgresBuckets,
    VenueLimiter,
    credential_ref,
    is_venue_outage,
    rate_limit_from_http,
    refill_gain_milli,
    wait_ms_for,
)

T0 = 1_800_000_000_000  # epoch ms; any fixed instant works

KRAKEN = RateBudget(
    scope=RateScope.CREDENTIAL,
    capacity=15,
    refill_per_minute=20,
    costs=(("TradesHistory", 2), ("Ledgers", 2)),
)
HYPERLIQUID = RateBudget(
    scope=RateScope.IP, capacity=1200, refill_per_minute=1200, costs=(("userFills", 20),)
)


class Clock:
    def __init__(self, ms: int = T0) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms


@pytest.fixture(params=["memory", "sqlite", pytest.param("pg", marks=pytest.mark.pg)])
def backend(request):
    if request.param == "memory":
        yield MemoryBuckets()
        return
    if request.param == "sqlite":
        engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        Base.metadata.create_all(engine)
        yield PostgresBuckets(engine)
        engine.dispose()
        return
    engine = create_engine(request.getfixturevalue("pg_url"))
    yield PostgresBuckets(engine)
    engine.dispose()


def make_limiter(backend, budgets, clock, **kw):
    # A fresh venue name per test: the pg database is shared by the session.
    venue = f"v{uuid.uuid4().hex[:8]}"
    limiter = VenueLimiter(
        backend, "203.0.113.7", budgets_for=lambda v: budgets, clock_ms=clock, **kw
    )
    return limiter, venue


def level(limiter, venue, cred, index=0):
    spec = limiter.buckets(venue, cred)[index][1]
    return limiter.backend.state(spec, limiter.clock_ms()).level_milli


# ---- pure formulas -----------------------------------------------------------------


def test_refill_and_wait_formulas():
    # Kraken: 20 tokens/min = 20/60 milli-tokens per ms.
    # 3000 ms * 20 // 60 = 60000 // 60 = 1000 milli-tokens (one token every 3 s).
    assert refill_gain_milli(3000, 20, penalised=False) == 1000
    # Penalised (halved): 3000 * 20 // 120 = 500.
    assert refill_gain_milli(3000, 20, penalised=True) == 500
    # Floors: 2999 * 20 // 60 = 59980 // 60 = 999.
    assert refill_gain_milli(2999, 20, penalised=False) == 999
    assert refill_gain_milli(-5, 20, penalised=False) == 0  # clock skew never drains
    # Wait for a 1000 milli deficit: ceil(1000 * 60 / 20) = 3000 ms; halved rate 6000 ms.
    assert wait_ms_for(1000, 20, penalised=False) == 3000
    assert wait_ms_for(1000, 20, penalised=True) == 6000
    # Deficit 1: ceil(60 / 20) = 3 ms. Hyperliquid 20000 deficit: 20000*60/1200 = 1000 ms.
    assert wait_ms_for(1, 20, penalised=False) == 3
    assert wait_ms_for(20_000, 1200, penalised=False) == 1000
    assert wait_ms_for(0, 20, penalised=False) == 0


# ---- Kraken: capacity 15, refill 20/min, TradesHistory costs 2 ------------------------


def test_kraken_bucket_by_hand(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (KRAKEN,), clock)
    throttle = limiter.throttle(venue, "cred-a")
    # Full bucket 15 tokens = 15000 milli. Seven TradesHistory calls cost
    # 7 x 2000 = 14000, leaving 1000.
    for _ in range(7):
        throttle.before("TradesHistory")
    assert level(limiter, venue, "cred-a") == 1000
    # The 8th needs 2000, has 1000: deficit 1000 -> ceil(1000 * 60 / 20) = 3000 ms.
    with pytest.raises(BudgetExhausted) as exc:
        throttle.before("TradesHistory")
    assert exc.value.retry_after_seconds == 3.0
    assert exc.value.endpoint == "TradesHistory"
    assert level(limiter, venue, "cred-a") == 1000  # a refusal spends nothing
    # At +2999 ms: 1000 + 2999*20//60 (= 999) = 1999 < 2000; deficit 1 -> 3 ms.
    clock.ms = T0 + 2999
    with pytest.raises(BudgetExhausted) as exc:
        throttle.before("TradesHistory")
    assert exc.value.retry_after_seconds == 0.003
    # At +3000 ms: 1000 + 1000 = 2000 -> granted, level 0.
    clock.ms = T0 + 3000
    throttle.before("TradesHistory")
    assert level(limiter, venue, "cred-a") == 0
    # An unlisted endpoint costs default_cost 1 = 1000 milli: 1000*60/20 = 3000 ms away.
    with pytest.raises(BudgetExhausted) as exc:
        throttle.before("Balance")
    assert exc.value.retry_after_seconds == 3.0
    # Another credential has its own bucket (RateScope.CREDENTIAL).
    limiter.throttle(venue, "cred-b").before("TradesHistory")
    assert level(limiter, venue, "cred-b") == 13_000


def test_kraken_refill_never_exceeds_capacity(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (KRAKEN,), clock)
    limiter.throttle(venue, "c").before("Ledgers")  # 15000 - 2000 = 13000
    clock.ms = T0 + 3_600_000  # an hour: 3.6e6 * 20 // 60 = 1.2e6 gained, capped at 15000
    assert level(limiter, venue, "c") == 15_000


def test_rate_limited_halves_refill_for_ten_minutes_and_honours_retry_after(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (KRAKEN,), clock)
    throttle = limiter.throttle(venue, "c")
    for _ in range(7):
        throttle.before("TradesHistory")  # level 1000
    clock.ms = T0 + 3000
    throttle.before("TradesHistory")  # 1000 + 1000 - 2000 = 0
    # The venue answers 429 with Retry-After: 5 at T0+3000.
    limiter.on_rate_limited(venue, "c", 5.0, "TradesHistory")
    # At T0+6000: halved refill 3000 * 20 // 120 = 500 -> deficit 1500 ->
    # ceil(1500 * 120 / 20) = 9000 ms; blocked for 2000 ms more; wait = max = 9000.
    clock.ms = T0 + 6000
    with pytest.raises(BudgetExhausted) as exc:
        throttle.before("TradesHistory")
    assert exc.value.retry_after_seconds == 9.0
    # Blocked dominates when tokens are there: cost-1 call needs 1000, has 500:
    # deficit 500 -> 500*120/20 = 3000 ms; blocked 2000 ms; max = 3000.
    with pytest.raises(BudgetExhausted) as exc:
        throttle.before("Balance")
    assert exc.value.retry_after_seconds == 3.0
    # At T0+21000: 18000 ms * 20 // 120 = 3000 -> granted, 3000 - 2000 = 1000.
    # (Unpenalised it would have been 18000 * 20 // 60 = 6000.)
    clock.ms = T0 + 21_000
    throttle.before("TradesHistory")
    assert level(limiter, venue, "c") == 1000


def test_retry_after_blocks_even_a_full_bucket(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (KRAKEN,), clock)
    limiter.throttle(venue, "c").before("Balance")  # creates the bucket: 14000
    limiter.on_rate_limited(venue, "c", 30.0)
    clock.ms = T0 + 10_000
    with pytest.raises(BudgetExhausted) as exc:
        limiter.throttle(venue, "c").before("Balance")
    assert exc.value.retry_after_seconds == 20.0  # blocked until T0 + 30000
    clock.ms = T0 + 30_000
    limiter.throttle(venue, "c").before("Balance")


# ---- Hyperliquid: IP bucket 1200/min, +1 per 20 items after the response -------------


def test_hyperliquid_ip_bucket_with_after_charge(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (HYPERLIQUID,), clock)
    # IP scope: every account behind this egress IP shares one bucket, and
    # credential-less market data spends it too.
    alice, bob, public = (limiter.throttle(venue, c) for c in ("alice", "bob", None))
    # 1200 tokens = 1,200,000 milli. userFills weight 20: 1,180,000.
    alice.before("userFills")
    # The response carried 2000 fills: +1 per 20 items = 2000 // 20 = 100 tokens.
    alice.after("userFills", extra_cost=2000 // 20)
    assert level(limiter, venue, None) == 1_080_000  # 1,180,000 - 100,000
    # Bob's request spends the same IP bucket: 1,080,000 - 20,000 = 1,060,000.
    bob.before("userFills")
    assert level(limiter, venue, "bob") == 1_060_000
    # 53 more weight-20 requests: 1,060,000 - 53 * 20,000 = 0.
    for _ in range(53):
        public.before("userFills")
    assert level(limiter, venue, None) == 0
    # Refill 1200/min = 20 milli-tokens per ms: a 20,000 deficit is 1000 ms.
    with pytest.raises(BudgetExhausted) as exc:
        public.before("userFills")
    assert exc.value.retry_after_seconds == 1.0
    # 6 s later: 6000 * 1200 // 60 = 120,000 milli = 120 tokens.
    clock.ms = T0 + 6000
    assert level(limiter, venue, None) == 120_000


def test_after_charge_can_overdraw_and_delays_the_next_call(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (HYPERLIQUID,), clock)
    t = limiter.throttle(venue, None)
    t.before("userFills")  # 1,180,000
    t.after("userFills", extra_cost=1280)  # 1,180,000 - 1,280,000 = -100,000
    assert level(limiter, venue, None) == -100_000
    # Next weight-20 call needs 20,000: deficit 120,000 -> 120000 * 60 / 1200 = 6000 ms.
    with pytest.raises(BudgetExhausted) as exc:
        t.before("userFills")
    assert exc.value.retry_after_seconds == 6.0


def test_after_used_syncs_down_to_venue_reported_usage(backend):
    clock = Clock()
    binance_like = RateBudget(scope=RateScope.IP, capacity=1200, refill_per_minute=1200)
    limiter, venue = make_limiter(backend, (binance_like,), clock)
    t = limiter.throttle(venue, None)
    t.before("myTrades")  # 1,199,000
    # X-MBX-USED-WEIGHT-1M: 300 -> level = min(1,199,000, (1200 - 300) * 1000) = 900,000.
    t.after("myTrades", used=300)
    assert level(limiter, venue, None) == 900_000
    # Reported usage below ours never raises the level (we stay conservative).
    t.after("myTrades", used=10)
    assert level(limiter, venue, None) == 900_000


def test_refusal_on_one_bucket_refunds_the_others(backend):
    clock = Clock()
    per_key = RateBudget(scope=RateScope.CREDENTIAL, capacity=10, refill_per_minute=60)
    per_ip = RateBudget(scope=RateScope.IP, capacity=1, refill_per_minute=60)
    limiter, venue = make_limiter(backend, (per_key, per_ip), clock)
    t = limiter.throttle(venue, "k")
    t.before("x")  # key 9000, ip 0
    with pytest.raises(BudgetExhausted) as exc:
        t.before("x")  # key -> 8000, then ip refuses: key refunded to 9000
    # IP deficit 1000 milli at 60/min: ceil(1000 * 60 / 60) = 1000 ms.
    assert exc.value.retry_after_seconds == 1.0
    assert level(limiter, venue, "k", 0) == 9000
    assert level(limiter, venue, "k", 1) == 0


def test_public_calls_skip_credential_buckets():
    limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda v: (KRAKEN, HYPERLIQUID))
    assert [s.key for _, s in limiter.buckets("kraken", None)] == ["kraken:ip:default:1"]
    assert [s.key for _, s in limiter.buckets("kraken", "abc")] == [
        "kraken:cred:abc:0",
        "kraken:ip:default:1",
    ]


def test_credential_ref_is_stable_and_hides_the_key_id():
    ref = credential_ref("kraken", "my-key-id")
    assert ref == credential_ref("kraken", "my-key-id")
    assert ref != credential_ref("binanceus", "my-key-id")
    assert "my-key-id" not in ref and len(ref) == 16


# ---- circuit breaker ------------------------------------------------------------------


def test_circuit_breaker_opens_after_five_failures_and_closes(backend):
    clock = Clock()
    limiter, venue = make_limiter(backend, (), clock)
    for _ in range(4):
        assert limiter.record_failure(venue) is None
    assert limiter.breaker_open_until(venue) is None
    opened = limiter.record_failure(venue)  # 5th consecutive: open for 2 minutes
    assert opened is not None
    assert int(opened.timestamp() * 1000) == T0 + 120_000
    clock.ms = T0 + 119_999
    assert limiter.breaker_open_until(venue) == opened
    clock.ms = T0 + 120_000
    assert limiter.breaker_open_until(venue) is None  # half-open: next job may try
    # Half-open: one more failure reopens at once.
    reopened = limiter.record_failure(venue)
    assert int(reopened.timestamp() * 1000) == T0 + 240_000
    # A success closes it and resets the count: four failures stay closed.
    clock.ms = T0 + 240_000
    limiter.record_success(venue)
    for _ in range(4):
        assert limiter.record_failure(venue) is None
    assert limiter.breaker_open_until(venue) is None


# ---- classifying errors ----------------------------------------------------------------


def _status_error(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.example.test/0/private/TradesHistory")
    resp = httpx.Response(code, headers=headers or {}, request=req)
    return httpx.HTTPStatusError("boom", request=req, response=resp)


def test_outage_classification():
    assert is_venue_outage(_status_error(503))
    assert is_venue_outage(httpx.ConnectTimeout("t"))
    assert is_venue_outage(httpx.ConnectError("c"))
    assert not is_venue_outage(_status_error(401))
    assert not is_venue_outage(_status_error(429))
    assert not is_venue_outage(ValueError("x"))


def test_http_429_becomes_rate_limited_with_retry_after():
    rl = rate_limit_from_http(_status_error(429, {"Retry-After": "12"}))
    assert isinstance(rl, RateLimited) and rl.retry_after_seconds == 12.0
    assert rl.endpoint == "/0/private/TradesHistory"
    assert rate_limit_from_http(_status_error(418)).retry_after_seconds == 60.0
    dated = _status_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert rate_limit_from_http(dated).retry_after_seconds == 60.0
    assert rate_limit_from_http(_status_error(500)) is None
