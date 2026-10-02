"""OAuth refresh in the worker (wave2 w2-account-security).

A fake provider's token endpoint is served by respx (invariant 6). Alpaca's
real tokens never expire, so the tests register a provider whose do, under
the registry's get_oauth_provider seam."""

import itertools
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

import alphamarket.worker  # noqa: F401  (registers the OAUTH_REFRESH handler)
from alphamarket import alerts, credentials, oauth, oauth_jobs
from alphamarket.config import get_settings
from alphamarket.connectors import registry
from alphamarket.ingest import connector_for
from alphamarket.jobs import JobKind, JobRunner, JobStatus, enqueue
from alphamarket.models import AuditLog, Job, User, VenueAccount, VenueAccountStatus
from alphamarket.oauth import OAuthProvider

TOKEN_URL = "https://auth.fake-venue.test/oauth/token"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
_SEQ = itertools.count(1)


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    provider = OAuthProvider(
        venue="alpaca",
        authorize_url="https://auth.fake-venue.test/authorize",
        token_url=TOKEN_URL,
        revoke_url=None,
        client_id="cid",
        client_secret="csecret",
        forbidden_scopes=frozenset({"trading"}),
    )
    monkeypatch.setattr(registry, "get_oauth_provider", lambda venue: provider)
    return provider


@pytest.fixture()
def captured_alerts():
    seen: list[alerts.SecurityAlert] = []
    alerts.register(seen.append)
    yield seen
    alerts.unregister(seen.append)


def _record(expires_at: datetime | None, access="old-access", refresh="old-refresh") -> str:
    return json.dumps(
        {
            "access_token": access,
            "token_type": "bearer",
            "scope": "account:read",
            "refresh_token": refresh,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }
    )


def _seed(db, expires_at: datetime | None, status=VenueAccountStatus.ACTIVE) -> int:
    n = next(_SEQ)
    user = User(email=f"o{n}@x.com", password_hash="x", display_name="Owner")
    db.add(user)
    db.flush()
    account = VenueAccount(
        user_id=user.id,
        venue="alpaca",
        venue_env=get_settings().venue_env("alpaca"),
        key_id=f"acct-{n}",
        status=VenueAccountStatus.ACTIVE,
    )
    db.add(account)
    db.flush()
    credentials.store(db, account, _record(expires_at), kind="oauth2", expires_at=expires_at)
    account.status = status
    db.commit()
    return account.id


def _token_ok(route_calls: list | None = None):
    def respond(request: httpx.Request) -> httpx.Response:
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "old-refresh"
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "token_type": "bearer",
            },
        )

    return respx.post(TOKEN_URL).mock(side_effect=respond)


def _runner(session_factory, now=NOW):
    return JobRunner(session_factory, None, clock=lambda: now, kinds=[JobKind.OAUTH_REFRESH])


def _refresh_rows(db, account_id):
    return db.scalars(
        select(AuditLog)
        .where(AuditLog.action == "credential.refresh", AuditLog.target_id == str(account_id))
        .order_by(AuditLog.id)
    ).all()


def test_tick_queues_only_active_oauth_accounts_inside_twice_the_margin(session_factory):
    margin = oauth.REFRESH_MARGIN
    with session_factory() as db:
        due = _seed(db, NOW + 2 * margin)  # exactly on the horizon: due
        _seed(db, NOW + 2 * margin + timedelta(seconds=1))  # just outside
        _seed(db, None)  # Alpaca-style token that never expires
        _seed(db, NOW + timedelta(minutes=1), status=VenueAccountStatus.SUSPENDED)
        _seed(db, NOW + timedelta(minutes=1), status=VenueAccountStatus.ERROR)
        expired = _seed(db, NOW - timedelta(hours=1))  # already expired: still refreshable
    assert oauth_jobs.tick(session_factory, now=NOW) == 2
    assert oauth_jobs.tick(session_factory, now=NOW) == 0  # one live job per account
    with session_factory() as db:
        jobs = db.scalars(select(Job).order_by(Job.venue_account_id)).all()
        assert [(j.kind, j.venue_account_id) for j in jobs] == [
            ("oauth_refresh", due),
            ("oauth_refresh", expired),
        ]
        assert all(j.priority == 5 for j in jobs)


@respx.mock
def test_a_token_inside_the_margin_is_refreshed_exactly_once(session_factory):
    route = _token_ok()
    with session_factory() as db:
        account_id = _seed(db, NOW + timedelta(minutes=3))
    assert oauth_jobs.tick(session_factory, now=NOW) == 1
    assert _runner(session_factory).run_once() == 1
    assert route.call_count == 1

    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        cred = credentials.active_credential(db, account)
        expires = cred.expires_at.replace(tzinfo=UTC)
        assert expires == NOW + timedelta(seconds=3600)
        opened = credentials.open_credential(db, account, purpose="test")
        record = json.loads(opened.secret)
        assert record["access_token"] == "new-access"
        assert record["refresh_token"] == "new-refresh"
        assert record["scope"] == "account:read"  # kept from the original grant
        (row,) = _refresh_rows(db, account_id)
        assert row.details["outcome"] == "refreshed" and row.details["trigger"] == "job"
        assert "new-access" not in json.dumps(row.details)
        assert db.scalar(select(Job.status)) == JobStatus.DONE

        # The lazy path finds nothing due now, and makes no call.
        assert oauth_jobs.refresh_if_due(db, account, opened, now=NOW) is None
    assert oauth_jobs.tick(session_factory, now=NOW) == 0  # 1 h out: not due
    # A second job that was already queued finds the token fresh and stops.
    with session_factory() as db:
        enqueue(db, JobKind.OAUTH_REFRESH, account_id=account_id, now=NOW)
        db.commit()
    assert _runner(session_factory).run_once() == 1
    assert route.call_count == 1


@respx.mock
def test_invalid_grant_puts_the_account_in_error_and_alerts_once(session_factory, captured_alerts):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"})
    )
    with session_factory() as db:
        account_id = _seed(db, NOW + timedelta(minutes=2))
    oauth_jobs.tick(session_factory, now=NOW)
    _runner(session_factory).run_once()
    assert route.call_count == 1
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.ERROR
        assert account.last_sync_error.startswith("Reconnect Alpaca")
        (row,) = _refresh_rows(db, account_id)
        assert row.details == {
            "trigger": "job",
            "venue": "alpaca",
            "outcome": "refused",
            "error": "invalid_grant",
        }
        assert db.scalar(select(Job.status)) == JobStatus.DONE  # no pointless retries
    assert [a.kind for a in captured_alerts] == ["oauth_reconnect"]
    assert captured_alerts[0].user_id is not None

    # The tick no longer picks it up, and a sync's lazy path fails with the
    # reconnect message without a second alert.
    assert oauth_jobs.tick(session_factory, now=NOW) == 0
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        opened = credentials.open_credential(db, account, purpose="sync")
        with pytest.raises(oauth_jobs.ReconnectRequired, match="Reconnect Alpaca"):
            oauth_jobs.refresh_if_due(db, account, opened, now=NOW)
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.ERROR
    assert len(captured_alerts) == 1


@respx.mock
def test_a_5xx_from_the_token_endpoint_is_retried_not_a_reconnect(session_factory, captured_alerts):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))
    with session_factory() as db:
        account_id = _seed(db, NOW + timedelta(minutes=2))
    oauth_jobs.tick(session_factory, now=NOW)
    _runner(session_factory).run_once()
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.ACTIVE
        job = db.scalars(select(Job)).one()
        assert job.status == JobStatus.PENDING and job.attempts == 1
    assert captured_alerts == []


@respx.mock
def test_sync_path_refreshes_a_due_token_before_building_the_connector(session_factory):
    route = _token_ok()
    with session_factory() as db:
        # Inside the 5 min margin, whatever the jitter: always due.
        account_id = _seed(db, datetime.now(UTC) + timedelta(seconds=30))
        account = db.get(VenueAccount, account_id)
        connector = connector_for(account, db, purpose="sync")
        assert connector is not None
        assert route.call_count == 1
        cred = credentials.active_credential(db, account)
        assert cred.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(minutes=55)
        (row,) = _refresh_rows(db, account_id)
        assert row.details["trigger"] == "sync"
        # A second build reads the stored fresh token: no second refresh.
        connector_for(db.get(VenueAccount, account_id), db, purpose="sync")
    assert route.call_count == 1


@respx.mock
def test_refresh_that_grants_a_write_scope_is_a_reconnect(session_factory, captured_alerts):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "x", "expires_in": 3600, "scope": "account:read trading"}
        )
    )
    with session_factory() as db:
        account_id = _seed(db, NOW + timedelta(minutes=2))
    oauth_jobs.tick(session_factory, now=NOW)
    _runner(session_factory).run_once()
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.ERROR
        cred = credentials.active_credential(db, account)
        opened = credentials.open_credential(db, account, purpose="test")
        assert json.loads(opened.secret)["access_token"] == "old-access"  # never stored
        assert cred.expires_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=2)
    assert len(captured_alerts) == 1
