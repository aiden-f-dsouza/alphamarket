"""Email verification, password reset/change, security alerts, the HIBP
breached-password check (respx only, invariant 6) and the P3 pages."""

import hashlib
import logging
import smtplib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from alphamarket import passwords
from alphamarket.config import get_settings
from alphamarket.email import (
    ConsoleEmailSender,
    SmtpEmailSender,
    send_email,
    send_security_alert,
    set_sender,
)
from alphamarket.models import AuthToken, User, UserSession
from authutil import last_token

SIGNUP = {"email": "acct@example.com", "password": "hunter2hunter2", "display_name": "Ada"}
LOGIN = {"email": SIGNUP["email"], "password": SIGNUP["password"]}
HIBP = "https://api.pwnedpasswords.com/range/"


@pytest.fixture(autouse=True)
def fresh_outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


# ---- email verification ------------------------------------------------------------


def test_signup_sends_single_use_verification_link(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    assert client.get("/auth/me").json()["email_verified"] is False
    token = last_token(SIGNUP["email"], "/verify-email")
    with session_factory() as db:
        row = db.scalars(select(AuthToken)).one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()  # stored hashed
        assert row.kind == "verify_email"
    html = client.get(f"/verify-email?token={token}").text
    assert "Email confirmed." in html
    assert client.get("/auth/me").json()["email_verified"] is True
    assert "expired" in client.get(f"/verify-email?token={token}").text  # single use
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 400


def test_expired_verification_token_is_refused(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    token = last_token(SIGNUP["email"], "/verify-email")
    with session_factory() as db:
        db.scalars(select(AuthToken)).one().expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    assert client.post("/auth/verify-email", json={"token": token}).status_code == 400


def test_resend_verification_is_limited(client, fresh_outbox):
    client.post("/auth/signup", json=SIGNUP)
    for _ in range(3):
        assert client.post("/auth/verify-email/resend").status_code == 202
    assert client.post("/auth/verify-email/resend").status_code == 429
    assert len(fresh_outbox.outbox) == 4  # signup + 3 resends


# ---- password reset ---------------------------------------------------------------


def test_reset_request_is_200_for_unknown_email_and_sends_nothing(client, fresh_outbox):
    resp = client.post("/auth/password-reset/request", json={"email": "ghost@example.com"})
    assert resp.status_code == 200
    assert fresh_outbox.outbox == type(fresh_outbox.outbox)()  # empty


def test_reset_flow_single_use_and_revokes_sessions(client, session_factory, fresh_outbox):
    client.post("/auth/signup", json=SIGNUP)
    client.post("/auth/password-reset/request", json={"email": SIGNUP["email"]})
    token = last_token(SIGNUP["email"], "/reset-password")
    resp = client.post(
        "/auth/password-reset/confirm", json={"token": token, "password": "new-password-123"}
    )
    assert resp.status_code == 200
    assert client.get("/auth/me").status_code == 401  # every session revoked
    with session_factory() as db:
        assert all(s.revoked_at is not None for s in db.scalars(select(UserSession)))
    assert fresh_outbox.outbox[-1].subject == "Your Koine password was changed"
    again = client.post(
        "/auth/password-reset/confirm", json={"token": token, "password": "another-pass-456"}
    )
    assert again.status_code == 400
    assert client.post("/auth/login", json=LOGIN).status_code == 401
    assert (
        client.post(
            "/auth/login", json={"email": SIGNUP["email"], "password": "new-password-123"}
        ).status_code
        == 200
    )


def test_reset_token_expires_after_an_hour(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    client.post("/auth/password-reset/request", json={"email": SIGNUP["email"]})
    token = last_token(SIGNUP["email"], "/reset-password")
    with session_factory() as db:
        row = db.scalars(select(AuthToken).where(AuthToken.kind == "password_reset")).one()
        assert _ttl(row) == timedelta(hours=1)
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
    resp = client.post(
        "/auth/password-reset/confirm", json={"token": token, "password": "new-password-123"}
    )
    assert resp.status_code == 400


def _ttl(row: AuthToken) -> timedelta:
    return row.expires_at - row.created_at


def test_reset_request_limits(client, fresh_outbox):
    client.post("/auth/signup", json=SIGNUP)
    fresh_outbox.outbox.clear()
    for _ in range(5):  # 3 per hour per email; the rest are silently dropped
        resp = client.post("/auth/password-reset/request", json={"email": SIGNUP["email"]})
        assert resp.status_code == 200
    assert len(fresh_outbox.outbox) == 3
    for i in range(5):  # 10 per hour per IP in total, then 429
        client.post("/auth/password-reset/request", json={"email": f"n{i}@example.com"})
    assert (
        client.post("/auth/password-reset/request", json={"email": "z@example.com"}).status_code
        == 429
    )


def test_reset_pages(client):
    client.post("/auth/signup", json=SIGNUP)
    client.post("/logout-form")
    assert "Forgot your password?" in client.get("/login").text
    assert "Email me a link" in client.get("/forgot-password").text
    html = client.post("/forgot-password", data={"email": SIGNUP["email"]}).text
    assert "Check your email." in html
    token = last_token(SIGNUP["email"], "/reset-password")
    assert f'value="{token}"' in client.get(f"/reset-password?token={token}").text
    short = client.post("/reset-password", data={"token": token, "password": "short"})
    assert short.status_code == 422
    ok = client.post("/reset-password", data={"token": token, "password": "brand-new-pass-1"})
    assert "Password changed." in ok.text


def test_change_password_keeps_this_session_only(client, session_factory, fresh_outbox):
    from fastapi.testclient import TestClient

    client.post("/auth/signup", json=SIGNUP)
    with TestClient(client.app) as other:
        other.post("/auth/login", json=LOGIN)
        bad = {"current_password": "wrong-one-1", "new_password": "next-password-1"}
        assert client.post("/auth/password/change", json=bad).status_code == 401
        good = {"current_password": SIGNUP["password"], "new_password": "next-password-1"}
        assert client.post("/auth/password/change", json=good).status_code == 204
        assert client.get("/auth/me").status_code == 200
        assert other.get("/auth/me").status_code == 401
    assert fresh_outbox.outbox[-1].subject == "Your Koine password was changed"


# ---- HIBP breached-password check (k-anonymity) ------------------------------------------


def test_sha1_split_hand_computed():
    # SHA-1("password") = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
    assert passwords.sha1_split("password") == ("5BAA6", "1E4C9B93F3F0682250B6CF8331B7EE68FD8")


@respx.mock
def test_breach_count_sends_only_the_prefix():
    route = respx.get(f"{HIBP}5BAA6").mock(
        return_value=httpx.Response(
            200,
            text=(
                "0018A45C4D1DEF81644B54AB7F969B88D65:1\r\n"
                "1E4C9B93F3F0682250B6CF8331B7EE68FD8:10434004\r\n"
                "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF:0\r\n"
            ),
        )
    )
    assert passwords.breach_count("password") == 10434004
    request = route.calls.last.request
    assert request.url.path == "/range/5BAA6"  # 5 hex chars leave the server, nothing else
    assert request.headers["Add-Padding"] == "true"
    assert b"password" not in request.content and "1E4C9B" not in str(request.url)


@respx.mock
def test_padding_rows_do_not_count():
    prefix, suffix = passwords.sha1_split("a-long-unique-password-xyz")
    respx.get(f"{HIBP}{prefix}").mock(return_value=httpx.Response(200, text=f"{suffix}:0\r\n"))
    assert passwords.breach_count("a-long-unique-password-xyz") == 0


@respx.mock
def test_signup_rejects_breached_password(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "hibp_enabled", True)
    prefix, suffix = passwords.sha1_split(SIGNUP["password"])
    respx.get(f"{HIBP}{prefix}").mock(return_value=httpx.Response(200, text=f"{suffix}:3\r\n"))
    resp = client.post("/auth/signup", json=SIGNUP)
    assert resp.status_code == 422 and "data breach" in resp.json()["detail"]
    other = dict(SIGNUP, password="not-in-the-mock-list-1")
    prefix2, _ = passwords.sha1_split(other["password"])
    respx.get(f"{HIBP}{prefix2}").mock(return_value=httpx.Response(200, text="ABC:1\r\n"))
    assert client.post("/auth/signup", json=other).status_code == 201


@respx.mock
def test_hibp_unreachable_allows_and_warns(client, monkeypatch, caplog):
    monkeypatch.setattr(get_settings(), "hibp_enabled", True)
    prefix, _ = passwords.sha1_split(SIGNUP["password"])
    respx.get(f"{HIBP}{prefix}").mock(side_effect=httpx.ConnectTimeout("down"))
    with caplog.at_level(logging.WARNING, logger="alphamarket.passwords"):
        assert client.post("/auth/signup", json=SIGNUP).status_code == 201
    assert "HIBP range check unavailable" in caplog.text
    assert SIGNUP["password"] not in caplog.text


@respx.mock
def test_reset_rejects_breached_password_without_burning_token(client, monkeypatch):
    client.post("/auth/signup", json=SIGNUP)
    client.post("/auth/password-reset/request", json={"email": SIGNUP["email"]})
    token = last_token(SIGNUP["email"], "/reset-password")
    monkeypatch.setattr(get_settings(), "hibp_enabled", True)
    prefix, suffix = passwords.sha1_split("pwned-password-1")
    respx.get(f"{HIBP}{prefix}").mock(return_value=httpx.Response(200, text=f"{suffix}:9\r\n"))
    body = {"token": token, "password": "pwned-password-1"}
    assert client.post("/auth/password-reset/confirm", json=body).status_code == 422
    prefix2, _ = passwords.sha1_split("fresh-password-2")
    respx.get(f"{HIBP}{prefix2}").mock(return_value=httpx.Response(200, text=""))
    body = {"token": token, "password": "fresh-password-2"}
    assert client.post("/auth/password-reset/confirm", json=body).status_code == 200


# ---- email senders and alerts ------------------------------------------------------------


def test_security_alert_body_names_the_event_and_the_security_page(fresh_outbox):
    user = User(email="u@example.com", display_name="Una", password_hash="x")
    assert send_security_alert(user, "key_suspended", {"Venue": "kraken", "Key": "…a1b2"})
    msg = fresh_outbox.outbox[-1]
    assert msg.to == "u@example.com" and msg.subject == "A venue key was suspended"
    assert "Venue: kraken" in msg.body and "/dashboard/security" in msg.body


def test_send_failure_is_swallowed_and_logged_without_body(caplog):
    class Broken:
        def send(self, to, subject, body):
            raise smtplib.SMTPException("relay down")

    set_sender(Broken())
    with caplog.at_level(logging.WARNING, logger="alphamarket.email"):
        assert send_email("x@example.com", "Reset", "secret-token-abc") is False
    assert "secret-token-abc" not in caplog.text and "@example.com" in caplog.text


def test_smtp_sender_uses_starttls_and_login(monkeypatch):
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = (host, port)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent["tls"] = True

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, msg):
            sent["msg"] = msg

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    SmtpEmailSender("smtp.example.com", 587, "apikey", "pw", "Koine <k@example.com>").send(
        "to@example.com", "Hi", "Body"
    )
    assert sent["host"] == ("smtp.example.com", 587) and sent["tls"] and sent["login"] == "apikey"
    assert sent["msg"]["To"] == "to@example.com" and sent["msg"]["Subject"] == "Hi"


# ---- pages ---------------------------------------------------------------------------------

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "alphamarket" / "templates"
P3_TEMPLATES = (
    "login_2fa.html",
    "account_security.html",
    "totp_enroll.html",
    "recovery_codes.html",
    "password_reset.html",
    "account_notice.html",
)


def test_p3_templates_have_no_script_or_inline_handlers():
    for name in P3_TEMPLATES:
        html = (TEMPLATES / name).read_text().lower()
        assert "<script" not in html, name
        assert " onclick" not in html and " onsubmit" not in html and " onload" not in html, name


def test_enrol_through_pages_then_connect_page_unlocks(client):
    import pyotp

    client.post("/auth/signup", json=SIGNUP)
    connect = client.get("/dashboard/connect").text
    assert "Turn on two-factor authentication before connecting" in connect
    assert "disabled" in connect
    page = client.post("/dashboard/security/totp/enroll").text
    assert "<svg" in page and "<script" not in page.split("<main>")[1].split("</main>")[0]
    secret = page.split('word-break:break-all">')[1].split("<")[0].replace(" ", "")
    now = int(datetime.now(UTC).timestamp())
    wrong = client.post("/dashboard/security/totp/confirm", data={"code": "000000"})
    assert wrong.status_code == 422 and "<svg" in wrong.text  # back on the enrol page
    ok = client.post(
        "/dashboard/security/totp/confirm", data={"code": pyotp.TOTP(secret).at(now - 30)}
    )
    assert "Two-factor is on." in ok.text and ok.text.count("-") >= 30
    connect = client.get("/dashboard/connect").text
    assert "Confirm your email address" in connect  # next gate: email
    token = last_token(SIGNUP["email"], "/verify-email")
    client.get(f"/verify-email?token={token}")
    connect = client.get("/dashboard/connect").text
    assert "before connecting" not in connect and "Confirm your email" not in connect
    html = client.get("/dashboard/security").text
    assert "10 of 10" in html and "confirmed" in html


def test_dashboard_prompts_for_security_setup(client):
    client.post("/auth/signup", json=SIGNUP)
    html = client.get("/dashboard").text
    assert "/dashboard/security" in html and "two-factor" in html


# ---- preflight --------------------------------------------------------------------------


def test_preflight_refuses_dev_security_backends(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "preflight", Path(__file__).resolve().parents[1] / "scripts" / "preflight.py"
    )
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    s = get_settings()
    problems = preflight.security_problems(s)
    assert any("RATE_LIMITER" in p for p in problems)
    assert any("EMAIL_BACKEND" in p for p in problems)
    assert any("HIBP_ENABLED" in p for p in problems)
    monkeypatch.setattr(s, "rate_limiter", "postgres")
    monkeypatch.setattr(s, "email_backend", "smtp")
    monkeypatch.setattr(s, "smtp_host", "smtp.postmarkapp.com")
    monkeypatch.setattr(s, "email_from", "Koine <no-reply@koine.example>")
    monkeypatch.setattr(s, "hibp_enabled", True)
    assert preflight.security_problems(s) == []
