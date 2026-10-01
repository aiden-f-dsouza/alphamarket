"""Review fixes for P3: open-redirect guard, atomic token consumption, and
bcrypt's 72-byte ceiling on every password path."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from alphamarket import account
from alphamarket.email import ConsoleEmailSender, set_sender
from alphamarket.models import AuthToken, AuthTokenKind
from alphamarket.security_pages import _safe_next
from authutil import last_token

SIGNUP = {"email": "hard@example.com", "password": "hunter2hunter2", "display_name": "Hu"}


@pytest.fixture(autouse=True)
def fresh_outbox():
    sender = ConsoleEmailSender()
    set_sender(sender)
    yield sender
    set_sender(None)


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example",
        "/\\evil.example",  # browsers normalise "\" to "/": //evil.example
        "/\\/evil.example",
        "/\t/evil.example",  # tabs and newlines are stripped by the URL parser
        "/\n/evil.example",
        "https://evil.example",
        "evil.example",
        "",
    ],
)
def test_safe_next_refuses_offsite_targets(target):
    assert _safe_next(target) == "/dashboard/security"


def test_safe_next_keeps_same_site_paths():
    assert _safe_next("/dashboard/connect?venue=kalshi") == "/dashboard/connect?venue=kalshi"


def test_step_up_form_does_not_redirect_offsite(client):
    client.post("/auth/signup", json=SIGNUP)
    # No TOTP yet, so the step-up fails and re-renders; the point is that no
    # Location header ever carries the backslash target.
    resp = client.post(
        "/dashboard/security/step-up",
        data={"code": "000000", "next": "/\\evil.example"},
        follow_redirects=False,
    )
    assert "evil.example" not in resp.headers.get("location", "")


def test_consume_token_loses_the_race_when_already_claimed(client, session_factory):
    """A second request that read the row before the first one marked it used
    must not succeed: the claim is a conditional UPDATE, not read-then-write."""
    client.post("/auth/signup", json=SIGNUP)
    token = last_token(SIGNUP["email"], "/verify-email")
    with session_factory() as db:
        # Simulate the other request winning between our SELECT and our UPDATE:
        # load the row (still unused), then mark it used behind the ORM's back.
        row = db.scalar(select(AuthToken).where(AuthToken.kind == AuthTokenKind.VERIFY_EMAIL))
        assert row.used_at is None
        with session_factory() as other:
            other.execute(
                update(AuthToken).where(AuthToken.id == row.id).values(used_at=datetime.now(UTC))
            )
            other.commit()
        assert account.consume_token(db, token, AuthTokenKind.VERIFY_EMAIL) is None


def test_long_password_login_is_401_not_500(client):
    client.post("/auth/signup", json=SIGNUP)
    # 80 ASCII chars = 80 bytes > 72: bcrypt>=5 raises ValueError on it.
    resp = client.post("/auth/login", json={"email": SIGNUP["email"], "password": "a" * 80})
    assert resp.status_code == 401


def test_signup_refuses_password_over_72_bytes(client):
    # 25 x "é" (2 bytes each in UTF-8) = 50 bytes: fits. 37 x "é" = 74 bytes: refused.
    ok = client.post(
        "/auth/signup", json={**SIGNUP, "email": "fits@example.com", "password": "é" * 25}
    )
    assert ok.status_code == 201
    long = client.post(
        "/auth/signup", json={**SIGNUP, "email": "long@example.com", "password": "é" * 37}
    )
    assert long.status_code == 422


def test_reset_with_overlong_password_does_not_burn_the_token(client):
    client.post("/auth/signup", json=SIGNUP)
    client.post("/auth/password-reset/request", json={"email": SIGNUP["email"]})
    token = last_token(SIGNUP["email"], "/reset-password")
    # 100 chars passes the 128-char field limit but is 100 bytes > 72.
    bad = client.post("/auth/password-reset/confirm", json={"token": token, "password": "b" * 100})
    assert bad.status_code == 422
    good = client.post("/auth/password-reset/confirm", json={"token": token, "password": "c" * 72})
    assert good.status_code == 200  # 72 bytes is exactly the limit
    login = client.post("/auth/login", json={"email": SIGNUP["email"], "password": "c" * 72})
    assert login.status_code == 200


def test_change_password_refuses_overlong_new_password(client):
    client.post("/auth/signup", json=SIGNUP)
    resp = client.post(
        "/auth/password/change",
        json={"current_password": SIGNUP["password"], "new_password": "d" * 90},
    )
    assert resp.status_code == 422
