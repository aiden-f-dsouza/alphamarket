"""Email change (wave2 w2-account-security): password + step-up, a 1-hour
single-use link to the NEW address, an alert to the old one, the same answer
whether or not the address is taken, and a uniqueness re-check at confirm."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import sessions
from alphamarket.email import ConsoleEmailSender, set_sender
from alphamarket.models import AuditLog, AuthToken, User, UserSession
from authutil import code, last_token, secure_account

OLD = "old@example.com"
NEW = "new@example.com"
PASSWORD = "hunter2hunter2"


@pytest.fixture(autouse=True)
def outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


@pytest.fixture()
def member(client):
    resp = client.post(
        "/auth/signup", json={"email": OLD, "password": PASSWORD, "display_name": "Mover"}
    )
    assert resp.status_code == 201
    return {"id": resp.json()["id"], "totp": secure_account(client, OLD)}


def _request(client, new_email=NEW, password=PASSWORD):
    return client.post(
        "/account/email", json={"new_email": new_email, "current_password": password}
    )


def _other_session(session_factory, user_id: int) -> int:
    now = datetime.now(UTC)
    with session_factory() as db:
        sess = UserSession(
            token_hash=sessions.hash_token("another-device"),
            user_id=user_id,
            created_at=now,
            last_seen_at=now,
        )
        db.add(sess)
        db.commit()
        return sess.id


def test_happy_path(client, member, session_factory, outbox):
    other = _other_session(session_factory, member["id"])
    client.post("/auth/password-reset/request", json={"email": OLD})  # an outstanding token

    resp = _request(client)
    assert resp.status_code == 202
    link_mail = outbox.last_to(NEW)
    assert "/account/email/confirm?token=" in link_mail.body
    warn = outbox.last_to(OLD)
    assert warn.subject == "A change of your Koine email address was requested"
    assert "n***@example.com" in warn.body and "/account/email/confirm" not in warn.body
    with session_factory() as db:
        row = db.scalars(select(AuthToken).where(AuthToken.kind == "email_change")).one()
        assert row.new_email == NEW
        assert row.expires_at.replace(tzinfo=UTC) - row.created_at.replace(tzinfo=UTC) == timedelta(
            hours=1
        )
        assert db.get(User, member["id"]).email == OLD  # nothing changes yet

    token = last_token(NEW, "/account/email/confirm")
    page = client.get(f"/account/email/confirm?token={token}")
    assert "Email changed." in page.text

    me = client.get("/auth/me").json()  # this browser stays signed in
    assert me["email"] == NEW and me["email_verified"] is True
    with session_factory() as db:
        user = db.get(User, member["id"])
        assert user.email == NEW and user.email_verified_at is not None
        assert db.get(UserSession, other).revoked_at is not None
        assert all(
            t.used_at is not None
            for t in db.scalars(select(AuthToken).where(AuthToken.user_id == user.id))
        )
        (audit_row,) = db.scalars(
            select(AuditLog).where(AuditLog.action == "account.email_change")
        ).all()
        assert audit_row.actor_user_id == user.id
        assert audit_row.details == {"old_domain": "example.com", "new_domain": "example.com"}
        assert db.scalars(
            select(AuditLog).where(AuditLog.action == "account.email_change_request")
        ).one()
    done = outbox.last_to(OLD)
    assert done.subject == "Your Koine email address was changed"
    # Log in with the new address works; the old one is gone.
    client.post("/auth/logout")
    assert client.post("/auth/login", json={"email": OLD, "password": PASSWORD}).status_code == 401


def test_the_link_is_single_use(client, member, session_factory):
    _request(client)
    token = last_token(NEW, "/account/email/confirm")
    assert "Email changed." in client.get(f"/account/email/confirm?token={token}").text
    again = client.get(f"/account/email/confirm?token={token}")
    assert "cannot be used" in again.text


def test_taken_address_answers_the_same_and_sends_no_link(client, member, session_factory, outbox):
    free = _request(client, "free@example.com")
    with session_factory() as db:
        db.add(User(email="taken@example.com", password_hash="x", display_name="T"))
        db.commit()
    taken = _request(client, "taken@example.com")
    assert taken.status_code == free.status_code == 202
    assert taken.json() == free.json()
    notice = outbox.last_to("taken@example.com")
    assert "already has a Koine account" in notice.body
    assert "token=" not in notice.body
    with session_factory() as db:
        news = db.scalars(select(AuthToken.new_email).where(AuthToken.kind == "email_change"))
        assert list(news) == ["free@example.com"]


def test_address_taken_before_confirm_fails_and_spends_the_token(client, member, session_factory):
    _request(client)
    token = last_token(NEW, "/account/email/confirm")
    with session_factory() as db:
        db.add(User(email=NEW, password_hash="x", display_name="Fast"))
        db.commit()
    page = client.get(f"/account/email/confirm?token={token}")
    assert "cannot be used" in page.text
    with session_factory() as db:
        assert db.get(User, member["id"]).email == OLD
        row = db.scalars(select(AuthToken).where(AuthToken.kind == "email_change")).one()
        assert row.used_at is not None
        assert not db.scalars(
            select(AuditLog).where(AuditLog.action == "account.email_change")
        ).all()


def test_expired_link_fails(client, member, session_factory):
    _request(client)
    token = last_token(NEW, "/account/email/confirm")
    with session_factory() as db:
        row = db.scalars(select(AuthToken).where(AuthToken.kind == "email_change")).one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert "cannot be used" in client.get(f"/account/email/confirm?token={token}").text
    with session_factory() as db:
        assert db.get(User, member["id"]).email == OLD


def test_wrong_password_and_missing_step_up_are_refused(client, member, session_factory, outbox):
    assert _request(client, password="not-my-password").status_code == 401
    with session_factory() as db:
        for sess in db.scalars(select(UserSession)).all():
            sess.step_up_at = datetime.now(UTC) - timedelta(minutes=11)
        db.commit()
    assert _request(client).status_code == 403
    assert outbox.last_to(NEW) is None
    assert _request(client, OLD.upper()).status_code == 403  # step-up is checked first


def test_same_address_is_refused(client, member):
    assert _request(client, OLD).status_code == 422


def test_requests_are_rate_limited_per_user(client, member):
    codes = [_request(client, f"n{i}@example.com").status_code for i in range(6)]
    assert codes == [202] * 5 + [429]


def test_security_page_form(client, member, session_factory, outbox):
    page = client.get("/dashboard/security")
    assert 'action="/dashboard/security/email"' in page.text
    resp = client.post(
        "/dashboard/security/email",
        data={"new_email": NEW, "current_password": PASSWORD},
    )
    assert resp.status_code == 200
    assert "confirmation link is on its way" in resp.text
    assert outbox.last_to(NEW) is not None
    wrong = client.post(
        "/dashboard/security/email",
        data={"new_email": NEW, "current_password": "nope-nope-nope"},
    )
    assert wrong.status_code == 401 and "Current password is wrong." in wrong.text


def test_security_page_form_takes_a_code_when_not_stepped_up(client, member, session_factory):
    with session_factory() as db:
        for sess in db.scalars(select(UserSession)).all():
            sess.step_up_at = None
        db.commit()
    resp = client.post(
        "/dashboard/security/email",
        data={"new_email": NEW, "current_password": PASSWORD, "code": code(member["totp"], 1)},
    )
    assert resp.status_code == 200 and "confirmation link is on its way" in resp.text
