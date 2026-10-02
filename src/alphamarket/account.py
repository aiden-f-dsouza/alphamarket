"""Email verification, password reset and password change (P3).

Emailed tokens are 32 random bytes (urlsafe base64); only their SHA-256 is
stored, with an expiry, and `used_at` makes each one single-use.

- Verification: sent on signup (48 h). An unverified user can browse and
  follow but cannot connect a venue (`require_verified_email`).
- Reset: the request endpoint answers 200 whether or not the email exists,
  and whether or not the per-email limit was hit, so it reveals nothing.
  Tokens last 1 hour. A successful reset revokes every session and every
  other outstanding reset token, and emails a security alert.
- Change: needs the current password; other sessions are revoked.
- Email change (wave2): needs the current password and a fresh step-up.
  The confirmation link (1 hour, an EMAIL_CHANGE token carrying new_email)
  goes to the NEW address; the old one is told at once. The request answers
  the same whether or not the address is already in use. Confirming
  re-checks that, switches the address (already verified: the link reached
  it), revokes every other session and outstanding token, audits
  EMAIL_CHANGE and tells the old address.
"""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alphamarket import audit, mfa, ratelimit, sessions
from alphamarket.audit import AuditAction
from alphamarket.auth import current_user, ensure_password_fits, hash_password, verify_password
from alphamarket.config import get_settings
from alphamarket.db import get_db
from alphamarket.email import (
    ALERT_LEADS,
    ALERT_SUBJECTS,
    security_event,
    send_email,
    send_security_alert,
)
from alphamarket.models import AuthToken, AuthTokenKind, User
from alphamarket.passwords import ensure_not_breached

router = APIRouter(prefix="/auth", tags=["auth"])

VERIFY_TTL = timedelta(hours=48)
RESET_TTL = timedelta(hours=1)
NEEDS_VERIFIED_EMAIL = (
    "Verify your email address before connecting a venue. We sent you a link; "
    "you can send a new one from /dashboard/security."
)
RESET_REQUESTED = "If that email has an account, a reset link is on its way."


def _new_token(db: Session, user: User, kind: str, ttl: timedelta) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        AuthToken(
            user_id=user.id,
            kind=kind,
            token_hash=sessions.hash_token(token),
            created_at=now,
            expires_at=now + ttl,
        )
    )
    db.commit()
    return token


def consume_token(db: Session, token: str, kind: str, now: datetime | None = None) -> User | None:
    """Mark a valid, unexpired, unused token used; return its user."""
    now = now or datetime.now(UTC)
    row = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == sessions.hash_token(token), AuthToken.kind == kind
        )
    )
    if row is None or row.used_at is not None or now >= sessions.aware(row.expires_at):
        return None
    # Conditional UPDATE, not a read-then-write: two concurrent requests with
    # the same token cannot both see used_at IS NULL and both succeed.
    claimed = db.execute(
        update(AuthToken)
        .where(AuthToken.id == row.id, AuthToken.used_at.is_(None))
        .values(used_at=now)
    ).rowcount
    if claimed != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(User, row.user_id)


def _link(path: str, token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}{path}?token={token}"


def send_verification_email(db: Session, user: User) -> None:
    token = _new_token(db, user, AuthTokenKind.VERIFY_EMAIL, VERIFY_TTL)
    send_email(
        user.email,
        "Confirm your email for Koine",
        "\n".join(
            [
                f"Hi {user.display_name},",
                "",
                "Confirm this address to finish setting up your Koine account:",
                _link("/verify-email", token),
                "",
                "The link works once and expires in 48 hours. If you did not sign up,",
                "ignore this email.",
                "",
                "Koine",
            ]
        ),
    )


def verify_email(db: Session, token: str) -> User | None:
    user = consume_token(db, token, AuthTokenKind.VERIFY_EMAIL)
    if user is not None and user.email_verified_at is None:
        user.email_verified_at = datetime.now(UTC)
        db.commit()
    return user


def request_password_reset(request: Request, db: Session, email: str) -> None:
    """Always returns normally (the caller answers 200). The per-IP limit is
    the only one that surfaces, as a 429 that says nothing about the email."""
    ratelimit.check(request, "reset_ip", ratelimit.client_ip(request))
    email = email.lower()
    if not ratelimit.hit_quietly(request, "reset_email", email):
        return
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        return
    token = _new_token(db, user, AuthTokenKind.PASSWORD_RESET, RESET_TTL)
    send_email(
        user.email,
        "Reset your Koine password",
        "\n".join(
            [
                f"Hi {user.display_name},",
                "",
                "Use this link to choose a new password:",
                _link("/reset-password", token),
                "",
                "It works once and expires in 1 hour. Resetting signs you out everywhere.",
                "If you did not ask for this, ignore this email; your password is unchanged.",
                "",
                "Koine",
            ]
        ),
    )


def reset_password(db: Session, token: str, new_password: str) -> User:
    ensure_password_fits(new_password)  # before the token is spent
    ensure_not_breached(new_password)
    user = consume_token(db, token, AuthTokenKind.PASSWORD_RESET)
    if user is None:
        raise HTTPException(
            status_code=400, detail="This reset link is invalid or has expired. Request a new one."
        )
    now = datetime.now(UTC)
    user.password_hash = hash_password(new_password)
    if user.email_verified_at is None:  # the link reached the inbox: the address is real
        user.email_verified_at = now
    db.execute(
        update(AuthToken)
        .where(
            AuthToken.user_id == user.id,
            AuthToken.kind == AuthTokenKind.PASSWORD_RESET,
            AuthToken.used_at.is_(None),
        )
        .values(used_at=now)
    )
    db.commit()
    sessions.revoke_all(db, user.id)
    security_event(db, user, "password_changed", {"How": "password reset link"})
    return user


def change_password(
    request: Request, db: Session, user: User, current_password: str, new_password: str
) -> None:
    ratelimit.check(request, "login_email", user.email)  # before bcrypt, like login
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is wrong.")
    ensure_password_fits(new_password)
    ensure_not_breached(new_password)
    user.password_hash = hash_password(new_password)
    db.commit()
    current = sessions.load(db, request)
    sessions.revoke_all(db, user.id, except_id=current.id if current else None)
    security_event(db, user, "password_changed", {"How": "changed while signed in"})


def require_verified_email(user: User = Depends(current_user)) -> User:
    if user.email_verified_at is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NEEDS_VERIFIED_EMAIL)
    return user


# ---- JSON routes -----------------------------------------------------------------


class TokenIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)


class ResetRequestIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str = Field(min_length=10, max_length=200)
    password: str = Field(min_length=10, max_length=128)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=10, max_length=128)


@router.post("/verify-email")
def verify_email_route(payload: TokenIn, db: Session = Depends(get_db)) -> dict:
    if verify_email(db, payload.token) is None:
        raise HTTPException(status_code=400, detail="This link is invalid or has expired.")
    return {"email_verified": True}


@router.post("/verify-email/resend", status_code=status.HTTP_202_ACCEPTED)
def resend_verification(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> dict:
    if user.email_verified_at is None:
        ratelimit.check(request, "verify_resend_user", user.id)
        send_verification_email(db, user)
    return {"email_verified": user.email_verified_at is not None}


@router.post("/password-reset/request")
def password_reset_request(
    payload: ResetRequestIn, request: Request, db: Session = Depends(get_db)
) -> dict:
    request_password_reset(request, db, payload.email)
    return {"detail": RESET_REQUESTED}


@router.post("/password-reset/confirm")
def password_reset_confirm(payload: ResetIn, db: Session = Depends(get_db)) -> dict:
    reset_password(db, payload.token, payload.password)
    return {"detail": "Password changed. Log in with the new one."}


@router.post("/password/change", status_code=status.HTTP_204_NO_CONTENT)
def password_change(
    payload: ChangePasswordIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    change_password(request, db, user, payload.current_password, payload.new_password)


@router.post("/sessions/revoke-all", status_code=status.HTTP_204_NO_CONTENT)
def sign_out_everywhere(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    """Sign out everywhere, this browser included."""
    sessions.revoke_all(db, user.id)
    sessions.revoke_current(db, request)


@router.get("/sessions")
def list_sessions(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> list[dict]:
    current = sessions.load(db, request)
    return [
        {
            "id": s.id,
            "created_at": sessions.aware(s.created_at),
            "last_seen_at": sessions.aware(s.last_seen_at),
            "ip": s.ip,
            "user_agent": s.user_agent,
            "current": current is not None and s.id == current.id,
        }
        for s in sessions.active_sessions(db, user.id)
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session(
    session_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> None:
    if not sessions.revoke_one(db, user.id, session_id):
        raise HTTPException(status_code=404, detail="Session not found")


# ---- wave2:account-security: email change -------------------------------------------

EMAIL_CHANGE_TTL = timedelta(hours=1)
EMAIL_CHANGE_REQUESTED = (
    "If that address can be used, a confirmation link is on its way to it. "
    "The link lasts 1 hour; your address changes only when you open it."
)
EMAIL_CHANGE_FAILED = "This link is invalid, has expired, or the address is already in use."

# Per-user budget for change requests (each one sends two emails). Registered
# here so ratelimit.RULES' literal stays untouched; integration may move it.
ratelimit.RULES.setdefault("email_change_user", ratelimit.Rule("email_change_user", 5, 3600))

ALERT_SUBJECTS.setdefault(
    "email_change_requested", "A change of your Koine email address was requested"
)
ALERT_LEADS.setdefault(
    "email_change_requested",
    "Someone signed in to your account asked to change its email address. Nothing "
    "changes unless the link sent to the new address is opened within 1 hour.",
)
ALERT_SUBJECTS.setdefault("email_changed", "Your Koine email address was changed")
ALERT_LEADS.setdefault(
    "email_changed",
    "The email address on your Koine account was changed and every other session was "
    "signed out. This address no longer receives Koine email.",
)


@dataclass(frozen=True)
class _Recipient:
    """send_security_alert reads only these two fields: lets an alert reach the
    OLD address after the user row already holds the new one."""

    email: str
    display_name: str


def mask_email(address: str) -> str:
    """a***@example.com: enough for the owner to recognise, no more."""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def _email_taken(db: Session, address: str, except_user_id: int) -> bool:
    found = db.scalar(select(User.id).where(User.email == address, User.id != except_user_id))
    return found is not None


def _new_email_change_token(db: Session, user: User, new_email: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(UTC)
    db.add(
        AuthToken(
            user_id=user.id,
            kind=AuthTokenKind.EMAIL_CHANGE,
            token_hash=sessions.hash_token(token),
            created_at=now,
            expires_at=now + EMAIL_CHANGE_TTL,
            new_email=new_email,
        )
    )
    db.commit()
    return token


def request_email_change(
    request: Request, db: Session, user: User, password: str, new_email: str
) -> None:
    """Returns normally whether or not `new_email` is taken (the caller answers
    the same either way). Raises 401 for a wrong password, 403 without TOTP
    and a fresh step-up, 422 for the current address, 429 over a limit."""
    ratelimit.check(request, "login_email", user.email)  # before bcrypt, like login
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is wrong.")
    mfa.ensure_step_up(request, db, user)
    new_email = new_email.strip().lower()
    if new_email == user.email:
        raise HTTPException(status_code=422, detail="That is already your email address.")
    ratelimit.check(request, "email_change_user", user.id)
    taken = _email_taken(db, new_email, user.id)
    audit.record(
        db,
        AuditAction.EMAIL_CHANGE_REQUEST,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        details={"new_domain": new_email.rpartition("@")[2]},
    )
    db.commit()
    # The new address gets one email either way (a link, or a notice that it
    # already has an account), so neither the response nor the mail flow says
    # which. Mail to one address is capped like reset mail (no inbox bombing).
    if ratelimit.hit_quietly(request, "reset_email", new_email):
        if taken:
            send_email(
                new_email,
                "Someone tried to use this address on Koine",
                "\n".join(
                    [
                        "Hi,",
                        "",
                        "Someone asked to move another Koine account to this email address.",
                        "This address already has a Koine account, so nothing changed.",
                        "If you did not expect this, you can ignore this email.",
                        "",
                        "Koine",
                    ]
                ),
            )
        else:
            token = _new_email_change_token(db, user, new_email)
            send_email(
                new_email,
                "Confirm your new email for Koine",
                "\n".join(
                    [
                        f"Hi {user.display_name},",
                        "",
                        "Open this link to make this your Koine email address:",
                        _link("/account/email/confirm", token),
                        "",
                        "It works once and expires in 1 hour. Confirming signs out every",
                        "other session. If you did not ask for this, ignore this email.",
                        "",
                        "Koine",
                    ]
                ),
            )
    send_security_alert(user, "email_change_requested", {"New address": mask_email(new_email)})


def confirm_email_change(
    request: Request, db: Session, token: str, now: datetime | None = None
) -> User | None:
    """Spend the token and switch the address. None when the token is unknown,
    used or expired, or its address was taken meanwhile (the token is spent
    either way: single use)."""
    now = now or datetime.now(UTC)
    row = db.scalar(
        select(AuthToken).where(
            AuthToken.token_hash == sessions.hash_token(token),
            AuthToken.kind == AuthTokenKind.EMAIL_CHANGE,
        )
    )
    if row is None or row.used_at is not None or now >= sessions.aware(row.expires_at):
        return None
    # Conditional UPDATE (as consume_token): two clicks cannot both succeed.
    claimed = db.execute(
        update(AuthToken)
        .where(AuthToken.id == row.id, AuthToken.used_at.is_(None))
        .values(used_at=now)
    ).rowcount
    if claimed != 1:
        db.rollback()
        return None
    db.commit()
    user = db.get(User, row.user_id)
    new_email = (row.new_email or "").strip().lower()
    if user is None or not new_email or _email_taken(db, new_email, user.id):
        return None
    old = _Recipient(email=user.email, display_name=user.display_name)
    user.email = new_email
    user.email_verified_at = now  # the link reached the new inbox
    try:
        db.commit()
    except IntegrityError:  # taken between the check and the write
        db.rollback()
        return None
    # Every other outstanding token (reset links sent to the old address,
    # other change links) dies with the old address.
    db.execute(
        update(AuthToken)
        .where(AuthToken.user_id == user.id, AuthToken.used_at.is_(None))
        .values(used_at=now)
    )
    db.commit()
    current = sessions.load(db, request)
    keep = current.id if current is not None and current.user_id == user.id else None
    sessions.revoke_all(db, user.id, except_id=keep, now=now)
    audit.record(
        db,
        AuditAction.EMAIL_CHANGE,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
        details={
            "old_domain": old.email.rpartition("@")[2],
            "new_domain": new_email.rpartition("@")[2],
        },
    )
    db.commit()
    send_security_alert(old, "email_changed", {"New address": mask_email(new_email)})
    return user


class EmailChangeIn(BaseModel):
    new_email: EmailStr
    current_password: str = Field(max_length=128)


# No prefix (/account/...). security_pages.router includes it, and main.py
# already includes that router; integration may include this one directly.
email_router = APIRouter(tags=["account"])


@email_router.post("/account/email", status_code=status.HTTP_202_ACCEPTED)
def email_change_route(
    payload: EmailChangeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    request_email_change(request, db, user, payload.current_password, str(payload.new_email))
    return {"detail": EMAIL_CHANGE_REQUESTED}
