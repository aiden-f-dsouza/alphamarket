"""Integration of P3 auth with the P3 audit log and alert hooks: every sign-in
and account-security change is recorded ("security.<kind>"), and key
suspensions raised by the nightly scope re-check reach the owner by email."""

from sqlalchemy import select

from alphamarket import alerts, email
from alphamarket.models import AuditLog, User
from authutil import outbox, secure_account

PASSWORD = "correct horse battery"


def _actions(session_factory, user_id=None) -> list[str]:
    with session_factory() as db:
        q = select(AuditLog.action).order_by(AuditLog.id)
        if user_id is not None:
            q = q.where(AuditLog.actor_user_id == user_id)
        return list(db.scalars(q).all())


def test_login_success_and_failure_are_audited(client, session_factory):
    user = client.post(
        "/auth/signup",
        json={"email": "a@example.com", "password": PASSWORD, "display_name": "Ay"},
    ).json()
    client.post("/auth/logout")
    bad = client.post("/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
    assert bad.status_code == 401
    ok = client.post("/auth/login", json={"email": "a@example.com", "password": PASSWORD})
    assert ok.status_code == 200
    actions = _actions(session_factory, user["id"])
    assert actions.count("security.login_failed") == 1
    assert "security.login" in actions
    # An unknown address is audited without an actor and without storing it.
    client.post("/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    with session_factory() as db:
        row = db.scalars(
            select(AuditLog)
            .where(AuditLog.action == "security.login_failed")
            .order_by(AuditLog.id.desc())
        ).first()
    assert row.actor_user_id is None
    assert "nobody@example.com" not in str(row.details)


def test_totp_enrolment_is_audited_and_emailed(client, session_factory):
    user = client.post(
        "/auth/signup",
        json={"email": "t@example.com", "password": PASSWORD, "display_name": "Tee"},
    ).json()
    secure_account(client, "t@example.com")
    assert "security.totp_enabled" in _actions(session_factory, user["id"])
    assert any(m.to == "t@example.com" and "Two-factor" in m.subject for m in outbox().outbox)


def test_suspension_alert_reaches_the_owner_by_email(session_factory):
    with session_factory() as db:
        owner = User(email="s@example.com", password_hash="x", display_name="Ess")
        db.add(owner)
        db.commit()
        owner_id = owner.id
    before = len(alerts.HOOKS)
    email.register_alert_email(session_factory)
    try:
        delivered = alerts.emit(
            alerts.SecurityAlert("key_suspended", owner_id, "s", "b", {"venue": "kraken"})
        )
    finally:
        alerts.HOOKS[:] = alerts.HOOKS[:before]
    assert delivered == before + 1  # the default logging hook plus ours
    msg = outbox().last_to("s@example.com")
    assert msg is not None and "suspended" in msg.subject.lower()
    assert "venue: kraken" in msg.body
