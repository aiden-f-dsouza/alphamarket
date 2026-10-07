"""A failed SnapTrade deleteUser is retried until it succeeds or dead-letters
(implementation_plan section 8 follow-up; D20). Venue traffic is respx-mocked
(invariant 6). The user secret must never reach a job payload, an error or an
audit row (invariant 4)."""

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from sqlalchemy import select

import alphamarket.worker  # noqa: F401  (registers the AGGREGATOR_DELETE_USER handler)
from alphamarket import aggregator_jobs, credentials, pending, scopecheck
from alphamarket.config import get_settings
from alphamarket.connectors.base import RateBudget, RateScope, ReadOnlyEvidence, ReadOnlyProof
from alphamarket.connectors.snaptrade import BASE_URL as SNAP
from alphamarket.jobs import JobKind, JobRunner, backoff_seconds
from alphamarket.models import AuditLog, Job, JobStatus, User, VenueAccount, VenueAccountStatus
from alphamarket.venue_limits import MemoryBuckets, VenueLimiter
from authutil import secure_account
from test_connect_wave2 import SNAP_ACCOUNT_REF, _mock_snaptrade, _start_snaptrade

SECRET = "snap-user-secret-not-real"  # what _mock_snaptrade's registerUser hands out
DELETE_URL = f"{SNAP}/snapTrade/deleteUser"
KIND = JobKind.AGGREGATOR_DELETE_USER


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
        json={"email": "agg@example.com", "password": "hunter2hunter2", "display_name": "Agg"},
    )
    assert resp.status_code == 201
    secure_account(client, "agg@example.com")
    return resp.json()


class Clock:
    """The runner's clock; the queue and the limiter both read it."""

    def __init__(self) -> None:
        self.at = datetime.now(UTC) + timedelta(minutes=5)  # past the first retry delay

    def __call__(self) -> datetime:
        return self.at


def _runner(session_factory, clock, *, limiter=True) -> JobRunner:
    return JobRunner(
        session_factory,
        VenueLimiter(MemoryBuckets(), clock_ms=lambda: int(clock.at.timestamp() * 1000))
        if limiter
        else None,
        worker_id="w1",
        clock=clock,
        kinds=[KIND],
    )


def _jobs(session_factory) -> list[Job]:
    with session_factory() as db:
        return list(db.scalars(select(Job).where(Job.kind == KIND).order_by(Job.id)).all())


def _audit_rows(session_factory, account_id: int) -> list[AuditLog]:
    with session_factory() as db:
        return list(
            db.scalars(
                select(AuditLog)
                .where(
                    AuditLog.action == "aggregator.user_delete",
                    AuditLog.target_id == str(account_id),
                )
                .order_by(AuditLog.id)
            ).all()
        )


def _connect(client) -> int:
    """A finished (ACTIVE) SnapTrade connection."""
    account_id = _start_snaptrade(client)
    resp = client.post(
        f"/dashboard/connect/snaptrade/{account_id}",
        data={"account_ref": SNAP_ACCOUNT_REF, "strategy_name": "Robinhood swing"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    return account_id


def _seed_account(db, status, key_id="koine-seeded") -> VenueAccount:
    owner = User(email=f"{key_id}@example.com", password_hash="x", display_name="Seed")
    db.add(owner)
    db.flush()
    account = VenueAccount(
        user_id=owner.id, venue="snaptrade", venue_env="prod", key_id=key_id, status=status
    )
    db.add(account)
    db.flush()
    credentials.store(db, account, SECRET, kind="aggregator")
    db.commit()
    return account


# ---- a failed delete queues exactly one retry -----------------------------------------


@respx.mock
def test_failed_disconnect_delete_queues_one_retry_job(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    routes["delete"].mock(return_value=httpx.Response(503, json={"detail": "down"}))

    # The credential is erased and the account released whatever SnapTrade says.
    assert client.delete(f"/venues/{account_id}").status_code == 204
    assert routes["delete"].call_count == 1  # one immediate attempt

    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.PENDING and job.attempts == 0
    assert job.venue_account_id == account_id
    assert job.dedupe_key == f"aggregator_delete_user:{account_id}"
    assert job.priority == 5 and job.max_attempts == aggregator_jobs.DELETE_MAX_ATTEMPTS == 12
    # The first retry waits one backoff base (30 s), it does not repeat the failure at once.
    created = job.created_at.replace(tzinfo=UTC)
    assert job.run_after.replace(tzinfo=UTC) - created == timedelta(seconds=30)
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.DISCONNECTED
        assert credentials.active_credential(db, account) is None
        # A second request for the same account (another caller path, a double
        # click) queues nothing: the dedupe key allows one active job.
        assert aggregator_jobs.enqueue_delete(db, account, cause="user_disconnect") is None
        db.commit()
    assert len(_jobs(session_factory)) == 1
    (row,) = _audit_rows(session_factory, account_id)
    assert row.details["outcome"] == "retry_queued" and row.details["cause"] == "user_disconnect"


@respx.mock
def test_a_busy_limiter_hands_the_delete_to_the_job_without_calling_snaptrade(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    # One token, refilling at 1 a minute, already spent: the next call would
    # wait 60 s, far over the 2 s a web request may sleep.
    limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda v: (RateBudget(RateScope.IP, 1, 1),))
    limiter.acquire("snaptrade", None, "ListAccounts")
    client.app.state.venue_limiter = limiter
    assert client.delete(f"/venues/{account_id}").status_code == 204
    assert not routes["delete"].called  # fail fast: no request was sent
    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.PENDING
    (row,) = _audit_rows(session_factory, account_id)
    assert row.details["outcome"] == "retry_queued"


# ---- the retry ------------------------------------------------------------------------


@respx.mock
def test_the_retry_deletes_the_user_by_id_for_an_inactive_account(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    with session_factory() as db:
        user_id = db.get(VenueAccount, account_id).key_id
    routes["delete"].mock(return_value=httpx.Response(503))
    assert client.delete(f"/venues/{account_id}").status_code == 204
    assert routes["delete"].call_count == 1

    routes["delete"].mock(return_value=httpx.Response(200, json={"status": "deleted"}))
    clock = Clock()
    assert _runner(session_factory, clock).run_once() == 1

    # The account is DISCONNECTED, yet the job ran: it is exempt from the
    # runner's inactive-account skip. One more call, by user id, no secret.
    assert routes["delete"].call_count == 2
    q = parse_qs(urlsplit(str(routes["delete"].calls.last.request.url)).query)
    assert q["userId"] == [user_id] and "userSecret" not in q
    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.DONE and job.attempts == 0
    first, second = _audit_rows(session_factory, account_id)
    assert (first.details["outcome"], second.details["outcome"]) == ("retry_queued", "deleted")
    assert second.details["via"] == "retry_job"
    # Nothing left to do: a later run finds no job.
    assert _runner(session_factory, clock).run_once() == 0


@respx.mock
def test_user_not_found_on_a_retry_counts_as_done(client, user, session_factory, snaptrade_keys):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    routes["delete"].mock(return_value=httpx.Response(503))
    assert client.delete(f"/venues/{account_id}").status_code == 204

    routes["delete"].mock(return_value=httpx.Response(404, json={"detail": "User not found"}))
    assert _runner(session_factory, Clock()).run_once() == 1
    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.DONE and job.attempts == 0 and job.last_error is None
    assert _audit_rows(session_factory, account_id)[-1].details["outcome"] == "already_gone"


@respx.mock
def test_user_not_found_on_the_first_attempt_queues_nothing(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    routes["delete"].mock(return_value=httpx.Response(404, json={"detail": "not found"}))
    assert client.delete(f"/venues/{account_id}").status_code == 204
    assert _jobs(session_factory) == []
    (row,) = _audit_rows(session_factory, account_id)
    assert row.details["outcome"] == "already_gone"


@respx.mock
def test_a_rate_limited_retry_defers_without_counting_an_attempt(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    routes["delete"].mock(return_value=httpx.Response(503))
    client.delete(f"/venues/{account_id}")

    routes["delete"].mock(return_value=httpx.Response(429, headers={"Retry-After": "90"}))
    clock = Clock()
    assert _runner(session_factory, clock).run_once() == 1
    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.PENDING and job.attempts == 0 and job.deferrals == 1
    # Retry-After 90 s (plus the runner's per-job jitter, job id % 7).
    wait = (job.run_after.replace(tzinfo=UTC) - clock.at).total_seconds()
    assert 90 <= wait < 97


@respx.mock
def test_a_live_account_is_never_deleted_by_the_job(session_factory, snaptrade_keys):
    route = respx.delete(DELETE_URL).mock(return_value=httpx.Response(200, json={}))
    with session_factory() as db:
        account = _seed_account(db, VenueAccountStatus.ACTIVE)
        aggregator_jobs.enqueue_delete(db, account, cause="bug")
        db.commit()
    clock = Clock()
    assert _runner(session_factory, clock).run_once() == 1
    assert not route.called
    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.DONE


# ---- persistent failure ---------------------------------------------------------------


@respx.mock
def test_a_persistent_failure_backs_off_then_dead_letters(session_factory, snaptrade_keys):
    route = respx.delete(DELETE_URL).mock(return_value=httpx.Response(401, json={"detail": "bad"}))
    with session_factory() as db:
        account = _seed_account(db, VenueAccountStatus.DISCONNECTED)
        account_id = account.id
        aggregator_jobs.enqueue_delete(db, account, cause="user_disconnect")
        db.commit()
    runner_clock = Clock()
    runner = _runner(session_factory, runner_clock)
    settings = get_settings()

    delays = []
    for attempt in range(1, 13):
        (job,) = _jobs(session_factory)
        assert job.status == JobStatus.PENDING and job.attempts == attempt - 1
        runner_clock.at = max(runner_clock.at, job.run_after.replace(tzinfo=UTC)) + timedelta(
            seconds=1
        )
        failed_at = runner_clock.at
        assert runner.run_once() == 1
        (job,) = _jobs(session_factory)
        assert job.attempts == attempt
        if job.status == JobStatus.PENDING:
            delays.append((job.run_after.replace(tzinfo=UTC) - failed_at).total_seconds())

    # base 30 s x 2^(n-1) after failure n, capped at 3600: 30, 60, 120, 240, 480, 960,
    # 1920, then 30 x 128 = 3840 -> 3600 for the rest. 11 retries, the 12th failure is the last.
    assert delays == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600, 3600, 3600]
    assert delays == [
        backoff_seconds(
            n, settings.worker_backoff_base_seconds, settings.worker_backoff_cap_seconds
        )
        for n in range(1, 12)
    ]
    assert route.call_count == 12
    (job,) = _jobs(session_factory)
    assert job.status == JobStatus.DEAD and job.attempts == 12
    assert job.last_error.startswith("HTTPStatusError")
    # Still released locally: a dead letter does not turn the account into ERROR.
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.DISCONNECTED
    (row,) = _audit_rows(session_factory, account_id)
    assert row.details["outcome"] == "gave_up" and row.details["attempts"] == 12
    # A dead job is not picked up again.
    assert runner.run_once() == 0


@respx.mock
def test_an_outage_opens_the_circuit_and_defers_instead_of_burning_attempts(
    session_factory, snaptrade_keys
):
    route = respx.delete(DELETE_URL).mock(return_value=httpx.Response(503))
    with session_factory() as db:
        account = _seed_account(db, VenueAccountStatus.SUSPENDED)
        aggregator_jobs.enqueue_delete(db, account, cause="scope_writable")
        db.commit()
    clock = Clock()
    runner = _runner(session_factory, clock)
    for _ in range(8):
        (job,) = _jobs(session_factory)
        clock.at = max(clock.at, job.run_after.replace(tzinfo=UTC)) + timedelta(seconds=1)
        runner.run_once()
    (job,) = _jobs(session_factory)
    # Five consecutive 5xx open the circuit (BREAKER_THRESHOLD = 5). Failures 1-4
    # each counted an attempt; the 5th opened the circuit and was DEFERRED, and so
    # was every later probe (the circuit reopens on each failure): runs 5-8 are four
    # deferrals and attempts stay at 4, far from the 12 that would dead-letter.
    assert job.status == JobStatus.PENDING
    assert (job.attempts, job.deferrals) == (4, 4)
    assert route.call_count == 8


# ---- every caller path, and no secret anywhere ------------------------------------------


@respx.mock
def test_suspension_and_abandoned_connects_queue_the_retry_too(session_factory, snaptrade_keys):
    respx.delete(DELETE_URL).mock(return_value=httpx.Response(500))
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    with session_factory() as db:
        writable = _seed_account(db, VenueAccountStatus.ACTIVE, key_id="koine-writable")
        stale = _seed_account(db, VenueAccountStatus.PENDING, key_id="koine-stale")
        stale.created_at = now - timedelta(hours=25)
        db.commit()
        evidence = ReadOnlyEvidence(
            method=ReadOnlyProof.AGGREGATOR_FLAG, read_only=False, checked_at=now
        )
        scopecheck.suspend(db, writable, evidence)
        assert pending.abandon_stale(db, now) == 1
        assert writable.status == VenueAccountStatus.SUSPENDED
        assert stale.status == VenueAccountStatus.DISCONNECTED
        ids = {writable.id: "scope_writable", stale.id: "pending_abandoned"}
    jobs = _jobs(session_factory)
    assert {j.venue_account_id: j.payload["cause"] for j in jobs} == ids
    assert all(j.status == JobStatus.PENDING for j in jobs)


@respx.mock
def test_no_secret_in_a_payload_an_error_or_an_audit_row(
    client, user, session_factory, snaptrade_keys
):
    routes = _mock_snaptrade()
    account_id = _connect(client)
    routes["delete"].mock(return_value=httpx.Response(503))
    client.delete(f"/venues/{account_id}")
    routes["delete"].mock(return_value=httpx.Response(401))
    clock = Clock()
    runner = _runner(session_factory, clock)
    runner.run_once()  # a failed retry records last_error

    (job,) = _jobs(session_factory)
    assert job.payload == {"cause": "user_disconnect"}  # a label, nothing else
    assert job.last_error and job.attempts == 1
    blob = json.dumps(
        [job.payload, job.last_error]
        + [row.details for row in _audit_rows(session_factory, account_id)]
    )
    assert SECRET not in blob and "userSecret" not in blob
    assert "consumer-secret-key" not in blob
    with session_factory() as db:  # and the erased credential holds nothing either
        account = db.get(VenueAccount, account_id)
        assert credentials.active_credential(db, account) is None


def test_the_kind_fits_the_column_and_has_a_priority():
    from alphamarket.jobs import DEFAULT_PRIORITY, RUNS_ON_INACTIVE_ACCOUNT

    assert len(KIND.value) <= 40  # jobs.kind is String(40)
    assert DEFAULT_PRIORITY[KIND] == 5
    assert str(KIND) in RUNS_ON_INACTIVE_ACCOUNT
