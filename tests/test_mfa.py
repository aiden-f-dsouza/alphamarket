"""TOTP, recovery codes, two-step login, step-up and the venue-connect gate (P3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from alphamarket import mfa
from alphamarket.email import ConsoleEmailSender, set_sender
from alphamarket.models import RecoveryCode, UserSession, UserTotp
from alphamarket.vault import decrypt_secret
from authutil import code, enrol_totp, outbox, secure_account, verify_email

SIGNUP = {"email": "mfa@example.com", "password": "hunter2hunter2", "display_name": "Mo"}
# RFC 6238 appendix B seed "12345678901234567890" (ASCII), base32-encoded.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.fixture(autouse=True)
def fresh_outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


@pytest.fixture()
def member(client):
    assert client.post("/auth/signup", json=SIGNUP).status_code == 201
    return client


def _at(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


# ---- pure helpers -------------------------------------------------------------


def test_rfc6238_vectors():
    # RFC 6238 appendix B (SHA-1, 8 digits) truncated to our 6 digits:
    # T=59 -> 94287082, T=1111111109 -> 07081804, T=1234567890 -> 89005924.
    assert mfa.time_step(_at(59)) == 1  # floor(59 / 30)
    assert mfa.code_at_step(RFC_SECRET, 1) == "287082"
    assert mfa.code_at_step(RFC_SECRET, 1111111109 // 30) == "081804"
    assert mfa.code_at_step(RFC_SECRET, 1234567890 // 30) == "005924"


def test_match_step_drift_and_replay():
    # At t = 89 s the current step is floor(89 / 30) = 2; steps 1..3 are accepted.
    t = _at(89)
    assert mfa.match_step(RFC_SECRET, "287082", t, None) == 1  # previous step (drift)
    assert mfa.match_step(RFC_SECRET, mfa.code_at_step(RFC_SECRET, 3), t, None) == 3
    assert mfa.match_step(RFC_SECRET, mfa.code_at_step(RFC_SECRET, 4), t, None) is None
    # Replay guard: once step 1 was used, its code is refused.
    assert mfa.match_step(RFC_SECRET, "287082", t, last_used_step=1) is None
    assert mfa.match_step(RFC_SECRET, " 287 082 ", t, None) == 1  # spaces are ignored
    assert mfa.match_step(RFC_SECRET, "28708", t, None) is None
    assert mfa.match_step(RFC_SECRET, "abcdef", t, None) is None


def test_recovery_code_shape_and_hash():
    c = mfa.new_recovery_code()
    assert len(c) == 19 and c.count("-") == 3  # 16 base32 chars = 80 bits
    assert mfa.hash_recovery_code(c.upper().replace("-", " ")) == mfa.hash_recovery_code(c)
    assert len(mfa.hash_recovery_code(c)) == 64


def test_qr_is_inline_svg_without_script():
    svg = mfa.qr_svg(mfa.provisioning_uri(RFC_SECRET, "a@example.com"))
    assert svg.startswith("<svg") and "<script" not in svg


# ---- enrolment -------------------------------------------------------------------


def test_enrol_confirm_stores_encrypted_secret_and_hashed_codes(member, session_factory):
    resp = member.post("/auth/totp/enroll")
    secret = resp.json()["secret"]
    assert resp.json()["provisioning_uri"].startswith("otpauth://totp/Koine:")
    assert member.post("/auth/totp/confirm", json={"code": "000000"}).status_code == 422
    resp = member.post("/auth/totp/confirm", json={"code": code(secret)})
    assert resp.status_code == 200
    codes = resp.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    with session_factory() as db:
        row = db.scalars(select(UserTotp)).one()
        assert secret.encode() not in row.secret_encrypted  # vault ciphertext only
        assert decrypt_secret(row.secret_encrypted) == secret
        stored = {r.code_hash for r in db.scalars(select(RecoveryCode))}
        assert stored == {mfa.hash_recovery_code(c) for c in codes}
        assert not any(c in stored for c in codes)
    assert member.get("/auth/me").json()["totp_enabled"] is True
    assert member.post("/auth/totp/enroll").status_code == 409  # already on
    assert "Two-factor authentication turned on" in outbox().outbox[-1].subject


# ---- login in two steps ------------------------------------------------------------


def test_login_needs_second_step_when_totp_on(member):
    secret, _ = enrol_totp(member)
    member.post("/auth/logout")
    resp = member.post("/auth/login", json={"email": SIGNUP["email"], "password": "hunter2hunter2"})
    assert resp.status_code == 200 and resp.json()["mfa_required"] is True
    assert member.get("/auth/me").status_code == 401  # a pending session authenticates nothing
    assert member.post("/auth/totp/verify", json={"code": "123456"}).status_code == 401
    resp = member.post("/auth/totp/verify", json={"code": code(secret)})
    assert resp.status_code == 200, resp.text
    assert member.get("/auth/me").status_code == 200


def test_totp_verify_without_password_step_is_refused(client):
    assert client.post("/auth/totp/verify", json={"code": "123456"}).status_code == 401


def test_recovery_code_logs_in_once(member, fresh_outbox):
    _, codes = enrol_totp(member)
    member.post("/auth/logout")
    creds = {"email": SIGNUP["email"], "password": "hunter2hunter2"}
    member.post("/auth/login", json=creds)
    assert member.post("/auth/totp/verify", json={"code": codes[0]}).status_code == 200
    assert fresh_outbox.outbox[-1].subject.startswith("A recovery code was used")
    member.post("/auth/logout")
    member.post("/auth/login", json=creds)
    assert member.post("/auth/totp/verify", json={"code": codes[0]}).status_code == 401


def test_login_form_two_steps(client):
    client.post("/auth/signup", json=SIGNUP)
    secret, _ = enrol_totp(client)
    client.post("/logout-form")
    resp = client.post(
        "/login-form",
        data={"email": SIGNUP["email"], "password": "hunter2hunter2"},
        follow_redirects=False,
    )
    assert resp.headers["location"] == "/login/2fa"
    assert "authenticator app" in client.get("/login/2fa").text
    bad = client.post("/login/2fa", data={"code": "000000"})
    assert bad.status_code == 401 and 'role="alert"' in bad.text
    ok = client.post("/login/2fa", data={"code": code(secret)}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/dashboard"
    assert "Mo" in client.get("/dashboard").text


# ---- lockout -----------------------------------------------------------------------


def test_five_wrong_codes_lock_for_fifteen_minutes(member, session_factory, fresh_outbox):
    secret, _ = enrol_totp(member)
    for _ in range(4):
        assert member.post("/auth/totp/step-up", json={"code": "000000"}).status_code == 401
    resp = member.post("/auth/totp/step-up", json={"code": "000000"})
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "900"
    assert "15 minutes" in fresh_outbox.outbox[-1].subject
    # Even the right code is refused while locked.
    resp = member.post("/auth/totp/step-up", json={"code": code(secret, 1)})
    assert resp.status_code == 429
    with session_factory() as db:
        row = db.scalars(select(UserTotp)).one()
        row.locked_until = datetime.now(UTC) - timedelta(seconds=1)  # the lock ran out
        db.commit()
    assert member.post("/auth/totp/step-up", json={"code": code(secret, 1)}).status_code == 204


# ---- step-up and the connect gate ---------------------------------------------------


def _connect(client):
    return client.post(
        "/venues", json={"venue": "polymarket", "key_id": "0xabc", "strategy_name": "Gate test"}
    )


def test_connect_without_totp_is_403_pointing_at_security_page(member):
    verify_email(member, SIGNUP["email"])
    resp = _connect(member)
    assert resp.status_code == 403
    assert "/dashboard/security" in resp.json()["detail"]
    assert "Two-factor" in resp.json()["detail"]


def test_connect_needs_verified_email(member):
    enrol_totp(member)
    resp = _connect(member)
    assert resp.status_code == 403 and "Verify your email" in resp.json()["detail"]


def test_step_up_expires_after_ten_minutes(member, session_factory):
    secret = secure_account(member, SIGNUP["email"])
    with session_factory() as db:
        for s in db.scalars(select(UserSession)):
            if s.step_up_at is not None:
                s.step_up_at = datetime.now(UTC) - timedelta(minutes=11)
        db.commit()
    resp = _connect(member)
    assert resp.status_code == 403 and "Confirm a current two-factor code" in resp.json()["detail"]
    assert member.delete("/venues/1").status_code == 403  # disconnect is guarded too
    assert member.post("/auth/totp/step-up", json={"code": code(secret, 1)}).status_code == 204
    assert member.delete("/venues/1").status_code == 404  # past the gate; no such account


def test_step_up_rotates_the_session(member, session_factory):
    secret = secure_account(member, SIGNUP["email"])
    before = member.cookies.get("koine_sid")
    assert member.post("/auth/totp/step-up", json={"code": code(secret, 1)}).status_code == 204
    assert member.cookies.get("koine_sid") != before
    member.cookies.set("koine_sid", before)
    assert member.get("/auth/me").status_code == 401  # the old id was revoked


def test_disable_requires_code_or_recent_step_up(member, session_factory, fresh_outbox):
    secret, _ = enrol_totp(member)
    with session_factory() as db:
        for s in db.scalars(select(UserSession)):
            s.step_up_at = None
        db.commit()
    assert member.post("/auth/totp/disable", json={}).status_code == 403
    assert member.post("/auth/totp/disable", json={"code": "000000"}).status_code == 401
    assert member.post("/auth/totp/disable", json={"code": code(secret, 1)}).status_code == 204
    assert member.get("/auth/me").json()["totp_enabled"] is False
    assert fresh_outbox.outbox[-1].subject == "Two-factor authentication turned off"
    with session_factory() as db:
        assert db.scalars(select(RecoveryCode)).all() == []


def test_regenerate_recovery_codes_needs_step_up(member, session_factory):
    _, old = enrol_totp(member)
    resp = member.post("/auth/totp/recovery-codes")
    assert resp.status_code == 200  # the enrolment counted as a step-up
    new = resp.json()["recovery_codes"]
    assert set(new).isdisjoint(old)
    with session_factory() as db:
        assert len(db.scalars(select(RecoveryCode)).all()) == 10
