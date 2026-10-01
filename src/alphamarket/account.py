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
"""

import secrets
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from alphamarket import ratelimit, sessions
from alphamarket.auth import current_user, ensure_password_fits, hash_password, verify_password
from alphamarket.config import get_settings
from alphamarket.db import get_db
from alphamarket.email import security_event, send_email
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
