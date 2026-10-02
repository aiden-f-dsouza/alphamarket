"""Test helpers for the P3 account-security gates (not a test module).

Connecting a venue needs a confirmed email, TOTP and a fresh step-up. Tests
that exercise connect flows call `secure_account` right after signing up.
"""

import re
from datetime import UTC, datetime

import pyotp

from alphamarket.email import ConsoleEmailSender, get_sender
from alphamarket.mfa import STEP_SECONDS

TOKEN_RE = re.compile(r"token=([A-Za-z0-9_\-]+)")


def outbox() -> ConsoleEmailSender:
    sender = get_sender()
    assert isinstance(sender, ConsoleEmailSender), "tests need EMAIL_BACKEND=console"
    return sender


def last_token(email: str, path: str) -> str:
    """The token from the newest email to `email` whose link points at `path`."""
    for msg in reversed(outbox().outbox):
        if msg.to == email and f"{path}?token=" in msg.body:
            return TOKEN_RE.search(msg.body).group(1)
    raise AssertionError(f"no {path} email to {email}")


def code(secret: str, offset_steps: int = 0) -> str:
    """The TOTP code `offset_steps` 30-second steps from now. Offsets -1..1
    are accepted; each step can be used once, so a second code inside the
    same 30 s window uses offset 1."""
    now = int(datetime.now(UTC).timestamp())
    return pyotp.TOTP(secret).at(now + offset_steps * STEP_SECONDS)


def verify_email(client, email: str) -> None:
    resp = client.post("/auth/verify-email", json={"token": last_token(email, "/verify-email")})
    assert resp.status_code == 200, resp.text


def enrol_totp(client) -> tuple[str, list[str]]:
    """Enrol and confirm TOTP for the logged-in user (confirming also steps up)."""
    secret = client.post("/auth/totp/enroll").json()["secret"]
    resp = client.post("/auth/totp/confirm", json={"code": code(secret, -1)})
    assert resp.status_code == 200, resp.text
    return secret, resp.json()["recovery_codes"]


def secure_account(client, email: str) -> str:
    """Confirm the email, turn on TOTP and step up; returns the TOTP secret."""
    verify_email(client, email)
    secret, _ = enrol_totp(client)
    return secret


def consent(venue: str) -> str:
    """wave2 (invariant 10): the sha256 of the venue's current consent text,
    which every connect (form, JSON POST /venues, OAuth start, SnapTrade
    start) must send as `consent_sha256`."""
    from alphamarket.consent import consent_text

    return consent_text(venue).sha256
