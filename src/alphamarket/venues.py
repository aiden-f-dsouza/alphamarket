"""Venue connect flow (M1.2, generalized for several venues in M7).

POST /venues: the user identifies an account (Kalshi: key id + private key
PEM; Polymarket: wallet address, no secret). We verify it works, reject
keys with write scope (invariant #4 / SEC-1 at MVP scale), encrypt any
secret at rest, create the account's strategy, and run the first historical
import inline so the user sees their data immediately.

Ownership: a valid read-only key proves the account is the user's. A public
wallet address proves nothing, so those accounts get a claim code the user
puts in their venue profile bio; POST /venues/{id}/claim checks for it and
only claimed accounts ever reach a public page.

Secrets are accepted in the request body, used, encrypted, and never appear
in any response, log line, or error detail.
"""

import json
import logging
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import audit, credentials
from alphamarket.audit import AuditAction
from alphamarket.auth import current_user
from alphamarket.config import get_settings
from alphamarket.connectors.base import CredentialKind, OwnershipProof
from alphamarket.connectors.registry import SUPPORTED_VENUES, build_connector, get_profile
from alphamarket.db import get_db
from alphamarket.email import send_security_alert
from alphamarket.ingest import SyncResult, is_asset_venue, sync_account
from alphamarket.jobs import PRIORITY_URGENT, JobKind, enqueue
from alphamarket.mfa import require_connect_ready, require_step_up
from alphamarket.models import (
    AccountSchedule,
    Strategy,
    User,
    VenueAccount,
    VenueAccountStatus,
    utcnow,
)
from alphamarket.ratelimit import rate_limit
from alphamarket.reconcile import (
    reconcilable,
    reconcile_account,
    store_anchor_balances,
    take_anchor,
)
from alphamarket.scopecheck import apply_evidence, evidence_json, prove

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/venues", tags=["venues"])


class ConnectIn(BaseModel):
    venue: str = "kalshi"
    # The public identifier: Kalshi key id, Polymarket wallet address.
    key_id: str = Field(min_length=1, max_length=120)
    # The credential, when the venue has one. `private_key_pem` is the
    # original Kalshi field name and is accepted as an alias.
    secret: str | None = None
    private_key_pem: str | None = None
    strategy_name: str = Field(min_length=2, max_length=120)
    # Which venue account to read when one credential spans several
    # (VenueConnector.account_ref); None when key and account are 1:1.
    account_ref: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def _merge_secret(self) -> "ConnectIn":
        if self.secret is None and self.private_key_pem:
            self.secret = self.private_key_pem
        self.private_key_pem = None  # one place holds it from here on
        return self


class VenueAccountOut(BaseModel):
    """Public view of a connection. Deliberately excludes key material."""

    id: int
    venue: str
    venue_env: str
    status: str
    scope_checked_at: datetime | None = None
    last_synced_at: datetime | None = None
    last_sync_error: str | None = None
    strategy_id: int | None = None
    strategy_name: str | None = None
    claimed: bool = True
    claim_code: str | None = None  # only set while a profile-code claim is pending
    verification_tier: str = "reconciled"


class ConnectOut(VenueAccountOut):
    imported_fills: int
    imported_settlements: int


def _to_out(account: VenueAccount, strategy: Strategy | None) -> dict:
    return {
        "id": account.id,
        "venue": account.venue,
        "venue_env": account.venue_env,
        "status": account.status,
        "scope_checked_at": account.scope_checked_at,
        "last_synced_at": account.last_synced_at,
        "last_sync_error": account.last_sync_error,
        "strategy_id": strategy.id if strategy else None,
        "strategy_name": strategy.name if strategy else None,
        "claimed": account.claimed,
        "claim_code": None if account.claimed else account.claim_code,
        "verification_tier": verification_tier(account),
    }


def verification_tier(account: VenueAccount) -> str:
    """What the platform can vouch for on this account (see methodology):
    `reconciled`: fills verified and cash balance checked nightly;
    `fills_only`: fills verified, the venue exposes no balance to check;
    `unclaimed`: ownership not yet proven, nothing shown publicly."""
    if not account.claimed:
        return "unclaimed"
    return "reconciled" if get_profile(account.venue).balance_observable else "fills_only"


def new_claim_code() -> str:
    return "koine-" + secrets.token_hex(4)


def slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return base[:100] or "strategy"


def venue_closed_reason(venue: str) -> str | None:
    """Why `venue` cannot be connected yet, or None. Invariant 10: a venue
    whose terms need written approval stays closed until the approval is on
    file (the profile flag flips) or a non-prod deployment opts in."""
    profile = get_profile(venue)
    if profile.requires_partner_approval and not get_settings().enable_unapproved_venues:
        return (
            f"{profile.display_name} is not open for connections yet: Koine is waiting "
            f"for {profile.display_name}'s written approval to show account data."
        )
    return None


def oauth_expires_at(secret: str) -> datetime | None:
    """wave2: an OAuth token record's absolute expiry (oauth.token_record), so
    the refresh tick can find tokens due without decrypting every credential."""
    try:
        record = json.loads(secret)
    except ValueError:
        return None
    raw = record.get("expires_at") if isinstance(record, dict) else None
    return datetime.fromisoformat(raw) if raw else None


def ensure_venue_open(venue: str) -> None:
    reason = venue_closed_reason(venue)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)


def connect_account(
    db: Session, user: User, payload: ConnectIn, *, oauth_grant: bool = False
) -> dict:
    """Validate, scope-check, encrypt, anchor, create the strategy, first
    import. Raises HTTPException with user-safe detail on any failure.
    `oauth_grant` is set only by oauth.callback: OAuth venues never accept a
    pasted token."""
    if payload.venue not in SUPPORTED_VENUES:
        raise HTTPException(status_code=422, detail=f"Unsupported venue: {payload.venue}")
    profile = get_profile(payload.venue)
    ensure_venue_open(payload.venue)
    if profile.credential is CredentialKind.OAUTH2 and not oauth_grant:
        raise HTTPException(
            status_code=422,
            detail=f"Connect {profile.display_name} with its Connect button (OAuth).",
        )
    if profile.needs_secret and not payload.secret:
        raise HTTPException(status_code=422, detail=f"{profile.secret_label} is required.")
    secret = payload.secret if profile.needs_secret else None

    venue_env = get_settings().venue_env(payload.venue)
    try:
        connector = build_connector(
            payload.venue, venue_env, payload.key_id, secret, account_ref=payload.account_ref
        )
        connector.check_connection()
    except HTTPException:
        raise
    except Exception:
        # Deliberately generic: no exception detail can be allowed to echo key
        # material back, so none of it leaves the server.
        raise HTTPException(
            status_code=422,
            detail=(
                "Could not authenticate with the venue using this key."
                if profile.needs_secret
                else f"{profile.display_name} does not know this account."
            ),
        ) from None

    evidence = prove(connector)
    if evidence.read_only is False:
        # Audited and committed before refusing: a writable key offered to
        # Koine is a security event even though nothing is stored.
        audit.record(
            db,
            AuditAction.CREDENTIAL_REJECTED,
            actor_user_id=user.id,
            target_type="venue",
            target_id=payload.venue,
            details={"reason": "scope_writable", **evidence_json(evidence)},
        )
        db.commit()
        raise HTTPException(
            status_code=422,
            detail=(
                "This API key has write (trading) scope. Koine only accepts "
                "read-only keys: create a new key with read-only scope and connect that."
            ),
        )

    # Reconciliation anchor: the balance at this moment is what every later
    # equity number is measured from (see ledger.py).
    anchor = take_anchor(connector)

    key_proves_ownership = profile.ownership is OwnershipProof.KEY
    account = VenueAccount(
        user_id=user.id,
        venue=payload.venue,
        venue_env=venue_env,
        key_id=payload.key_id.lower() if not profile.needs_secret else payload.key_id,
        # The key goes to venue_credentials (credentials.store below); the
        # legacy column is no longer written.
        encrypted_private_key=None,
        status=VenueAccountStatus.ACTIVE,
        anchor_balance_micros=anchor.balance_micros,
        anchor_at=anchor.at,
        anchor_positions=anchor.positions,
        claim_code=None if key_proves_ownership else new_claim_code(),
        claimed_at=utcnow() if key_proves_ownership else None,
    )
    # Undetermined evidence leaves scope_checked_at unset ("scope unverified").
    apply_evidence(account, evidence)
    db.add(account)
    db.flush()
    store_anchor_balances(db, account, anchor)  # asset venues: the anchor vector (P1)
    if secret:
        credentials.store(
            db,
            account,
            secret,
            kind=profile.credential,
            account_ref=payload.account_ref,
            expires_at=oauth_expires_at(secret) if oauth_grant else None,  # wave2
            actor_user_id=user.id,
        )
    audit.record(
        db,
        AuditAction.SCOPE_CHECK,
        actor_user_id=user.id,
        target_type="venue_account",
        target_id=account.id,
        details={"trigger": "connect", "venue": account.venue, **evidence_json(evidence)},
    )
    strategy = Strategy(
        user_id=user.id, venue_account_id=account.id, name=payload.strategy_name, slug="pending"
    )
    db.add(strategy)
    db.flush()
    strategy.slug = f"{slugify(payload.strategy_name)}-{strategy.id}"
    db.commit()
    if secret:  # p3-auth: tell the owner whenever a key is added (never the key itself)
        send_security_alert(user, "key_connected", {"Venue": profile.display_name})

    result = import_now(db, account)
    return {
        **_to_out(account, strategy),
        "imported_fills": result.new_fills,
        "imported_settlements": result.new_settlements,
    }


@router.post(
    "",
    response_model=ConnectOut,
    status_code=status.HTTP_201_CREATED,
    # p3-auth: TOTP on + email verified + step-up within 10 min, then 10/hour.
    dependencies=[Depends(require_connect_ready), Depends(rate_limit("venues_user", by="user"))],
)
def connect(payload: ConnectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return connect_account(db, user, payload)


@router.get("", response_model=list[VenueAccountOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(current_user)):
    accounts = db.scalars(select(VenueAccount).where(VenueAccount.user_id == user.id)).all()
    out = []
    for account in accounts:
        strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
        out.append(_to_out(account, strategy))
    return out


def claim_account(db: Session, user: User, account_id: int) -> dict:
    """Check the venue profile for the claim code; idempotent once claimed."""
    account = _owned_account(db, account_id, user)
    if account.claimed:
        strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
        return _to_out(account, strategy)
    if account.status == VenueAccountStatus.DISCONNECTED or account.claim_code is None:
        raise HTTPException(status_code=409, detail="Account cannot be claimed")
    connector = build_connector(account.venue, account.venue_env, account.key_id, None)
    found = connector.verify_claim(account.claim_code)
    if found is None:
        raise HTTPException(status_code=502, detail="Could not read the venue profile; try again.")
    if not found:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Code {account.claim_code} was not found in the profile bio. "
                "Save the bio on the venue first, then try again."
            ),
        )
    account.claimed_at = utcnow()
    db.commit()
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    return _to_out(account, strategy)


@router.post(
    "/{account_id}/claim",
    response_model=VenueAccountOut,
    dependencies=[Depends(rate_limit("venues_user", by="user"))],  # p3-auth
)
def claim(account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return claim_account(db, user, account_id)


@router.post("/{account_id}/sync", response_model=ConnectOut)
def manual_sync(account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    account = _owned_account(db, account_id, user)
    if account.status == VenueAccountStatus.DISCONNECTED:
        raise HTTPException(status_code=409, detail="Account is disconnected")
    if account.status == VenueAccountStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="Account is suspended; connect a read-only key")
    try:
        result = import_now(db, account)
    except Exception:
        logger.exception("manual sync failed for account %s", account.id)
        raise HTTPException(status_code=502, detail="Sync failed; see connection status.") from None
    strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
    return {
        **_to_out(account, strategy),
        "imported_fills": result.new_fills,
        "imported_settlements": result.new_settlements,
    }


class SnapshotOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    strategy_id: int
    as_of: datetime
    venue_balance_micros: int
    computed_cash_micros: int
    divergence_micros: int
    tolerance_micros: int
    reconciled: bool


@router.post("/{account_id}/reconcile", response_model=SnapshotOut)
def manual_reconcile(
    account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)
):
    """Run reconciliation now (the worker also runs it nightly)."""
    account = _owned_account(db, account_id, user)
    if account.status == VenueAccountStatus.DISCONNECTED:
        raise HTTPException(status_code=409, detail="Account is disconnected")
    if account.status == VenueAccountStatus.SUSPENDED:
        raise HTTPException(status_code=409, detail="Account is suspended; connect a read-only key")
    if not reconcilable(account):
        raise HTTPException(
            status_code=409, detail="This venue exposes no balance to reconcile against."
        )
    try:
        return reconcile_account(db, account)
    except Exception:
        logger.exception("manual reconcile failed for account %s", account.id)
        raise HTTPException(status_code=502, detail="Reconcile failed; see status.") from None


@router.delete(
    "/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_step_up)],  # p3-auth
)
def disconnect(account_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Disconnect erases the encrypted key immediately (invariant #4). The
    already-ingested ledger stays: it is append-only history the track record
    was built on, and the methodology page says exactly that."""
    account = _owned_account(db, account_id, user)
    credentials.disconnect(db, account, actor_user_id=user.id)


def import_now(db: Session, account: VenueAccount) -> SyncResult:
    """Binary venues import inline (a few requests). Asset venues can need
    hundreds of paged, rate-limited requests (Binance.US per-symbol windows),
    so the worker does it: a first connect queues a full backfill, a manual
    sync an urgent incremental sync. Returns zero counts for queued work."""
    if not is_asset_venue(account.venue):
        return sync_account(db, account)
    done = db.scalar(
        select(AccountSchedule.backfill_done_at).where(
            AccountSchedule.venue_account_id == account.id
        )
    )
    kind = JobKind.SYNC if done is not None else JobKind.BACKFILL
    enqueue(db, kind, account_id=account.id, priority=PRIORITY_URGENT)
    db.commit()
    return SyncResult(new_fills=0, new_settlements=0)


def _owned_account(db: Session, account_id: int, user: User) -> VenueAccount:
    account = db.get(VenueAccount, account_id)
    if account is None or account.user_id != user.id:
        # 404 for both cases: do not reveal other users' account ids exist.
        raise HTTPException(status_code=404, detail="Venue account not found")
    return account
