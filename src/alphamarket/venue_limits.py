"""Outbound rate limiting (platform_plan 2.5, implementation_plan P2).

Koine calls venues on behalf of every connected account, so it must spend
each venue's budget the way the venue counts it: per key/account
(RateScope.CREDENTIAL) and per caller IP (RateScope.IP, shared by every
account a worker polls). Each `RateBudget` in the venue's profile becomes one
token bucket per (venue, credential) or per (venue, egress IP), and the
connector charges it through `connectors.base.Throttle` before every request.

Bucket arithmetic is integer only: levels are milli-tokens (1 token = 1000)
and clocks are epoch milliseconds. A bucket refills at
`refill_per_minute` tokens per minute, which is refill_per_minute / 60
milli-tokens per millisecond:

    gain_milli = elapsed_ms * refill_per_minute // 60      (// 120 while penalised)
    level      = min(capacity_milli, level + gain_milli)

and a request that finds `level < cost` waits

    wait_ms = ceil((cost_milli - level) * 60 / refill_per_minute)   (x2 while penalised)

Flooring the gain loses under one milli-token per update, so the limiter can
only ever be slightly more conservative than the venue, never less.

Adaptive behaviour: a 429/418 (`RateLimited`) halves that bucket's refill for
10 minutes and blocks it until the venue's Retry-After. A per-venue circuit
breaker opens for 2 minutes after 5 consecutive 5xx/timeouts; jobs for an
open venue are deferred, never failed (jobs.py), so an outage cannot
dead-letter every account.

Two backends share one interface: MemoryBuckets (tests, one process) and
PostgresBuckets (rate_buckets / venue_circuits tables, shared by every worker
process; the refill+take is one UPDATE ... RETURNING, so two workers can never
both spend the last token). The SQL is plain enough that it also runs on
SQLite, which the unit tests use.
"""

import hashlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx
from sqlalchemy import Engine, text

from alphamarket.connectors.base import BudgetExhausted, RateBudget, RateLimited, RateScope

logger = logging.getLogger(__name__)

MILLI = 1000  # milli-tokens per token
PENALTY_MS = 10 * 60 * 1000  # adaptive: refill halved for 10 minutes after a 429
BREAKER_THRESHOLD = 5  # consecutive 5xx/timeouts that open a venue's circuit
BREAKER_OPEN_MS = 2 * 60 * 1000  # how long an open circuit stays open
DEFAULT_RETRY_AFTER_S = 60.0  # 429 without a usable Retry-After header


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def ms_to_datetime(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC)


def credential_ref(venue: str, key_id: str) -> str:
    """Stable, non-reversible bucket id for one credential. The raw key id
    never lands in rate_buckets (invariant 4 errs on the side of caution)."""
    return hashlib.sha256(f"{venue}\x00{key_id}".encode()).hexdigest()[:16]


# ---- pure bucket math (the SQL backend computes exactly the same) -------------


def refill_gain_milli(elapsed_ms: int, refill_per_minute: int, penalised: bool) -> int:
    """Milli-tokens restored after `elapsed_ms`: rpm tokens/min = rpm/60
    milli-tokens per ms, halved while penalised. Floors; never negative."""
    if elapsed_ms <= 0:
        return 0
    return elapsed_ms * refill_per_minute // (120 if penalised else 60)


def refilled_level(
    level_milli: int,
    updated_ms: int,
    at_ms: int,
    capacity_milli: int,
    refill_per_minute: int,
    penalty_until_ms: int,
) -> int:
    gained = level_milli + refill_gain_milli(
        at_ms - updated_ms, refill_per_minute, penalty_until_ms > at_ms
    )
    return capacity_milli if gained > capacity_milli else gained


def wait_ms_for(deficit_milli: int, refill_per_minute: int, penalised: bool) -> int:
    """Milliseconds until `deficit_milli` has refilled (ceil)."""
    if deficit_milli <= 0:
        return 0
    num = deficit_milli * (120 if penalised else 60)
    return -(-num // refill_per_minute)


@dataclass(frozen=True)
class BucketSpec:
    key: str
    capacity_milli: int
    refill_per_minute: int


@dataclass
class BucketState:
    level_milli: int
    updated_ms: int
    penalty_until_ms: int = 0
    blocked_until_ms: int = 0


def _refusal_wait_ms(state: BucketState, spec: BucketSpec, cost_milli: int, at_ms: int) -> int:
    level = refilled_level(
        state.level_milli,
        state.updated_ms,
        at_ms,
        spec.capacity_milli,
        spec.refill_per_minute,
        state.penalty_until_ms,
    )
    wait = wait_ms_for(cost_milli - level, spec.refill_per_minute, state.penalty_until_ms > at_ms)
    blocked = state.blocked_until_ms - at_ms
    return max(wait, blocked, 1)


# ---- backends -----------------------------------------------------------------


class BucketBackend(Protocol):
    def take(self, spec: BucketSpec, cost_milli: int, at_ms: int) -> int:
        """Refill, then take `cost_milli` if available. 0 = granted; otherwise
        the milliseconds to wait (nothing was taken)."""

    def charge(self, spec: BucketSpec, cost_milli: int, at_ms: int) -> None:
        """Take unconditionally (the level may go negative); a negative cost
        refunds, capped at capacity."""

    def cap_to_used(self, spec: BucketSpec, used_milli: int, at_ms: int) -> None:
        """The venue reported `used` of the capacity: level = min(level, cap - used)."""

    def penalize(
        self, spec: BucketSpec, at_ms: int, penalty_ms: int, blocked_until_ms: int
    ) -> None:
        """Refill at the old rate, then halve refill until at+penalty and block
        the bucket until `blocked_until_ms`."""

    def state(self, spec: BucketSpec, at_ms: int) -> BucketState | None:
        """Stored state refilled to `at_ms` (read-only); None if never used."""

    def record_failure(self, venue: str, at_ms: int, threshold: int, open_ms: int) -> int:
        """Count one consecutive failure; returns open_until_ms (0 = closed)."""

    def record_success(self, venue: str) -> None: ...

    def open_until_ms(self, venue: str) -> int: ...


class MemoryBuckets:
    """In-process buckets and breakers. One worker process only: two
    processes would each spend the venue's full budget."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, BucketState] = {}
        self._circuits: dict[str, list[int]] = {}  # venue -> [consecutive, open_until_ms]

    def _get(self, spec: BucketSpec, at_ms: int) -> BucketState:
        st = self._buckets.get(spec.key)
        if st is None:
            st = self._buckets[spec.key] = BucketState(spec.capacity_milli, at_ms)
        return st

    def _refill(self, st: BucketState, spec: BucketSpec, at_ms: int) -> None:
        st.level_milli = refilled_level(
            st.level_milli,
            st.updated_ms,
            at_ms,
            spec.capacity_milli,
            spec.refill_per_minute,
            st.penalty_until_ms,
        )
        st.updated_ms = max(st.updated_ms, at_ms)

    def take(self, spec: BucketSpec, cost_milli: int, at_ms: int) -> int:
        with self._lock:
            st = self._get(spec, at_ms)
            level = refilled_level(
                st.level_milli,
                st.updated_ms,
                at_ms,
                spec.capacity_milli,
                spec.refill_per_minute,
                st.penalty_until_ms,
            )
            if st.blocked_until_ms <= at_ms and level >= cost_milli:
                st.level_milli = level - cost_milli
                st.updated_ms = max(st.updated_ms, at_ms)
                return 0
            return _refusal_wait_ms(st, spec, cost_milli, at_ms)

    def charge(self, spec: BucketSpec, cost_milli: int, at_ms: int) -> None:
        with self._lock:
            st = self._get(spec, at_ms)
            self._refill(st, spec, at_ms)
            st.level_milli = min(spec.capacity_milli, st.level_milli - cost_milli)

    def cap_to_used(self, spec: BucketSpec, used_milli: int, at_ms: int) -> None:
        with self._lock:
            st = self._get(spec, at_ms)
            self._refill(st, spec, at_ms)
            st.level_milli = min(st.level_milli, spec.capacity_milli - used_milli)

    def penalize(
        self, spec: BucketSpec, at_ms: int, penalty_ms: int, blocked_until_ms: int
    ) -> None:
        with self._lock:
            st = self._get(spec, at_ms)
            self._refill(st, spec, at_ms)
            st.penalty_until_ms = at_ms + penalty_ms
            st.blocked_until_ms = max(st.blocked_until_ms, blocked_until_ms)

    def state(self, spec: BucketSpec, at_ms: int) -> BucketState | None:
        with self._lock:
            st = self._buckets.get(spec.key)
            if st is None:
                return None
            level = refilled_level(
                st.level_milli,
                st.updated_ms,
                at_ms,
                spec.capacity_milli,
                spec.refill_per_minute,
                st.penalty_until_ms,
            )
            return BucketState(level, at_ms, st.penalty_until_ms, st.blocked_until_ms)

    def record_failure(self, venue: str, at_ms: int, threshold: int, open_ms: int) -> int:
        with self._lock:
            c = self._circuits.setdefault(venue, [0, 0])
            c[0] += 1
            if c[0] >= threshold:
                c[1] = at_ms + open_ms
            return c[1] if c[1] > at_ms else 0

    def record_success(self, venue: str) -> None:
        with self._lock:
            self._circuits.pop(venue, None)

    def open_until_ms(self, venue: str) -> int:
        with self._lock:
            return self._circuits.get(venue, [0, 0])[1]


# Shared SQL fragments. Every bound value is CAST so Postgres never has to
# guess a parameter type; the same statements run on SQLite.
_NOW = "CAST(:now AS BIGINT)"
_CAP = "CAST(:cap AS BIGINT)"
_RPM = "CAST(:rpm AS BIGINT)"
_GAIN = (
    f"(CASE WHEN {_NOW} > updated_ms THEN ({_NOW} - updated_ms) * {_RPM} / "
    f"(CASE WHEN penalty_until_ms > {_NOW} THEN 120 ELSE 60 END) ELSE 0 END)"
)
_REFILLED = f"(CASE WHEN level_milli + {_GAIN} > {_CAP} THEN {_CAP} ELSE level_milli + {_GAIN} END)"
_TOUCH = (
    f"updated_ms = CASE WHEN {_NOW} > updated_ms THEN {_NOW} ELSE updated_ms END, "
    f"capacity_milli = {_CAP}, refill_per_minute = {_RPM}"
)

_SQL_ENSURE = text(
    "INSERT INTO rate_buckets (bucket_key, capacity_milli, refill_per_minute, level_milli,"
    " updated_ms, penalty_until_ms, blocked_until_ms)"
    f" VALUES (:key, {_CAP}, {_RPM}, {_CAP}, {_NOW}, 0, 0)"
    " ON CONFLICT (bucket_key) DO NOTHING"
)
_SQL_TAKE = text(
    f"UPDATE rate_buckets SET level_milli = {_REFILLED} - CAST(:cost AS BIGINT), {_TOUCH}"
    f" WHERE bucket_key = :key AND blocked_until_ms <= {_NOW}"
    f" AND {_REFILLED} >= CAST(:cost AS BIGINT)"
    " RETURNING level_milli"
)
_SQL_CHARGE = text(
    "UPDATE rate_buckets SET level_milli = CASE"
    f" WHEN {_REFILLED} - CAST(:cost AS BIGINT) > {_CAP} THEN {_CAP}"
    f" ELSE {_REFILLED} - CAST(:cost AS BIGINT) END, {_TOUCH}"
    " WHERE bucket_key = :key"
)
_SQL_CAP_USED = text(
    "UPDATE rate_buckets SET level_milli = CASE"
    f" WHEN {_REFILLED} > {_CAP} - CAST(:used AS BIGINT) THEN {_CAP} - CAST(:used AS BIGINT)"
    f" ELSE {_REFILLED} END, {_TOUCH}"
    " WHERE bucket_key = :key"
)
_SQL_PENALIZE = text(
    f"UPDATE rate_buckets SET level_milli = {_REFILLED}, {_TOUCH},"
    f" penalty_until_ms = {_NOW} + CAST(:penalty AS BIGINT),"
    " blocked_until_ms = CASE WHEN blocked_until_ms > CAST(:blocked AS BIGINT)"
    " THEN blocked_until_ms ELSE CAST(:blocked AS BIGINT) END"
    " WHERE bucket_key = :key"
)
_SQL_STATE = text(
    "SELECT level_milli, updated_ms, penalty_until_ms, blocked_until_ms"
    " FROM rate_buckets WHERE bucket_key = :key"
)
_SQL_FAIL = text(
    "INSERT INTO venue_circuits (venue, consecutive_failures, open_until_ms)"
    " VALUES (:venue, 1, CASE WHEN 1 >= CAST(:threshold AS INTEGER)"
    " THEN CAST(:now AS BIGINT) + CAST(:open AS BIGINT) ELSE 0 END)"
    " ON CONFLICT (venue) DO UPDATE SET"
    " consecutive_failures = venue_circuits.consecutive_failures + 1,"
    " open_until_ms = CASE"
    " WHEN venue_circuits.consecutive_failures + 1 >= CAST(:threshold AS INTEGER)"
    " THEN CAST(:now AS BIGINT) + CAST(:open AS BIGINT)"
    " ELSE venue_circuits.open_until_ms END"
    " RETURNING open_until_ms"
)
_SQL_SUCCESS = text(
    "UPDATE venue_circuits SET consecutive_failures = 0, open_until_ms = 0"
    " WHERE venue = :venue AND (consecutive_failures <> 0 OR open_until_ms <> 0)"
)
_SQL_OPEN_UNTIL = text("SELECT open_until_ms FROM venue_circuits WHERE venue = :venue")


class PostgresBuckets:
    """Buckets and breakers in the rate_buckets / venue_circuits tables, shared
    by every worker process. Each call runs in its own short transaction on
    its own connection, independent of the job's session, so a charge is
    never rolled back with a failed job (the venue counted the request)."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @staticmethod
    def _params(spec: BucketSpec, at_ms: int, **extra: int) -> dict:
        return {
            "key": spec.key,
            "cap": spec.capacity_milli,
            "rpm": spec.refill_per_minute,
            "now": at_ms,
            **extra,
        }

    def _update(self, stmt, spec: BucketSpec, at_ms: int, **extra: int) -> None:
        params = self._params(spec, at_ms, **extra)
        with self.engine.begin() as conn:
            if conn.execute(stmt, params).rowcount == 0:
                conn.execute(_SQL_ENSURE, params)
                conn.execute(stmt, params)

    def take(self, spec: BucketSpec, cost_milli: int, at_ms: int) -> int:
        params = self._params(spec, at_ms, cost=cost_milli)
        with self.engine.begin() as conn:
            for _ in range(2):
                if conn.execute(_SQL_TAKE, params).first() is not None:
                    return 0
                row = conn.execute(_SQL_STATE, params).first()
                if row is not None:
                    return _refusal_wait_ms(BucketState(*row), spec, cost_milli, at_ms)
                conn.execute(_SQL_ENSURE, params)  # first use: a full bucket
        raise RuntimeError(f"rate bucket {spec.key} could not be created")  # pragma: no cover

    def charge(self, spec: BucketSpec, cost_milli: int, at_ms: int) -> None:
        self._update(_SQL_CHARGE, spec, at_ms, cost=cost_milli)

    def cap_to_used(self, spec: BucketSpec, used_milli: int, at_ms: int) -> None:
        self._update(_SQL_CAP_USED, spec, at_ms, used=used_milli)

    def penalize(
        self, spec: BucketSpec, at_ms: int, penalty_ms: int, blocked_until_ms: int
    ) -> None:
        self._update(_SQL_PENALIZE, spec, at_ms, penalty=penalty_ms, blocked=blocked_until_ms)

    def state(self, spec: BucketSpec, at_ms: int) -> BucketState | None:
        with self.engine.connect() as conn:
            row = conn.execute(_SQL_STATE, {"key": spec.key}).first()
        if row is None:
            return None
        st = BucketState(*row)
        level = refilled_level(
            st.level_milli,
            st.updated_ms,
            at_ms,
            spec.capacity_milli,
            spec.refill_per_minute,
            st.penalty_until_ms,
        )
        return BucketState(level, at_ms, st.penalty_until_ms, st.blocked_until_ms)

    def record_failure(self, venue: str, at_ms: int, threshold: int, open_ms: int) -> int:
        with self.engine.begin() as conn:
            open_until = conn.execute(
                _SQL_FAIL, {"venue": venue, "now": at_ms, "threshold": threshold, "open": open_ms}
            ).scalar_one()
        return open_until if open_until > at_ms else 0

    def record_success(self, venue: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(_SQL_SUCCESS, {"venue": venue})

    def open_until_ms(self, venue: str) -> int:
        with self.engine.connect() as conn:
            value = conn.execute(_SQL_OPEN_UNTIL, {"venue": venue}).scalar()
        return value or 0


# ---- the limiter --------------------------------------------------------------


def _profile_budgets(venue: str) -> tuple[RateBudget, ...]:
    from alphamarket.connectors.registry import PROFILES

    profile = PROFILES.get(venue)
    return profile.rate_budgets if profile is not None else ()


class VenueLimiter:
    """Maps venues' RateBudgets to buckets and owns the circuit breakers.

    Bucket keys: "<venue>:cred:<credential_ref>:<i>" for CREDENTIAL-scoped
    budgets and "<venue>:ip:<egress_ip>:<i>" for IP-scoped ones, where i is
    the budget's position in the profile (a venue may enforce several)."""

    def __init__(
        self,
        backend: BucketBackend,
        egress_ip: str = "default",
        *,
        budgets_for: Callable[[str], tuple[RateBudget, ...]] = _profile_budgets,
        clock_ms: Callable[[], int] = now_ms,
        penalty_ms: int = PENALTY_MS,
        breaker_threshold: int = BREAKER_THRESHOLD,
        breaker_open_ms: int = BREAKER_OPEN_MS,
    ) -> None:
        self.backend = backend
        self.egress_ip = egress_ip
        self.budgets_for = budgets_for
        self.clock_ms = clock_ms
        self.penalty_ms = penalty_ms
        self.breaker_threshold = breaker_threshold
        self.breaker_open_ms = breaker_open_ms

    def buckets(self, venue: str, credential_id: str | None) -> list[tuple[RateBudget, BucketSpec]]:
        """The buckets one request spends. Credential buckets are skipped when
        there is no credential (public market data)."""
        out = []
        for i, budget in enumerate(self.budgets_for(venue)):
            if budget.scope is RateScope.CREDENTIAL:
                if credential_id is None:
                    continue
                key = f"{venue}:cred:{credential_id}:{i}"
            else:
                key = f"{venue}:ip:{self.egress_ip}:{i}"
            out.append((budget, BucketSpec(key, budget.capacity * MILLI, budget.refill_per_minute)))
        return out

    def acquire(self, venue: str, credential_id: str | None, endpoint: str) -> None:
        """Charge `endpoint`'s cost to every bucket or raise BudgetExhausted
        (refunding any bucket already charged, so a refusal spends nothing)."""
        at = self.clock_ms()
        taken: list[tuple[BucketSpec, int]] = []
        for budget, spec in self.buckets(venue, credential_id):
            cost = min(budget.cost(endpoint), budget.capacity) * MILLI
            if cost <= 0:
                continue
            wait = self.backend.take(spec, cost, at)
            if wait:
                for done_spec, done_cost in taken:
                    self.backend.charge(done_spec, -done_cost, at)
                raise BudgetExhausted(wait / 1000, endpoint)
            taken.append((spec, cost))

    def settle(
        self,
        venue: str,
        credential_id: str | None,
        endpoint: str,
        extra_cost: int = 0,
        used: int | None = None,
    ) -> None:
        """Throttle.after(): charge response-dependent cost and/or sync to the
        venue's reported usage, on every bucket that charges `endpoint`."""
        at = self.clock_ms()
        for budget, spec in self.buckets(venue, credential_id):
            if budget.cost(endpoint) <= 0:
                continue
            if extra_cost:
                self.backend.charge(spec, extra_cost * MILLI, at)
            if used is not None:
                self.backend.cap_to_used(spec, min(used, budget.capacity) * MILLI, at)

    def on_rate_limited(
        self,
        venue: str,
        credential_id: str | None,
        retry_after_seconds: float,
        endpoint: str = "",
    ) -> None:
        """The venue said 429/418: halve refill for 10 minutes and block every
        bucket this caller spends until Retry-After."""
        at = self.clock_ms()
        blocked = at + max(0, int(retry_after_seconds * 1000))
        for _budget, spec in self.buckets(venue, credential_id):
            self.backend.penalize(spec, at, self.penalty_ms, blocked)
        logger.warning(
            "%s rate limited on %s: refill halved for %ds, blocked %.1fs",
            venue,
            endpoint or "venue",
            self.penalty_ms // 1000,
            retry_after_seconds,
        )

    def throttle(self, venue: str, credential_id: str | None) -> "VenueThrottle":
        return VenueThrottle(self, venue, credential_id)

    # ---- circuit breaker ----

    def breaker_open_until(self, venue: str) -> datetime | None:
        until = self.backend.open_until_ms(venue)
        return ms_to_datetime(until) if until > self.clock_ms() else None

    def record_failure(self, venue: str) -> datetime | None:
        """One more consecutive 5xx/timeout. Returns when the circuit reopens
        if this failure opened it (or it was already open), else None."""
        at = self.clock_ms()
        until = self.backend.record_failure(venue, at, self.breaker_threshold, self.breaker_open_ms)
        if until:
            logger.warning("%s circuit open until %s", venue, ms_to_datetime(until).isoformat())
            return ms_to_datetime(until)
        return None

    def record_success(self, venue: str) -> None:
        self.backend.record_success(venue)


class VenueThrottle:
    """connectors.base.Throttle for one (venue, credential)."""

    def __init__(self, limiter: VenueLimiter, venue: str, credential_id: str | None) -> None:
        self.limiter = limiter
        self.venue = venue
        self.credential_id = credential_id

    def before(self, endpoint: str) -> None:
        self.limiter.acquire(self.venue, self.credential_id, endpoint)

    def after(self, endpoint: str, extra_cost: int = 0, used: int | None = None) -> None:
        self.limiter.settle(self.venue, self.credential_id, endpoint, extra_cost, used)


def build_limiter(settings, engine: Engine) -> VenueLimiter:
    backend: BucketBackend = (
        MemoryBuckets() if settings.rate_limiter_backend == "memory" else PostgresBuckets(engine)
    )
    return VenueLimiter(backend, settings.egress_ip)


# ---- classifying venue errors ---------------------------------------------------


def is_venue_outage(exc: BaseException) -> bool:
    """5xx and transport failures count toward the circuit breaker; 4xx do not
    (they are this account's problem, not the venue's)."""
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500


def rate_limit_from_http(exc: BaseException) -> RateLimited | None:
    """Connectors written before contract v2 raise httpx errors on 429/418;
    treat them as RateLimited, honouring a numeric Retry-After header."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    if exc.response.status_code not in (418, 429):
        return None
    retry = DEFAULT_RETRY_AFTER_S
    header = exc.response.headers.get("retry-after")
    if header and header.strip().replace(".", "", 1).isdigit():
        retry = float(header)  # the HTTP-date form falls back to the default
    return RateLimited(retry, str(exc.request.url.path) if exc.request else "")
