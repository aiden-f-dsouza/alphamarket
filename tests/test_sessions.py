"""DB-backed sessions (P3): opaque hashed ids, cookie flags, rotation,
idle/absolute timeouts, sign out everywhere, the active-session list."""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from alphamarket.config import get_settings
from alphamarket.email import ConsoleEmailSender, set_sender
from alphamarket.models import UserSession
from alphamarket.sessions import COOKIE_NAME, is_live

SIGNUP = {"email": "sess@example.com", "password": "hunter2hunter2", "display_name": "Sid"}
LOGIN = {"email": SIGNUP["email"], "password": SIGNUP["password"]}


@pytest.fixture(autouse=True)
def fresh_outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


def _second_browser(client) -> TestClient:
    other = TestClient(client.app, headers={"origin": get_settings().public_base_url})
    other.__enter__()
    return other


def test_cookie_is_opaque_hashed_and_flagged(client, session_factory):
    resp = client.post("/auth/signup", json=SIGNUP)
    header = resp.headers["set-cookie"]
    assert header.startswith(f"{COOKIE_NAME}=")
    assert "HttpOnly" in header and "SameSite=Lax" in header and "Path=/" in header
    assert "Secure" not in header  # SECURE_COOKIES off in tests
    token = client.cookies.get(COOKIE_NAME)
    assert len(token) >= 43  # 32 random bytes, urlsafe base64
    with session_factory() as db:
        row = db.scalars(select(UserSession)).one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert row.ip == "testclient" and row.user_agent


def test_secure_flag_follows_settings(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "secure_cookies", True)
    resp = client.post("/auth/signup", json=SIGNUP)
    assert "Secure" in resp.headers["set-cookie"]


def test_login_rotates_the_session_id(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    before = client.cookies.get(COOKIE_NAME)
    client.post("/auth/login", json=LOGIN)
    after = client.cookies.get(COOKIE_NAME)
    assert before != after
    client.cookies.set(COOKIE_NAME, before)
    assert client.get("/auth/me").status_code == 401  # the pre-login id is dead


def test_logout_revokes_server_side(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    token = client.cookies.get(COOKIE_NAME)
    assert client.post("/auth/logout").status_code == 204
    client.cookies.set(COOKIE_NAME, token)  # a copied cookie is worthless afterwards
    assert client.get("/auth/me").status_code == 401
    with session_factory() as db:
        assert db.scalars(select(UserSession)).one().revoked_at is not None


def _age(session_factory, **delta) -> None:
    with session_factory() as db:
        for s in db.scalars(select(UserSession)):
            if "idle" in delta:
                s.last_seen_at = datetime.now(UTC) - delta["idle"]
            if "age" in delta:
                s.created_at = datetime.now(UTC) - delta["age"]
        db.commit()


def test_idle_timeout_24h(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    _age(session_factory, idle=timedelta(hours=23, minutes=59))
    assert client.get("/auth/me").status_code == 200  # and this refreshes last_seen_at
    _age(session_factory, idle=timedelta(hours=24, seconds=1))
    assert client.get("/auth/me").status_code == 401


def test_absolute_timeout_30d_even_when_active(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    _age(session_factory, age=timedelta(days=30, seconds=1))
    assert client.get("/auth/me").status_code == 401


def test_is_live_boundaries():
    now = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
    s = UserSession(created_at=now - timedelta(days=29), last_seen_at=now - timedelta(hours=23))
    s.mfa_pending, s.revoked_at = False, None
    assert is_live(s, now)
    s.last_seen_at = now - timedelta(hours=24)  # idle limit is exclusive
    assert not is_live(s, now)
    pending = UserSession(created_at=now - timedelta(minutes=9), last_seen_at=now)
    pending.mfa_pending, pending.revoked_at = True, None
    assert is_live(pending, now)
    pending.created_at = now - timedelta(minutes=10)
    assert not is_live(pending, now)


def test_sign_out_everywhere_and_session_list(client, session_factory):
    client.post("/auth/signup", json=SIGNUP)
    other = _second_browser(client)
    try:
        assert other.post("/auth/login", json=LOGIN).status_code == 200
        listed = client.get("/auth/sessions").json()
        assert len(listed) == 2 and sum(s["current"] for s in listed) == 1
        # Revoke the other browser's session from this one.
        other_id = next(s["id"] for s in listed if not s["current"])
        assert client.delete(f"/auth/sessions/{other_id}").status_code == 204
        assert other.get("/auth/me").status_code == 401
        assert other.post("/auth/login", json=LOGIN).status_code == 200
        assert client.post("/auth/sessions/revoke-all").status_code == 204
        assert client.get("/auth/me").status_code == 401
        assert other.get("/auth/me").status_code == 401
    finally:
        other.__exit__(None, None, None)


def test_security_page_lists_sessions_and_signs_out_everywhere(client):
    assert client.get("/dashboard/security", follow_redirects=False).status_code == 303
    client.post("/auth/signup", json=SIGNUP)
    html = client.get("/dashboard/security").text
    assert "this device" in html and "testclient" in html
    assert "Sign out everywhere" in html and "Turn on two-factor" in html
    resp = client.post("/dashboard/security/sessions/revoke-all", follow_redirects=False)
    assert resp.headers["location"] == "/login"
    assert client.get("/auth/me").status_code == 401


def test_new_ip_login_sends_alert(client, fresh_outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "trust_proxy_headers", True)
    client.post("/auth/signup", json=SIGNUP, headers={"CF-Connecting-IP": "203.0.113.1"})
    client.post("/auth/login", json=LOGIN, headers={"CF-Connecting-IP": "203.0.113.1"})
    assert not any("New sign-in" in m.subject for m in fresh_outbox.outbox)  # known IP
    client.post("/auth/login", json=LOGIN, headers={"CF-Connecting-IP": "198.51.100.2"})
    alert = fresh_outbox.outbox[-1]
    assert alert.subject == "New sign-in to your Koine account" and "198.51.100.2" in alert.body
