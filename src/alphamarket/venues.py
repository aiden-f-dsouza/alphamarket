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

wave2 (w2-p8-connect):
- Consent (D18, invariant 10): every connect sends `consent_sha256`, the
  hash of the venue's current consent text (consent.py); a missing or stale
  hash is a 422 before any venue call, and the acceptance is recorded with
  the account. No path skips it, the JSON API included.
- Signed claims (D21): SIGNED_MESSAGE venues (Hyperliquid) claim with a
  wallet signature over connector.claim_text(stored code); the verify_claim
  payload is built from the STORED code, so a signature over any other code
  fails.
- Aggregator connects (D20): begin_aggregator_connect registers one
  SnapTrade user per connection and creates a PENDING account;
  connect_account(pending=...) finishes it with the chosen account_ref.
  Disconnect deletes the aggregator user (credentials.disconnect).
"""

import json
import logging
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import audit, consent, credentials
from alphamarket.audit import AuditAction
from alphamarket.auth import current_user
from alphamarket.config import get_settings
from alphamarket.connectors.base import CredentialKind, OwnershipProof
from alphamarket.connectors.registry import (
    SUPPORTED_VENUES,
    aggregator_register_user,
    build_connector,
    get_profile,
)
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
    # wave2 (D18, invariant 10): sha256 of the consent text the user accepted
    # (consent.consent_text). Required: a missing or stale hash is a 422.
    consent_sha256: str | None = Field(default=None, max_length=64)
    # CredentialKind.KEY_SECRET_PASSPHRASE: the server assembles the secret
    # JSON {"secret", "passphrase"}; the passphrase is never stored alone.
    passphrase: str | None = Field(default=None, max_length=200)

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


# A claim on these is refused (PENDING has no address to check yet).
INACTIVE_FOR_CLAIM = (VenueAccountStatus.DISCONNECTED, VenueAccountStatus.PENDING)


def ensure_venue_open(venue: str) -> None:
    reason = venue_closed_reason(venue)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=reason)


def assemble_secret(profile, payload: ConnectIn) -> str | None:
    """The credential string the vault stores, from the form/API fields.
    KEY_SECRET_PASSPHRASE: {"secret", "passphrase"} JSON built here (an API
    caller may also send that JSON as `secret` itself). Raises 422 with
    user-safe detail; never echoes the values."""
    if not profile.needs_secret:
        return None
    if not payload.secret:
        raise HTTPException(status_code=422, detail=f"{profile.secret_label} is required.")
    if profile.credential is not CredentialKind.KEY_SECRET_PASSPHRASE:
        return payload.secret
    if payload.passphrase:
        return json.dumps({"secret": payload.secret, "passphrase": payload.passphrase})
    try:
        record = json.loads(payload.secret)
    except ValueError:
        record = None
    if isinstance(record, dict) and record.get("secret") and record.get("passphrase"):
        return payload.secret
    raise HTTPException(status_code=422, detail="Passphrase is required.")


def connect_account(
    db: Session,
    user: User,
    payload: ConnectIn,
    *,
    oauth_grant: bool = False,
    request: Request | None = None,
    pending: VenueAccount | None = None,
) -> dict:
    """Validate, check consent, scope-check, encrypt, anchor, create the
    strategy, first import. Raises HTTPException with user-safe detail on any
    failure. `oauth_grant` is set only by oauth.callback: OAuth venues never
    accept a pasted token.

    wave2: the consent hash in `payload` must match the venue's current
    consent text (consent.require_consent), and the acceptance is recorded in
    the transaction that creates the account. `pending` finishes a PENDING
    account (SnapTrade portal round trip): its stored credential is reused,
    the consent recorded when it was started stands, and the same account row
    becomes ACTIVE with `payload.account_ref`."""
    if payload.venue not in SUPPORTED_VENUES:
        raise HTTPException(status_code=422, detail=f"Unsupported venue: {payload.venue}")
    profile = get_profile(payload.venue)
    ensure_venue_open(payload.venue)
    screen: consent.ConsentScreen | None = None
    if pending is not None:
        if (
            pending.user_id != user.id
            or pending.venue != payload.venue
            or pending.status != VenueAccountStatus.PENDING
        ):
            raise HTTPException(status_code=409, detail="This connection is not pending.")
        if consent.consent_for(db, pending) is None:  # recorded by begin_aggregator_connect
            raise HTTPException(status_code=422, detail=consent.STALE_DETAIL)
        try:
            secret: str | None = credentials.open_credential(
                db, pending, purpose="connect_finish"
            ).secret
        except credentials.CredentialUnavailable:
            raise HTTPException(
                status_code=409, detail="This connection expired; start again."
            ) from None
        key_id = pending.key_id
    else:
        if profile.credential is CredentialKind.OAUTH2 and not oauth_grant:
            raise HTTPException(
                status_code=422,
                detail=f"Connect {profile.display_name} with its Connect button (OAuth).",
            )
        secret = assemble_secret(profile, payload)
        key_id = payload.key_id
        # Before any venue call: a connect without the current consent never
        # reaches the venue (invariant 10).
        screen = consent.require_consent(db, user, payload.venue, payload.consent_sha256, request)

    venue_env = get_settings().venue_env(payload.venue)
    try:
        connector = build_connector(
            payload.venue, venue_env, key_id, secret, account_ref=payload.account_ref
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
    if pending is not None:
        account = pending
        account.status = VenueAccountStatus.ACTIVE
        account.anchor_balance_micros = anchor.balance_micros
        account.anchor_at = anchor.at
        account.anchor_positions = anchor.positions
        account.claim_code = None if key_proves_ownership else new_claim_code()
        account.claimed_at = utcnow() if key_proves_ownership else None
    else:
        account = VenueAccount(
            user_id=user.id,
            venue=payload.venue,
            venue_env=venue_env,
            key_id=key_id.lower() if not profile.needs_secret else key_id,
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
            # A connector may learn the ref itself (Coinbase: the key's portfolio).
            account_ref=payload.account_ref or connector.account_ref,
            expires_at=oauth_expires_at(secret) if oauth_grant else None,  # wave2
            actor_user_id=user.id,
        )
    if screen is not None:
        consent.record_consent(db, user, screen, account, request)
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


def begin_aggregator_connect(
    db: Session,
    user: User,
    venue: str,
    consent_sha256: str | None,
    request: Request | None = None,
) -> VenueAccount:
    """Start an aggregator connect (D20): register one aggregator user for
    this connection, keep its user secret in venue_credentials, create a
    PENDING account and record the consent, all before the user leaves for
    the aggregator's portal. Finished by connect_account(pending=...), or
    abandoned by pending.abandon_stale."""
    if venue not in SUPPORTED_VENUES:
        raise HTTPException(status_code=422, detail=f"Unsupported venue: {venue}")
    profile = get_profile(venue)
    ensure_venue_open(venue)
    if profile.credential is not CredentialKind.AGGREGATOR:
        raise HTTPException(
            status_code=422, detail=f"{profile.display_name} does not connect through a portal."
        )
    screen = consent.require_consent(db, user, venue, consent_sha256, request)
    try:
        user_id, user_secret = aggregator_register_user(venue)
    except Exception:
        logger.warning("%s user registration failed", venue)
        raise HTTPException(
            status_code=502, detail=f"Could not reach {profile.display_name}; try again."
        ) from None
    account = VenueAccount(
        user_id=user.id,
        venue=venue,
        venue_env=get_settings().venue_env(venue),
        key_id=user_id,
        encrypted_private_key=None,
        status=VenueAccountStatus.PENDING,
    )
    db.add(account)
    db.flush()
    credentials.store(db, account, user_secret, kind=profile.credential, actor_user_id=user.id)
    consent.record_consent(db, user, screen, account, request)
    db.commit()
    return account


@router.post(
    "",
    response_model=ConnectOut,
    status_code=status.HTTP_201_CREATED,
    # p3-auth: TOTP on + email verified + step-up within 10 min, then 10/hour.
    dependencies=[Depends(require_connect_ready), Depends(rate_limit("venues_user", by="user"))],
)
def connect(
    payload: ConnectIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return connect_account(db, user, payload, request=request)


@router.get("", response_model=list[VenueAccountOut])
def list_accounts(db: Session = Depends(get_db), user: User = Depends(current_user)):
    accounts = db.scalars(select(VenueAccount).where(VenueAccount.user_id == user.id)).all()
    out = []
    for account in accounts:
        strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
        out.append(_to_out(account, strategy))
    return out


class ClaimIn(BaseModel):
    """POST /venues/{id}/claim body. `signature` is the wallet signature of
    claim_text(code) for OwnershipProof.SIGNED_MESSAGE venues (0x hex);
    profile-code venues send no body."""

    signature: str | None = Field(default=None, max_length=400)


def claim_account(
    db: Session, user: User, account_id: int, *, signature: str | None = None
) -> dict:
    """Prove ownership of an address-keyed account; idempotent once claimed.

    PROFILE_CODE: the stored claim code must be in the venue profile bio.
    SIGNED_MESSAGE (D21): `signature` must sign connector.claim_text(code)
    for the STORED code; the verify_claim payload is built here with
    connector.claim_payload(account.claim_code, signature), never from a code
    the user submits, so a signature over another code cannot be replayed."""
    account = _owned_account(db, account_id, user)
    if account.claimed:
        strategy = db.scalar(select(Strategy).where(Strategy.venue_account_id == account.id))
        return _to_out(account, strategy)
    if account.status in INACTIVE_FOR_CLAIM or account.claim_code is None:
        raise HTTPException(status_code=409, detail="Account cannot be claimed")
    profile = get_profile(account.venue)
    connector = build_connector(account.venue, account.venue_env, account.key_id, None)
    if profile.ownership is OwnershipProof.SIGNED_MESSAGE:
        sig = (signature or "").strip()
        if not sig:
            raise HTTPException(
                status_code=422,
                detail="Sign the Koine message with your wallet, then submit the signature.",
            )
        found = connector.verify_claim(connector.claim_payload(account.claim_code, sig))
        if found is None:
            raise HTTPException(
                status_code=502,
                detail=f"Could not reach {profile.display_name} to check the signature; try again.",
            )
        if not found:
            raise HTTPException(
                status_code=422,
                detail=(
                    "That signature does not match the Koine message for this address "
                    f"(code {account.claim_code}). Sign the exact message shown, with the "
                    "wallet that controls the address."
                ),
            )
    else:
        found = connector.verify_claim(account.claim_code)
        if found is None:
            raise HTTPException(
                status_code=502, detail="Could not read the venue profile; try again."
            )
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
def claim(
    account_id: int,
    payload: ClaimIn | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
):
    return claim_account(
        db, user, account_id, signature=payload.signature if payload is not None else None
    )


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
    if account.status == VenueAccountStatus.PENDING:  # wave2: nothing to read yet
        raise HTTPException(status_code=409, detail="Finish connecting this account first.")
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
