"""Nightly housekeeping (wave2 w2-account-security): deletes exactly the
seeded stale rate-limit windows, dead sessions, spent tokens, old quotes and
old finished jobs, and nothing fresh or append-only.

Retention used (config defaults): sessions 30 d after they died, tokens 7 d
after use or expiry, quotes 30 d, sessions idle 24 h / absolute 30 d,
password-only (mfa_pending) sessions 10 min. Each "old" row is 1 minute to
1 day past its cutoff and each "fresh" twin the same distance inside it.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import alphamarket.worker  # noqa: F401  (registers the HOUSEKEEPING handler)
from alphamarket import housekeeping
from alphamarket.config import get_settings
from alphamarket.jobs import JobKind, JobRunner, JobStatus, enqueue
from alphamarket.models import (
    AssetQuote,
    AuditLog,
    AuthToken,
    Instrument,
    Job,
    RateLimitCounter,
    User,
    UserSession,
)

NOW = datetime(2026, 10, 1, 4, 15, tzinfo=UTC)
D = timedelta(days=1)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def seed(db) -> dict[str, dict[str, set]]:
    """Rows in every pruned table, tagged "old" (must go) or "fresh" (must stay)."""
    tag = uuid.uuid4().hex[:8]
    user = User(email=f"hk-{tag}@example.com", password_hash="x", display_name="HK")
    db.add(user)
    db.flush()
    out: dict[str, dict[str, set]] = {}

    def add(table, kind, row, key):
        db.add(row)
        db.flush()
        out.setdefault(table, {"old": set(), "fresh": set()})[kind].add(key(row))

    def sess(kind, created, seen, revoked=None, pending=False):
        row = UserSession(
            token_hash=uuid.uuid4().hex,
            user_id=user.id,
            created_at=created,
            last_seen_at=seen,
            revoked_at=revoked,
            mfa_pending=pending,
        )
        add("sessions", kind, row, lambda r: r.id)

    # Revoked more than 30 d ago / less.
    # (Seen recently enough before that that idleness did not end them first.)
    sess("old", NOW - 32 * D, NOW - 31 * D - H, revoked=NOW - 31 * D)
    sess("fresh", NOW - 30 * D, NOW - 29 * D - H, revoked=NOW - 29 * D)
    # Idle-expired (last seen + 24 h) more than 30 d ago / less.
    sess("old", NOW - 32 * D, NOW - 31 * D - 1 * H)
    sess("fresh", NOW - 30 * D, NOW - 30 * D - 1 * H + 2 * H)
    # Past the 30 d absolute limit (created + 30 d) more than 30 d ago / less,
    # though seen an hour ago.
    sess("old", NOW - 61 * D, NOW - H)
    sess("fresh", NOW - 59 * D, NOW - H)
    # Password-only session: dies 10 min after creation.
    sess("old", NOW - 30 * D - 11 * M, NOW - 30 * D - 11 * M, pending=True)
    sess("fresh", NOW - 30 * D - 9 * M, NOW - 30 * D - 9 * M, pending=True)
    sess("fresh", NOW - D, NOW - H)  # live

    def token(kind, expires, used=None):
        row = AuthToken(
            user_id=user.id,
            kind="verify_email",
            token_hash=uuid.uuid4().hex,
            created_at=expires - 2 * D,
            expires_at=expires,
            used_at=used,
        )
        add("auth_tokens", kind, row, lambda r: r.id)

    token("old", NOW + D, used=NOW - 8 * D)  # used 8 d ago
    token("fresh", NOW + D, used=NOW - 6 * D)
    token("old", NOW - 8 * D)  # expired unused 8 d ago
    token("fresh", NOW - 6 * D)
    token("fresh", NOW + H)  # live

    def counter(kind, expires):
        row = RateLimitCounter(
            key=f"hk:{tag}:{kind}:{expires.timestamp()}",
            window_start=int(expires.timestamp()) - 60,
            count=3,
            expires_at=expires,
        )
        add("rate_limit_counters", kind, row, lambda r: r.key)

    counter("old", NOW - M)
    counter("fresh", NOW + M)

    inst = Instrument(
        venue="kraken",
        symbol=f"HK{tag}/USD",
        type="spot",
        base_asset="HK",
        quote_asset="USD",
        qty_decimals=8,
    )
    db.add(inst)
    db.flush()
    for kind, as_of in (("old", NOW - 31 * D), ("fresh", NOW - 29 * D), ("fresh", NOW - M)):
        add(
            "asset_quotes",
            kind,
            AssetQuote(instrument_id=inst.id, bid="1.0", ask="1.1", as_of=as_of, source="kraken"),
            lambda r: r.id,
        )

    def job(kind, status, done_at):
        row = Job(
            kind="prices",
            dedupe_key=f"hk:{tag}:{uuid.uuid4().hex}",
            payload={},
            priority=30,
            status=status,
            done_at=done_at,
            created_at=NOW - 40 * D,
            updated_at=NOW - 40 * D,
        )
        add("jobs", kind, row, lambda r: r.id)

    job("old", JobStatus.DONE, NOW - 8 * D)  # done jobs are kept 7 d
    job("fresh", JobStatus.DONE, NOW - 6 * D)
    job("old", JobStatus.DEAD, NOW - 31 * D)  # dead ones 30 d
    job("fresh", JobStatus.DEAD, NOW - 29 * D)
    db.add(AuditLog(action="hk.test", details={}, at=NOW - 400 * D))  # append-only, ancient
    db.commit()
    return out


_KEYS = {
    "sessions": (UserSession, UserSession.id),
    "auth_tokens": (AuthToken, AuthToken.id),
    "rate_limit_counters": (RateLimitCounter, RateLimitCounter.key),
    "asset_quotes": (AssetQuote, AssetQuote.id),
    "jobs": (Job, Job.id),
}


def surviving(db, seeded) -> dict[str, dict[str, set]]:
    out = {}
    for table, kinds in seeded.items():
        _, col = _KEYS[table]
        present = set(db.scalars(select(col).where(col.in_(kinds["old"] | kinds["fresh"]))))
        out[table] = {"old": kinds["old"] & present, "fresh": kinds["fresh"] & present}
    return out


def _ctx(db):
    return SimpleNamespace(db=db, clock=lambda: NOW, settings=get_settings())


def test_housekeeping_deletes_exactly_the_stale_rows(session_factory):
    with session_factory() as db:
        seeded = seed(db)
        audits = db.scalar(select(func.count()).select_from(AuditLog))
        counts = housekeeping.run(_ctx(db))
        assert counts == {
            "rate_limit_counters": 1,
            "sessions": 4,
            "auth_tokens": 2,
            "asset_quotes": 1,
            "pending_accounts": 0,
            "jobs": 2,
        }
        left = surviving(db, seeded)
        for table, kinds in left.items():
            assert kinds["old"] == set(), table
            assert kinds["fresh"] == seeded[table]["fresh"], table
        assert db.scalar(select(func.count()).select_from(AuditLog)) == audits
        assert set(housekeeping.run(_ctx(db)).values()) == {0}  # idempotent


def test_housekeeping_runs_as_the_nightly_job(session_factory):
    with session_factory() as db:
        seeded = seed(db)
        enqueue(db, JobKind.HOUSEKEEPING, dedupe_key="housekeeping:2026-10-01", once=True, now=NOW)
        db.commit()
    runner = JobRunner(session_factory, None, clock=lambda: NOW, kinds=[JobKind.HOUSEKEEPING])
    assert runner.run_once() == 1
    with session_factory() as db:
        left = surviving(db, seeded)
        assert all(not kinds["old"] for kinds in left.values())
        assert all(kinds["fresh"] == seeded[t]["fresh"] for t, kinds in left.items())
        hk = db.scalars(select(Job).where(Job.kind == "housekeeping")).one()
        assert hk.status == JobStatus.DONE


def test_retention_settings_are_honoured(session_factory):
    settings = get_settings()
    original = settings.token_retention_days
    settings.token_retention_days = 9  # now only tokens dead 9+ days go
    try:
        with session_factory() as db:
            seeded = seed(db)
            assert housekeeping.run(_ctx(db))["auth_tokens"] == 0
            left = surviving(db, seeded)["auth_tokens"]
            assert left["old"] == seeded["auth_tokens"]["old"]
    finally:
        settings.token_retention_days = original


@pytest.mark.pg
def test_housekeeping_on_postgres(pg_session_factory):
    """Same rules against real timestamptz columns; other tests share this
    database, so only the seeded rows are checked."""
    with pg_session_factory() as db:
        seeded = seed(db)
        housekeeping.run(_ctx(db))
        left = surviving(db, seeded)
        for table, kinds in left.items():
            assert kinds["old"] == set(), table
            assert kinds["fresh"] == seeded[table]["fresh"], table


def test_a_dormant_users_newest_session_is_kept_for_the_new_sign_in_alert(session_factory):
    """Every session of this user died long ago. All but the newest full one
    go; that one stays so sessions.has_prior_sessions still sees the user has
    signed in before, and a login from a new IP is still alerted."""
    from alphamarket import sessions

    with session_factory() as db:
        user = User(email="dormant@example.com", password_hash="x", display_name="D")
        db.add(user)
        db.flush()
        ids = []
        for created, pending in ((NOW - 200 * D, False), (NOW - 100 * D, False)):
            row = UserSession(
                token_hash=uuid.uuid4().hex,
                user_id=user.id,
                created_at=created,
                last_seen_at=created,
                mfa_pending=pending,
                ip="203.0.113.7",
            )
            db.add(row)
            db.flush()
            ids.append(row.id)
        pending_row = UserSession(
            token_hash=uuid.uuid4().hex,
            user_id=user.id,
            created_at=NOW - 90 * D,
            last_seen_at=NOW - 90 * D,
            mfa_pending=True,
        )
        db.add(pending_row)
        db.commit()
        pending_id = pending_row.id

        assert housekeeping.prune_sessions(db, NOW, timedelta(days=30)) == 2
        left = set(db.scalars(select(UserSession.id).where(UserSession.user_id == user.id)))
        assert left == {ids[1]}  # the newest full session, not the newer pending one
        assert pending_id not in left
        assert sessions.has_prior_sessions(db, user.id)
        assert not sessions.seen_ip_before(db, user.id, "198.51.100.1")
