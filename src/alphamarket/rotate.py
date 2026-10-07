"""Replace a venue key on an existing account (wave2, w2-account-security).

Plan D17. Gates: TOTP + verified email + a fresh step-up
(mfa.require_connect_ready), the "venues_user" rate limit, and ownership
(another user's account is a 404). The new key is proven exactly as connect
proves one (check_connection, then scopecheck.prove: a writable key is
audited as CREDENTIAL_REJECTED, committed, and refused with 422, and the
account is left untouched). Then: credentials.store (which erases the
previous credential), the evidence on the account, audit SCOPE_CHECK
(trigger "rotate") and CREDENTIAL_ROTATE, status ACTIVE, an urgent SYNC and a
one-off RECONCILE, and a security alert to the owner.

Rotation never re-anchors. The anchor is what the track record is measured
from; a new key on the SAME venue account continues the same ledger, and a
key for a DIFFERENT account diverges from it, so the one-off reconcile
quarantines it (reconcile.py) instead of silently starting a new record.

Only key-type credentials rotate here (RSA PEM, key + secret, report token).
OAuth grants, aggregator connections and public addresses have no key to
paste: those accounts are reconnected from the connect page (422).

The verification steps duplicate venues.connect_account on purpose: this
module imports only public names from venues (ensure_venue_open) and
scopecheck (prove, apply_evidence, evidence_json), and a shared
_verify_new_credential() is extracted at integration.

Routes:
  POST /venues/{account_id}/rotate              JSON
  GET/POST /dashboard/accounts/{account_id}/rotate  HTML form
"""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import audit, credentials, email, mfa, ratelimit
from alphamarket.audit import AuditAction
from alphamarket.auth import current_user, optional_user
from alphamarket.connectors.base import CredentialKind
from alphamarket.connectors.registry import get_profile
from alphamarket.db import get_db
from alphamarket.ingest import is_asset_venue
from alphamarket.jobs import PRIORITY_URGENT, JobKind, enqueue
from alphamarket.models import (
    AccountSchedule,
    User,
    VenueAccount,
    VenueAccountStatus,
    VenueCredential,
)
from alphamarket.ratelimit import rate_limit
from alphamarket.scopecheck import apply_evidence, evidence_json
from alphamarket.venue_limits import VenueLimiter, get_web_limiter
from alphamarket.venues import ensure_venue_open, verify_new_credential

logger = logging.getLogger(__name__)
router = APIRouter(tags=["rotate"])

# Credentials the user pastes; everything else is reconnected instead.
ROTATABLE_KINDS = frozenset(
    {
        CredentialKind.RSA_PEM,
        CredentialKind.KEY_SECRET,
        CredentialKind.KEY_SECRET_PASSPHRASE,
        CredentialKind.REPORT_TOKEN,
    }
)
ROTATABLE_STATUSES = (
    VenueAccountStatus.ACTIVE,
    VenueAccountStatus.ERROR,
    VenueAccountStatus.SUSPENDED,
)
WRITABLE_KEY = (
    "This API key has write (trading) scope. Koine only accepts read-only keys: "
    "create a new key with read-only scope and use that."
)


class RotateIn(BaseModel):
    # Same field names as POST /venues (ConnectIn).
    key_id: str = Field(min_length=1, max_length=120)
    secret: str | None = None
    private_key_pem: str | None = None  # the Kalshi field name, accepted as an alias
    # Which venue account the key reads when one key spans several; None keeps
    # the account's current one.
    account_ref: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _merge_secret(self) -> "RotateIn":
        if self.secret is None and self.private_key_pem:
            self.secret = self.private_key_pem
        self.private_key_pem = None
        return self


class RotateOut(BaseModel):
    """Deliberately excludes key material and the key id."""

    id: int
    venue: str
    venue_env: str
    status: str
    scope_checked_at: datetime | None = None
    last_sync_error: str | None = None
    previous_status: str
    sync_job_id: int | None = None
    reconcile_job_id: int | None = None


def owned_account(db: Session, account_id: int, user: User) -> VenueAccount:
    account = db.get(VenueAccount, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(status_code=404, detail="Venue account not found")
    return account


def rotate_block(account: VenueAccount) -> str | None:
    """Why this account's key cannot be replaced here, or None."""
    profile = get_profile(account.venue)
    if profile.credential not in ROTATABLE_KINDS:
        how = (
            "with its Connect button"
            if profile.credential is CredentialKind.OAUTH2
            else "from the connect page"
        )
        return (
            f"{profile.display_name} has no key to replace. Disconnect this account and "
            f"reconnect {profile.display_name} {how}."
        )
    if account.status not in ROTATABLE_STATUSES:
        return f"This account is {account.status}; connect it again from the connect page."
    return None


def _previous_account_ref(db: Session, account: VenueAccount) -> str | None:
    """The newest credential's account_ref, revoked or not (a suspended
    account's credential is erased but its account_ref is not a secret)."""
    return db.scalar(
        select(VenueCredential.account_ref)
        .where(VenueCredential.venue_account_id == account.id)
        .order_by(VenueCredential.id.desc())
        .limit(1)
    )


def rotate_credential(
    db: Session,
    user: User,
    account_id: int,
    payload: RotateIn,
    *,
    now: datetime | None = None,
    limiter: VenueLimiter | None = None,
) -> dict:
    """Verify, store and activate a replacement key. Raises HTTPException with
    user-safe detail; nothing about the account changes on any refusal. Web
    callers pass the outbound `limiter`: the venue check spends its buckets and
    a busy venue is a 503 (Retry-After), with the old key still in place."""
    account = owned_account(db, account_id, user)
    block = rotate_block(account)
    if block is not None:
        status = 422 if account.status in ROTATABLE_STATUSES else 409
        raise HTTPException(status_code=status, detail=block)
    profile = get_profile(account.venue)
    ensure_venue_open(account.venue)
    if not payload.secret:
        raise HTTPException(status_code=422, detail=f"{profile.secret_label} is required.")
    secret = payload.secret
    account_ref = payload.account_ref or _previous_account_ref(db, account)

    _connector, evidence = verify_new_credential(
        db,
        user,
        account.venue,
        account.venue_env,
        payload.key_id,
        secret,
        account_ref,
        reject_target=("venue_account", account.id),
        reject_details={"trigger": "rotate", "venue": account.venue},
        writable_detail=WRITABLE_KEY,
        limiter=limiter,
    )

    previous_status = str(account.status)
    previous = credentials.active_credential(db, account)
    cred = credentials.store(
        db,
        account,
        secret,
        kind=profile.credential,
        account_ref=account_ref,
        actor_user_id=user.id,
    )
    account.encrypted_private_key = None  # the pre-0012 column never outlives a rotation
    account.key_id = payload.key_id
    apply_evidence(account, evidence)
    audit.record(
        db,
        AuditAction.SCOPE_CHECK,
        actor_user_id=user.id,
        target_type="venue_account",
        target_id=account.id,
        details={"trigger": "rotate", "venue": account.venue, **evidence_json(evidence)},
    )
    audit.record(
        db,
        AuditAction.CREDENTIAL_ROTATE,
        actor_user_id=user.id,
        target_type="venue_account",
        target_id=account.id,
        details={
            "trigger": "user",
            "venue": account.venue,
            "credential_id": cred.id,
            "replaced_credential_id": previous.id if previous is not None else None,
            "previous_status": previous_status,
        },
    )
    account.status = VenueAccountStatus.ACTIVE
    account.last_sync_error = None

    now = now or datetime.now(UTC)
    sync_job = enqueue(db, JobKind.SYNC, account_id=account.id, priority=PRIORITY_URGENT, now=now)
    if is_asset_venue(account.venue) and not _backfill_done(db, account):
        # A backfill cut short (a suspension completes the running job
        # unfinished) is queued again; the default key dedupes a live one.
        enqueue(db, JobKind.BACKFILL, account_id=account.id, priority=PRIORITY_URGENT, now=now)
    reconcile_job = enqueue(
        db,
        JobKind.RECONCILE,
        account_id=account.id,
        dedupe_key=f"reconcile:{account.id}:rotate:{int(now.timestamp())}",
        once=True,
        now=now,
    )
    db.commit()
    logger.info("venue account %s: key replaced (was %s)", account.id, previous_status)
    email.send_security_alert(
        user,
        "key_rotated",
        {"Venue": profile.display_name, "Account": f"#{account.id}"},
    )
    return {
        "id": account.id,
        "venue": account.venue,
        "venue_env": account.venue_env,
        "status": str(account.status),
        "scope_checked_at": account.scope_checked_at,
        "last_sync_error": account.last_sync_error,
        "previous_status": previous_status,
        "sync_job_id": sync_job,
        "reconcile_job_id": reconcile_job,
    }


def profile_label(account: VenueAccount) -> str:
    return get_profile(account.venue).key_label.lower()


def _backfill_done(db: Session, account: VenueAccount) -> bool:
    done = db.scalar(
        select(AccountSchedule.backfill_done_at).where(
            AccountSchedule.venue_account_id == account.id
        )
    )
    return done is not None


# ---- JSON ---------------------------------------------------------------------------


@router.post(
    "/venues/{account_id}/rotate",
    response_model=RotateOut,
    # Same gate and limit as POST /venues: TOTP, verified email, fresh step-up.
    dependencies=[
        Depends(mfa.require_connect_ready),
        Depends(rate_limit("venues_user", by="user")),
    ],
)
def rotate(
    account_id: int,
    payload: RotateIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return rotate_credential(db, user, account_id, payload, limiter=get_web_limiter(request.app))


# ---- HTML ---------------------------------------------------------------------------


def _page(
    request: Request,
    db: Session,
    user: User,
    account: VenueAccount,
    *,
    error: str | None = None,
    status_code: int = 200,
):
    from alphamarket.pages import _render  # pages imports most modules; keep this lazy

    resp = _render(
        request,
        "rotate_key.html",
        user=user,
        account=account,
        profile=get_profile(account.venue),
        gate=mfa.connect_block(request, db, user),
        blocked=rotate_block(account),
        error=error,
    )
    resp.status_code = status_code
    return resp


@router.get(
    "/dashboard/accounts/{account_id}/rotate",
    response_class=HTMLResponse,
    include_in_schema=False,
)
def rotate_page(request: Request, account_id: int, db: Session = Depends(get_db)):
    user = optional_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    account = owned_account(db, account_id, user)
    return _page(request, db, user, account)


@router.post("/dashboard/accounts/{account_id}/rotate", include_in_schema=False)
def rotate_form(
    request: Request,
    account_id: int,
    key_id: str = Form(),
    secret: str = Form(""),
    db: Session = Depends(get_db),
):
    user = optional_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    account = owned_account(db, account_id, user)
    gate = mfa.connect_block(request, db, user)
    if gate is not None:
        return _page(request, db, user, account, error=gate[1], status_code=403)
    try:
        ratelimit.check(request, "venues_user", user.id)
        key_id = key_id.strip()
        if not 1 <= len(key_id) <= 120:
            raise HTTPException(status_code=422, detail=f"Enter the {profile_label(account)}.")
        payload = RotateIn(key_id=key_id, secret=secret or None)
        rotate_credential(db, user, account_id, payload, limiter=get_web_limiter(request.app))
    except HTTPException as exc:
        db.rollback()
        account = owned_account(db, account_id, user)
        code = exc.status_code if exc.status_code in (403, 409, 422, 429, 503) else 400
        resp = _page(request, db, user, account, error=str(exc.detail), status_code=code)
        if code == 503 and exc.headers:  # "busy": say when to come back
            resp.headers["Retry-After"] = exc.headers.get("Retry-After", "60")
        return resp
    return RedirectResponse("/dashboard", status_code=303)
