"""TOTP second factor, recovery codes and step-up (P3).

- Enrol: a random 160-bit base32 secret, vault-encrypted at rest
  (vault.encrypt_secret), shown once as an otpauth:// URI, a QR code (inline
  SVG, no script) and the raw secret. Enrolment completes with the first valid
  code, which also returns 10 single-use recovery codes (stored as SHA-256).
- Verify: RFC 6238 (SHA-1, 6 digits, 30 s steps), accepting the previous,
  current and next step for clock drift. The highest accepted step is stored
  and nothing at or below it is accepted again, so a code cannot be replayed.
- Lock: 5 wrong codes within a 5-minute window lock code entry for 15
  minutes and email the user (ratelimit rule `totp_user`).
- Step-up: a successful code stamps the session's `step_up_at`;
  `require_step_up` accepts it for 10 minutes. It guards POST /venues,
  credential rotation, disconnect and TOTP disable. A user without TOTP gets a
  403 pointing at /dashboard/security.
- Login: with TOTP on, the password step yields an `mfa_pending` session and
  POST /auth/totp/verify (or /login/2fa) finishes it. Every successful code
  rotates the session id.
"""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

import pyotp
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from alphamarket import ratelimit, sessions
from alphamarket.auth import current_user, finish_login, totp_enabled
from alphamarket.db import get_db
from alphamarket.email import send_security_alert
from alphamarket.models import RecoveryCode, User, UserTotp
from alphamarket.vault import decrypt_secret, encrypt_secret

router = APIRouter(prefix="/auth/totp", tags=["auth"])

ISSUER = "Koine"
STEP_SECONDS = 30
DRIFT_STEPS = 1  # accept the previous and next 30 s step too
LOCKOUT = timedelta(minutes=15)
RECOVERY_CODE_COUNT = 10
RECOVERY_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"  # base32, lowercase

NEEDS_TOTP = "Two-factor authentication is required for this. Turn it on at /dashboard/security."
NEEDS_STEP_UP = (
    "Confirm a current two-factor code first (at /dashboard/security or "
    "POST /auth/totp/step-up); confirmations last 10 minutes."
)


# ---- pure helpers ------------------------------------------------------------


def time_step(at: datetime) -> int:
    """RFC 6238 counter: floor(unix seconds / 30). E.g. 59 s -> 1."""
    return int(at.timestamp()) // STEP_SECONDS


def code_at_step(secret: str, step: int) -> str:
    return pyotp.TOTP(secret).at(step * STEP_SECONDS)


def match_step(secret: str, code: str, at: datetime, last_used_step: int | None) -> int | None:
    """The time step `code` is valid for within the drift window, if it is
    newer than `last_used_step`; else None."""
    code = code.strip().replace(" ", "")
    if len(code) != 6 or not code.isdigit():
        return None
    now_step = time_step(at)
    for step in range(now_step - DRIFT_STEPS, now_step + DRIFT_STEPS + 1):
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(code_at_step(secret, step), code):
            return step
    return None


def new_recovery_code() -> str:
    """16 base32 characters (80 bits) as xxxx-xxxx-xxxx-xxxx."""
    raw = "".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(16))
    return "-".join(raw[i : i + 4] for i in range(0, 16, 4))


def normalize_recovery_code(code: str) -> str:
    return "".join(ch for ch in code.lower() if ch.isalnum())


def hash_recovery_code(code: str) -> str:
    return hashlib.sha256(normalize_recovery_code(code).encode()).hexdigest()


def provisioning_uri(secret: str, email: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """Inline SVG of the otpauth URI (segno, pure python; no script)."""
    import segno

    return segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#141518")


# ---- state -------------------------------------------------------------------


def _record(db: Session, user: User) -> UserTotp | None:
    return db.get(UserTotp, user.id)


def start_enrolment(db: Session, user: User) -> str:
    """Create (or replace) a pending secret; returns the plaintext secret."""
    row = _record(db, user)
    if row is not None and row.enabled_at is not None:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already on.")
    secret = pyotp.random_base32(32)
    if row is None:
        row = UserTotp(user_id=user.id, secret_encrypted=encrypt_secret(secret))
        db.add(row)
    else:
        row.secret_encrypted = encrypt_secret(secret)
        row.last_used_step = None
        row.locked_until = None
    db.commit()
    return secret


def pending_secret(db: Session, user: User) -> str | None:
    row = _record(db, user)
    if row is None or row.enabled_at is not None:
        return None
    return decrypt_secret(row.secret_encrypted)


def replace_recovery_codes(db: Session, user: User) -> list[str]:
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    codes = [new_recovery_code() for _ in range(RECOVERY_CODE_COUNT)]
    db.add_all(RecoveryCode(user_id=user.id, code_hash=hash_recovery_code(c)) for c in codes)
    db.commit()
    return codes


def remaining_recovery_codes(db: Session, user: User) -> int:
    return len(
        db.scalars(
            select(RecoveryCode.id).where(
                RecoveryCode.user_id == user.id, RecoveryCode.used_at.is_(None)
            )
        ).all()
    )


def _use_recovery_code(db: Session, user: User, code: str, now: datetime) -> bool:
    if len(normalize_recovery_code(code)) != 16:
        return False
    row = db.scalar(
        select(RecoveryCode).where(
            RecoveryCode.user_id == user.id,
            RecoveryCode.code_hash == hash_recovery_code(code),
            RecoveryCode.used_at.is_(None),
        )
    )
    if row is None:
        return False
    row.used_at = now
    return True


def _fail(request: Request, db: Session, user: User, row: UserTotp, now: datetime) -> None:
    """Count a wrong code; the 5th inside the window locks entry for 15 min."""
    rule = ratelimit.RULES["totp_user"]
    decision = ratelimit.get_limiter(request.app).hit(
        f"{rule.name}:{user.id}", rule.limit, rule.window_seconds, now
    )
    if decision.count >= rule.limit:
        row.locked_until = now + LOCKOUT
        db.commit()
        send_security_alert(user, "totp_lockout", {"Locked until": _fmt(row.locked_until)})
        ratelimit.too_many(
            int(LOCKOUT.total_seconds()), "Too many wrong codes. Try again in 15 minutes."
        )


def _fmt(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def check_code(
    request: Request,
    db: Session,
    user: User,
    code: str,
    *,
    allow_recovery: bool = True,
    enrolling: bool = False,
    now: datetime | None = None,
) -> bool:
    """Verify a TOTP (or recovery) code for `user`. True on success (commits
    the replay guard). False on a wrong code; raises 429 while locked or when
    this failure trips the lock."""
    now = now or datetime.now(UTC)
    # Lock the row (Postgres; SQLite ignores FOR UPDATE and serialises writes
    # anyway) so two concurrent requests carrying the same code cannot both
    # pass the replay guard, and one recovery code cannot be spent twice.
    row = db.get(UserTotp, user.id, with_for_update=True, populate_existing=True)
    if row is None or (row.enabled_at is None) != enrolling:
        return False
    locked_until = sessions.aware(row.locked_until)
    if locked_until is not None and now < locked_until:
        ratelimit.too_many(
            max(1, int((locked_until - now).total_seconds())),
            "Too many wrong codes. Try again in 15 minutes.",
        )
    step = match_step(decrypt_secret(row.secret_encrypted), code, now, row.last_used_step)
    if step is not None:
        row.last_used_step = step
        db.commit()
        return True
    if allow_recovery and not enrolling and _use_recovery_code(db, user, code, now):
        db.commit()
        send_security_alert(
            user, "recovery_code_used", {"Codes left": str(remaining_recovery_codes(db, user))}
        )
        return True
    _fail(request, db, user, row, now)
    return False


def confirm_enrolment(request: Request, db: Session, user: User, code: str) -> list[str]:
    if not check_code(request, db, user, code, enrolling=True):
        raise HTTPException(status_code=422, detail="That code is not valid. Try the next one.")
    row = _record(db, user)
    row.enabled_at = datetime.now(UTC)
    db.commit()
    codes = replace_recovery_codes(db, user)
    sessions.issue(db, request, user, step_up=True)  # rotate; this counts as a step-up
    send_security_alert(user, "totp_enabled")
    return codes


def step_up(request: Request, db: Session, user: User, code: str) -> None:
    if not totp_enabled(db, user):
        raise HTTPException(status_code=403, detail=NEEDS_TOTP)
    if not check_code(request, db, user, code):
        raise HTTPException(status_code=401, detail="Invalid code")
    sessions.issue(db, request, user, step_up=True)


def complete_login(request: Request, db: Session, code: str) -> User:
    pending = sessions.pending(db, request)
    if pending is None:
        raise HTTPException(status_code=401, detail="Sign in with your password first.")
    user = db.get(User, pending.user_id)
    if user is None or not check_code(request, db, user, code):
        raise HTTPException(status_code=401, detail="Invalid code")
    finish_login(db, request, user, step_up=True)
    return user


def disable(request: Request, db: Session, user: User) -> None:
    """Caller has already enforced a recent step-up."""
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user.id))
    db.execute(delete(UserTotp).where(UserTotp.user_id == user.id))
    db.commit()
    send_security_alert(user, "totp_disabled")


# ---- dependencies --------------------------------------------------------------


def ensure_step_up(request: Request, db: Session, user: User) -> None:
    """403 unless the user has TOTP and confirmed a code on this session in
    the last 10 minutes."""
    if not totp_enabled(db, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NEEDS_TOTP)
    if not sessions.has_recent_step_up(sessions.load(db, request)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NEEDS_STEP_UP)


def require_step_up(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> User:
    ensure_step_up(request, db, user)
    return user


# ---- JSON routes -----------------------------------------------------------------


class CodeIn(BaseModel):
    code: str = Field(min_length=6, max_length=40)


class OptionalCodeIn(BaseModel):
    code: str | None = Field(default=None, max_length=40)


@router.post("/enroll")
def enroll(db: Session = Depends(get_db), user: User = Depends(current_user)) -> dict:
    """Start enrolment. The secret is returned once, to its owner, so an
    authenticator app can be set up; it is stored only vault-encrypted."""
    secret = start_enrolment(db, user)
    return {"secret": secret, "provisioning_uri": provisioning_uri(secret, user.email)}


@router.post("/confirm")
def confirm(
    payload: CodeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> dict:
    return {"enabled": True, "recovery_codes": confirm_enrolment(request, db, user, payload.code)}


@router.post("/verify")
def verify(payload: CodeIn, request: Request, db: Session = Depends(get_db)) -> dict:
    """Second login step (the cookie names an mfa_pending session)."""
    from alphamarket.auth import user_out

    return user_out(db, complete_login(request, db, payload.code))


@router.post("/step-up", status_code=status.HTTP_204_NO_CONTENT)
def step_up_route(
    payload: CodeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    step_up(request, db, user, payload.code)


@router.post("/disable", status_code=status.HTTP_204_NO_CONTENT)
def disable_route(
    payload: OptionalCodeIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> None:
    """Needs TOTP: a code in the body, or a step-up in the last 10 minutes."""
    if payload.code:
        step_up(request, db, user, payload.code)
    ensure_step_up(request, db, user)
    disable(request, db, user)


@router.post("/recovery-codes")
def regenerate_recovery_codes(
    request: Request, db: Session = Depends(get_db), user: User = Depends(require_step_up)
) -> dict:
    return {"recovery_codes": replace_recovery_codes(db, user)}


# ---- venue-connect gate ------------------------------------------------------------


def connect_block(request: Request, db: Session, user: User) -> tuple[str, str] | None:
    """Why `user` may not connect a venue right now, as (kind, message), or
    None. Kinds: "totp" (not enrolled), "email" (unverified), "step_up"
    (no code confirmed in the last 10 minutes)."""
    from alphamarket.account import NEEDS_VERIFIED_EMAIL

    if not totp_enabled(db, user):
        return "totp", NEEDS_TOTP
    if user.email_verified_at is None:
        return "email", NEEDS_VERIFIED_EMAIL
    if not sessions.has_recent_step_up(sessions.load(db, request)):
        return "step_up", NEEDS_STEP_UP
    return None


def require_connect_ready(
    request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)
) -> User:
    """POST /venues: TOTP on, email verified, step-up within 10 minutes."""
    block = connect_block(request, db, user)
    if block is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=block[1])
    return user
