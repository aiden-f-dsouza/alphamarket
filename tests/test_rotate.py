"""Replace a venue key (wave2 w2-account-security, plan D17).

Kraken is served by a respx double (invariant 6): Balance, TradesHistory and
Ledgers answer; AddOrder(validate) and WithdrawMethods are refused for a
read-only key and accepted for a writable one. Accounts are seeded through
the ORM, never through POST /venues.
"""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

import httpx
import pytest
import respx
from sqlalchemy import func, select

from alphamarket import credentials
from alphamarket.connectors.kraken import KRAKEN_API_URL
from alphamarket.email import ConsoleEmailSender, set_sender
from alphamarket.models import (
    AnchorBalance,
    AuditLog,
    Job,
    Strategy,
    User,
    UserSession,
    VenueAccount,
    VenueAccountStatus,
    VenueCredential,
)
from authutil import code, secure_account

EMAIL = "rot@example.com"
OLD_KEY = "old-kraken-key"
NEW_KEY = "new-kraken-key"
# Kraken's published signing example secret (base64, so the connector signs).
NEW_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5nE9qa99HAZtuZuj6F1huXg=="
)
DENIED = {"error": ["EGeneral:Permission denied"]}
ANCHOR_AT = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


@pytest.fixture()
def member(client):
    resp = client.post(
        "/auth/signup",
        json={"email": EMAIL, "password": "hunter2hunter2", "display_name": "Rotator"},
    )
    assert resp.status_code == 201
    secret = secure_account(client, EMAIL)
    return {"id": resp.json()["id"], "totp": secret}


def _seed_kraken(session_factory, user_id: int, status=VenueAccountStatus.SUSPENDED) -> int:
    """A Kraken account as the nightly re-check leaves a suspended one: its
    credential erased and revoked, the anchor vector intact."""
    with session_factory() as db:
        account = VenueAccount(
            user_id=user_id,
            venue="kraken",
            venue_env="prod",
            key_id=OLD_KEY,
            status=VenueAccountStatus.ACTIVE,
            anchor_balance_micros=1_000_000_000,
            anchor_at=ANCHOR_AT,
            claimed_at=ANCHOR_AT,
        )
        db.add(account)
        db.flush()
        db.add(
            AnchorBalance(
                venue_account_id=account.id, asset="USD", atoms=100_000, decimals=2, as_of=ANCHOR_AT
            )
        )
        db.add(Strategy(user_id=user_id, venue_account_id=account.id, name="K", slug="k-1"))
        credentials.store(db, account, "old-secret-value", kind="key_secret")
        if status == VenueAccountStatus.SUSPENDED:
            credentials.revoke(db, account, reason="scope_writable")
            account.last_sync_error = "Suspended: this key can now trade or withdraw."
        account.status = status
        db.commit()
        return account.id


class FakeKraken:
    def __init__(self, writable: bool = False, bad_key: bool = False):
        self.writable, self.bad_key = writable, bad_key
        self.methods: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["API-Key"] == NEW_KEY
        method = request.url.path.rsplit("/", 1)[-1]
        self.methods.append(method)
        params = dict(parse_qsl(request.content.decode()))
        if self.bad_key:
            return httpx.Response(200, json={"error": ["EAPI:Invalid key"]})
        if method == "Balance":
            return httpx.Response(200, json={"error": [], "result": {"ZUSD": "1000.0000"}})
        if method in ("TradesHistory", "Ledgers"):
            key = "trades" if method == "TradesHistory" else "ledger"
            return httpx.Response(200, json={"error": [], "result": {key: {}, "count": 0}})
        if method == "AddOrder":
            assert params["validate"] == "true"  # never a real order
            if self.writable:
                return httpx.Response(200, json={"error": [], "result": {"descr": {}}})
            return httpx.Response(200, json=DENIED)
        if method == "WithdrawMethods":
            return httpx.Response(200, json=DENIED)
        return httpx.Response(200, json={"error": [f"EGeneral:Unknown method {method}"]})


def _mock(fake: FakeKraken) -> FakeKraken:
    respx.post(url__regex=rf"{KRAKEN_API_URL}/0/private/\w+").mock(side_effect=fake)
    return fake


def _rotate(client, account_id, key=NEW_KEY, secret=NEW_SECRET):
    return client.post(f"/venues/{account_id}/rotate", json={"key_id": key, "secret": secret})


def _audit(db, action, account_id=None):
    stmt = select(AuditLog).where(AuditLog.action == action)
    if account_id is not None:
        stmt = stmt.where(AuditLog.target_id == str(account_id))
    return db.scalars(stmt.order_by(AuditLog.id)).all()


@respx.mock
def test_rotating_a_suspended_kraken_account(client, member, session_factory, outbox):
    account_id = _seed_kraken(session_factory, member["id"])
    with session_factory() as db:
        (old,) = db.scalars(select(VenueCredential)).all()
        anchors_before = db.scalar(select(func.count()).select_from(AnchorBalance))
    fake = _mock(FakeKraken())

    resp = _rotate(client, account_id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active" and body["previous_status"] == "suspended"
    assert body["last_sync_error"] is None
    assert NEW_SECRET not in resp.text and NEW_KEY not in resp.text  # no key material back
    # check_connection, then the read-only proof's five probes.
    assert fake.methods == [
        "Balance",
        "Balance",
        "TradesHistory",
        "Ledgers",
        "AddOrder",
        "WithdrawMethods",
    ]
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.ACTIVE
        assert account.key_id == NEW_KEY
        assert account.scope_checked_at is not None
        assert account.scope_evidence["read_only"] is True
        # The anchor is untouched: no re-anchor, no new anchor rows.
        assert account.anchor_balance_micros == 1_000_000_000
        assert db.scalar(select(func.count()).select_from(AnchorBalance)) == anchors_before
        creds = db.scalars(select(VenueCredential).order_by(VenueCredential.id)).all()
        assert [c.id for c in creds][0] == old.id and len(creds) == 2
        assert creds[0].revoked_at is not None and creds[0].ciphertext is None  # old erased
        new = credentials.active_credential(db, account)
        assert new.id == creds[1].id and new.ciphertext is not None
        assert credentials.open_credential(db, account, purpose="test").secret == NEW_SECRET

        jobs = db.scalars(select(Job).where(Job.venue_account_id == account_id)).all()
        by_kind = {j.kind: j for j in jobs}
        assert by_kind["sync"].priority == 5  # urgent
        assert by_kind["sync"].id == body["sync_job_id"]
        assert by_kind["reconcile"].dedupe_key.startswith(f"reconcile:{account_id}:rotate:")
        assert by_kind["reconcile"].id == body["reconcile_job_id"]
        assert "backfill" in by_kind  # the interrupted backfill is queued again

        (scope,) = _audit(db, "scope.check", account_id)
        assert scope.details["trigger"] == "rotate" and scope.actor_user_id == member["id"]
        (rotate,) = _audit(db, "credential.rotate", account_id)
        assert rotate.details["replaced_credential_id"] is None  # already erased by suspend
        assert rotate.details["previous_status"] == "suspended"
        assert rotate.details["credential_id"] == new.id
        assert len(_audit(db, "credential.connect", account_id)) == 2  # seed + rotate
    alert = outbox.last_to(EMAIL)
    assert alert.subject == "A venue key on your Koine account was replaced"
    assert NEW_SECRET not in alert.body and "Kraken" in alert.body


@respx.mock
def test_rotating_an_active_account_erases_the_old_key(client, member, session_factory):
    account_id = _seed_kraken(session_factory, member["id"], status=VenueAccountStatus.ACTIVE)
    _mock(FakeKraken())
    assert _rotate(client, account_id).status_code == 200
    with session_factory() as db:
        old, new = db.scalars(select(VenueCredential).order_by(VenueCredential.id)).all()
        assert old.revoked_at is not None and old.ciphertext is None and old.wrapped_dek is None
        assert new.revoked_at is None
        (rotate,) = _audit(db, "credential.rotate", account_id)
        assert rotate.details["replaced_credential_id"] == old.id


@respx.mock
def test_rotate_through_a_busy_limiter_is_503_and_changes_nothing(client, member, session_factory):
    from alphamarket.connectors.base import RateBudget, RateScope
    from alphamarket.venue_limits import MemoryBuckets, VenueLimiter

    account_id = _seed_kraken(session_factory, member["id"], status=VenueAccountStatus.ACTIVE)
    fake = FakeKraken()
    _mock(fake)
    drained = VenueLimiter(
        MemoryBuckets(),
        budgets_for=lambda venue: (RateBudget(RateScope.IP, 1, 1),),  # 1 token a minute
    )
    drained.acquire("kraken", None, "Balance")
    client.app.state.venue_limiter = drained

    resp = _rotate(client, account_id)
    assert resp.status_code == 503 and "Kraken is busy" in resp.json()["detail"]
    assert int(resp.headers["Retry-After"]) >= 1
    assert fake.methods == []  # nothing was sent to Kraken
    with session_factory() as db:
        (old,) = db.scalars(select(VenueCredential)).all()  # no new credential
        assert old.revoked_at is None and old.ciphertext is not None
        assert _audit(db, "credential.rotate", account_id) == []

    # The same request goes through once the limiter has room.
    client.app.state.venue_limiter = VenueLimiter(MemoryBuckets(), budgets_for=lambda venue: ())
    assert _rotate(client, account_id).status_code == 200


@respx.mock
def test_writable_key_is_refused_and_the_account_stays_suspended(
    client, member, session_factory, outbox
):
    account_id = _seed_kraken(session_factory, member["id"])
    _mock(FakeKraken(writable=True))
    sent_before = len(outbox.outbox)

    resp = _rotate(client, account_id)

    assert resp.status_code == 422
    assert "read-only" in resp.json()["detail"]
    with session_factory() as db:
        account = db.get(VenueAccount, account_id)
        assert account.status == VenueAccountStatus.SUSPENDED
        assert account.key_id == OLD_KEY
        assert credentials.active_credential(db, account) is None  # nothing stored
        assert db.scalar(select(func.count()).select_from(VenueCredential)) == 1
        assert db.scalars(select(Job)).all() == []
        (rejected,) = _audit(db, "credential.rejected", account_id)
        assert rejected.details["reason"] == "scope_writable"
        assert rejected.details["trigger"] == "rotate"
        assert _audit(db, "credential.rotate", account_id) == []
    assert len(outbox.outbox) == sent_before  # no "key replaced" alert


@respx.mock
def test_bad_key_is_refused_without_echoing_it(client, member, session_factory):
    account_id = _seed_kraken(session_factory, member["id"])
    _mock(FakeKraken(bad_key=True))
    resp = _rotate(client, account_id)
    assert resp.status_code == 422
    assert NEW_SECRET not in resp.text and NEW_KEY not in resp.text
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.SUSPENDED


def test_rotate_needs_a_fresh_step_up(client, member, session_factory):
    account_id = _seed_kraken(session_factory, member["id"])
    # Age the step-up out of its 10-minute window.
    with session_factory() as db:
        for sess in db.scalars(select(UserSession)).all():
            sess.step_up_at = datetime.now(UTC) - timedelta(minutes=11)
        db.commit()
    resp = _rotate(client, account_id)
    assert resp.status_code == 403
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.SUSPENDED


def test_rotate_another_users_account_is_404(client, member, session_factory):
    with session_factory() as db:
        other = User(email="other@example.com", password_hash="x", display_name="O")
        db.add(other)
        db.commit()
        other_id = other.id
    account_id = _seed_kraken(session_factory, other_id)
    assert _rotate(client, account_id).status_code == 404


@pytest.mark.parametrize(
    ("venue", "credential", "status", "expected"),
    [
        ("alpaca", "oauth2", VenueAccountStatus.ACTIVE, 422),
        ("snaptrade", "aggregator", VenueAccountStatus.ACTIVE, 422),
        ("hyperliquid", "address", VenueAccountStatus.ACTIVE, 422),
        ("kraken", "key_secret", VenueAccountStatus.DISCONNECTED, 409),
        ("kraken", "key_secret", VenueAccountStatus.PENDING, 409),
    ],
)
def test_only_key_accounts_in_a_live_state_rotate(
    client, member, session_factory, venue, credential, status, expected
):
    with session_factory() as db:
        account = VenueAccount(
            user_id=member["id"], venue=venue, venue_env="prod", key_id="k", status=status
        )
        db.add(account)
        db.commit()
        account_id = account.id
    resp = _rotate(client, account_id)
    assert resp.status_code == expected
    if expected == 422:
        assert "reconnect" in resp.json()["detail"].lower()


def test_rotate_is_rate_limited(client, member, session_factory):
    account_id = _seed_kraken(session_factory, member["id"], status=VenueAccountStatus.ACTIVE)
    with respx.mock:
        _mock(FakeKraken(bad_key=True))
        codes = [_rotate(client, account_id).status_code for _ in range(11)]
    assert codes[:10] == [422] * 10 and codes[10] == 429


# ---- HTML -------------------------------------------------------------------------


@respx.mock
def test_html_rotate_form(client, member, session_factory):
    account_id = _seed_kraken(session_factory, member["id"])
    page = client.get(f"/dashboard/accounts/{account_id}/rotate")
    assert page.status_code == 200
    assert "Replace the key." in page.text and "paused" in page.text
    assert f'action="/dashboard/accounts/{account_id}/rotate"' in page.text
    assert 'href="#"' not in page.text

    _mock(FakeKraken(writable=True))
    refused = client.post(
        f"/dashboard/accounts/{account_id}/rotate", data={"key_id": NEW_KEY, "secret": NEW_SECRET}
    )
    assert refused.status_code == 422 and "read-only" in refused.text
    assert NEW_SECRET not in refused.text

    respx.reset()
    _mock(FakeKraken())
    done = client.post(
        f"/dashboard/accounts/{account_id}/rotate",
        data={"key_id": NEW_KEY, "secret": NEW_SECRET},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == "/dashboard"
    with session_factory() as db:
        assert db.get(VenueAccount, account_id).status == VenueAccountStatus.ACTIVE


def test_html_rotate_without_step_up_shows_the_code_form(client, member, session_factory):
    account_id = _seed_kraken(session_factory, member["id"])
    with session_factory() as db:
        for sess in db.scalars(select(UserSession)).all():
            sess.step_up_at = None
        db.commit()
    page = client.get(f"/dashboard/accounts/{account_id}/rotate")
    assert "Confirm a code" in page.text
    assert f'name="next" value="/dashboard/accounts/{account_id}/rotate"' in page.text
    resp = client.post(
        f"/dashboard/accounts/{account_id}/rotate", data={"key_id": NEW_KEY, "secret": "s"}
    )
    assert resp.status_code == 403
    # Confirming a code there returns to the rotate page.
    step = client.post(
        "/dashboard/security/step-up",
        data={"code": code(member["totp"], 1), "next": f"/dashboard/accounts/{account_id}/rotate"},
        follow_redirects=False,
    )
    assert step.headers["location"] == f"/dashboard/accounts/{account_id}/rotate"


def test_html_rotate_page_for_an_oauth_account_points_to_reconnect(client, member, session_factory):
    with session_factory() as db:
        account = VenueAccount(
            user_id=member["id"], venue="alpaca", venue_env="paper", key_id="a", status="active"
        )
        db.add(account)
        db.commit()
        account_id = account.id
    page = client.get(f"/dashboard/accounts/{account_id}/rotate")
    assert "has no key to replace" in page.text
    assert 'name="secret"' not in page.text


def test_html_rotate_requires_login(client, session_factory):
    resp = client.get("/dashboard/accounts/1/rotate", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
