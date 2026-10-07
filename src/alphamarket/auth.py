"""Auth: signup, login (one or two steps), logout, me.

bcrypt password hashes; DB-backed sessions (sessions.py) with an opaque
cookie; TOTP second factor and step-up (mfa.py); email verification and
password reset (account.py); inbound rate limits (ratelimit.py) checked
before any bcrypt work so login cannot be used to burn CPU.

`current_user` stays the dependency every other module uses.
"""

from functools import cache

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import audit, ratelimit, sessions
from alphamarket.db import get_db
from alphamarket.email import send_security_alert
from alphamarket.models import User, UserTotp
from alphamarket.passwords import ensure_not_breached

router = APIRouter(prefix="/auth", tags=["auth"])


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(min_length=2, max_length=80)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserOut(BaseModel):
    id: int
    email: EmailStr
    display_name: str
    email_verified: bool = False
    totp_enabled: bool = False


class LoginOut(UserOut):
    # True when the password was right and a TOTP code is still needed
    # (POST /auth/totp/verify). Nothing else is authenticated until then.
    mfa_required: bool = False


# bcrypt only reads 72 bytes, and bcrypt>=5 raises ValueError past that. A
# 128-character limit is not enough: 80 ASCII chars, or 25 three-byte chars,
# already exceed it.
MAX_PASSWORD_BYTES = 72
PASSWORD_TOO_LONG = f"Use a password of at most {MAX_PASSWORD_BYTES} bytes."


def ensure_password_fits(password: str) -> None:
    """422 before any token is spent or hash computed on a password bcrypt refuses."""
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise HTTPException(status_code=422, detail=PASSWORD_TOO_LONG)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    # No stored hash can come from a longer password (hash_password refuses it).
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(password.encode(), password_hash.encode())


@cache
def _dummy_hash() -> str:
    """Checked against when the email is unknown, so both failure paths cost
    one bcrypt and response time does not reveal which accounts exist."""
    return hash_password("koine-timing-equalizer")


def totp_enabled(db: Session, user: User) -> bool:
    row = db.get(UserTotp, user.id)
    return row is not None and row.enabled_at is not None


def user_out(db: Session, user: User, **extra) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "email_verified": user.email_verified_at is not None,
        "totp_enabled": totp_enabled(db, user),
        **extra,
    }


def optional_user(request: Request, db: Session) -> User | None:
    """The logged-in user, or None (pages that render for everyone)."""
    sess = sessions.load(db, request)
    if sess is None:
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.is_system:  # belt and braces: no session works for it
        return None
    sessions.touch(db, sess)
    return user


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = optional_user(request, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not logged in")
    return user


def finish_login(db: Session, request: Request, user: User, *, step_up: bool = False) -> None:
    """Start a full session (rotating any presented one) and, when this
    user has signed in before but never from this IP, email an alert."""
    sess = sessions.issue(db, request, user, step_up=step_up)
    audit.record(db, "security.login", actor_user_id=user.id, target_type="user", target_id=user.id)
    db.commit()
    if sessions.has_prior_sessions(db, user.id, exclude_id=sess.id) and not (
        sessions.seen_ip_before(db, user.id, sess.ip, exclude_id=sess.id)
    ):
        send_security_alert(
            user,
            "new_login",
            {"IP address": sess.ip or "unknown", "Device": sess.user_agent or "unknown"},
        )


@router.post("/signup", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupIn, request: Request, db: Session = Depends(get_db)) -> dict:
    from alphamarket.account import send_verification_email

    ratelimit.check(request, "signup_ip", ratelimit.client_ip(request))
    email = payload.email.lower()
    exists = db.scalar(select(User).where(User.email == email))
    if exists:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email already registered")
    ensure_password_fits(payload.password)
    ensure_not_breached(payload.password)
    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    sessions.issue(db, request, user)
    send_verification_email(db, user)
    return user_out(db, user)


@router.post("/login", response_model=LoginOut)
def login(payload: LoginIn, request: Request, db: Session = Depends(get_db)) -> dict:
    email = payload.email.lower()
    # Both limits before bcrypt: a flood costs a counter increment, not a hash.
    ratelimit.check(request, "login_ip", ratelimit.client_ip(request))
    ratelimit.check(request, "login_email", email)
    user = db.scalar(select(User).where(User.email == email))
    if user is not None and user.is_system:
        user = None  # the watchlist's system user never logs in: same path as unknown
    ok = verify_password(payload.password, user.password_hash if user else _dummy_hash())
    # Same error for unknown email and wrong password: do not leak which one it was.
    if user is None or not ok:
        # The attempted address is not stored: it may be a typo of someone else's.
        audit.record(
            db,
            "security.login_failed",
            actor_user_id=user.id if user else None,
            target_type="user" if user else None,
            target_id=user.id if user else None,
        )
        db.commit()
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")
    if totp_enabled(db, user):
        sessions.issue(db, request, user, mfa_pending=True)
        return user_out(db, user, mfa_required=True)
    finish_login(db, request, user)
    return user_out(db, user)


def logout_current(request: Request, db: Session) -> None:
    sessions.revoke_current(db, request)
    if "session" in request.scope:  # legacy signed cookie: nothing auth lives there now
        request.session.clear()


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: Session = Depends(get_db)) -> None:
    logout_current(request, db)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    return user_out(db, user)
