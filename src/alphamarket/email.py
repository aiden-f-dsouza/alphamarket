"""Transactional email (P3): verification, password reset, security alerts.

`EmailSender` is the one interface. `ConsoleEmailSender` keeps an in-memory
outbox and logs each message (dev and tests; preflight refuses it in prod).
`SmtpEmailSender` uses stdlib smtplib with STARTTLS, which works with the
SMTP relays of Postmark, SES and Resend alike.

Sending never breaks the request that triggered it: a failed send is logged
(kind and recipient domain only, never the body, which may carry a token)
and swallowed. Callers must never put a secret in `details` of a security
alert (invariant 4).
"""

import logging
import smtplib
import threading
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from alphamarket.config import get_settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutgoingEmail:
    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class ConsoleEmailSender:
    """Dev/test sender: remembers the last `maxlen` messages and logs them."""

    def __init__(self, maxlen: int = 500) -> None:
        self.outbox: deque[OutgoingEmail] = deque(maxlen=maxlen)

    def send(self, to: str, subject: str, body: str) -> None:
        self.outbox.append(OutgoingEmail(to, subject, body))
        logger.info("console email to %s: %s\n%s", to, subject, body)

    def last_to(self, to: str) -> OutgoingEmail | None:
        for msg in reversed(self.outbox):
            if msg.to == to:
                return msg
        return None


class SmtpEmailSender:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        sender: str,
        starttls: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender, self.starttls, self.timeout = sender, starttls, timeout

    def send(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password)
            smtp.send_message(msg)


_sender: EmailSender | None = None
_lock = threading.Lock()


def build_sender(backend: str | None = None) -> EmailSender:
    s = get_settings()
    backend = backend or s.email_backend
    if backend == "console":
        return ConsoleEmailSender()
    if backend == "smtp":
        return SmtpEmailSender(
            s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password, s.email_from, s.smtp_starttls
        )
    raise ValueError(f"Unknown EMAIL_BACKEND {backend!r} (console | smtp)")


def get_sender() -> EmailSender:
    """The process-wide sender, built from settings on first use."""
    global _sender
    with _lock:
        if _sender is None:
            _sender = build_sender()
        return _sender


def set_sender(sender: EmailSender | None) -> None:
    """Swap the sender (tests); None rebuilds from settings on next use."""
    global _sender
    with _lock:
        _sender = sender


def send_email(to: str, subject: str, body: str) -> bool:
    try:
        get_sender().send(to, subject, body)
        return True
    except Exception as exc:  # noqa: BLE001 - email must never break the request
        domain = to.rsplit("@", 1)[-1]
        logger.warning("email to @%s failed (%s): %s", domain, subject, type(exc).__name__)
        return False


# ---- security alerts --------------------------------------------------------

ALERT_SUBJECTS = {
    "new_login": "New sign-in to your Koine account",
    "totp_enabled": "Two-factor authentication turned on",
    "totp_disabled": "Two-factor authentication turned off",
    "totp_lockout": "Two-factor code attempts locked for 15 minutes",
    "recovery_code_used": "A recovery code was used on your Koine account",
    "password_changed": "Your Koine password was changed",
    "sessions_revoked": "You were signed out everywhere",
    "key_connected": "A venue key was connected to your Koine account",
    "key_suspended": "A venue key was suspended",
}

ALERT_LEADS = {
    "new_login": "Your account was signed in from a device or network we had not seen before.",
    "totp_enabled": "Two-factor authentication is now required to sign in.",
    "totp_disabled": "Two-factor authentication was removed from your account.",
    "totp_lockout": "Too many wrong two-factor codes were entered, so code entry is paused.",
    "recovery_code_used": "One of your single-use recovery codes was used to sign in.",
    "password_changed": "Your password was changed and every session was signed out.",
    "sessions_revoked": "Every session on your account was signed out.",
    "key_connected": "A read-only venue key was connected.",
    "key_suspended": "A venue key was suspended and is no longer used.",
}


def send_security_alert(user, kind: str, details: dict[str, str] | None = None) -> bool:
    """Email `user` about a security-relevant event. `kind` is one of
    ALERT_SUBJECTS (other kinds get a generic subject). `details` is shown
    as "key: value" lines: IP, device, venue, time. Never a secret."""
    subject = ALERT_SUBJECTS.get(kind, "Security notice for your Koine account")
    lines = [
        f"Hi {user.display_name},",
        "",
        ALERT_LEADS.get(kind, f"Security event: {kind}."),
        "",
    ]
    for key, value in (details or {}).items():
        lines.append(f"{key}: {value}")
    base = get_settings().public_base_url.rstrip("/")
    lines += [
        "",
        "If this was you, there is nothing to do. If not, change your password and",
        f"sign out every session at {base}/dashboard/security.",
        "",
        "Koine",
    ]
    return send_email(user.email, subject, "\n".join(lines))
