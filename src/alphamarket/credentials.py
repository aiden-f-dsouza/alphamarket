"""Venue credentials v2: storage, audited decrypt, revoke, rotation.

The only module that reads or writes `venue_credentials` ciphertext. Every
decrypt writes an audit row (`credential.decrypt`, with the request or job
id) and COMMITS it before the plaintext is returned, so a credential is never
used without a durable record that it was.

One active credential per account (revoked_at IS NULL). The M1-M7 column
`venue_accounts.encrypted_private_key` is read as a fallback for one release
(accounts connected before migration 0012 ran, and tests that build accounts
directly); nothing writes it any more, and revoke() erases it.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import audit, vault
from alphamarket.audit import AuditAction
from alphamarket.connectors.registry import get_profile
from alphamarket.models import VenueAccount, VenueAccountStatus, VenueCredential, utcnow

logger = logging.getLogger(__name__)


class CredentialUnavailable(RuntimeError):
    """No usable credential: never stored, revoked, or the account is suspended."""


def active_credential(db: Session, account: VenueAccount) -> VenueCredential | None:
    return db.scalar(
        select(VenueCredential)
        .where(
            VenueCredential.venue_account_id == account.id,
            VenueCredential.revoked_at.is_(None),
        )
        .order_by(VenueCredential.id.desc())
        .limit(1)
    )


def store(
    db: Session,
    account: VenueAccount,
    secret: str,
    *,
    kind: str,
    account_ref: str | None = None,
    expires_at: datetime | None = None,
    refresh_token: str | None = None,
    actor_user_id: int | None = None,
) -> VenueCredential:
    """Seal and store a credential for `account`, revoking any previous one.
    Audited as credential.connect. Not committed (the caller's transaction
    also creates the account)."""
    if account.id is None:
        db.flush()
    previous = active_credential(db, account)
    if previous is not None:
        _erase(previous)
    sealed = vault.seal(secret, refresh=refresh_token)
    cred = VenueCredential(
        venue_account_id=account.id,
        kind=str(kind),
        ciphertext=sealed.ciphertext,
        wrapped_dek=sealed.wrapped_dek,
        key_version=sealed.key_version,
        refresh_ciphertext=sealed.refresh_ciphertext,
        account_ref=account_ref,
        expires_at=expires_at,
    )
    db.add(cred)
    db.flush()
    audit.record(
        db,
        AuditAction.CREDENTIAL_CONNECT,
        actor_user_id=actor_user_id,
        target_type="venue_account",
        target_id=account.id,
        details={
            "venue": account.venue,
            "kind": str(kind),
            "credential_id": cred.id,
            "key_version": sealed.key_version,
            "replaced_credential_id": previous.id if previous is not None else None,
        },
    )
    return cred


@dataclass(frozen=True)
class OpenedCredential:
    secret: str
    refresh_token: str | None
    account_ref: str | None
    expires_at: datetime | None


def open_credential(db: Session, account: VenueAccount, *, purpose: str) -> OpenedCredential:
    """Decrypt the account's credential. Audited and committed first.

    Raises CredentialUnavailable when there is nothing usable, and refuses
    suspended/disconnected accounts outright."""
    if account.status in (VenueAccountStatus.SUSPENDED, VenueAccountStatus.DISCONNECTED):
        raise CredentialUnavailable(f"venue account {account.id} is {account.status}")
    cred = active_credential(db, account)
    if cred is not None and cred.ciphertext is not None:
        source = "venue_credentials"
        blob = (cred.ciphertext, cred.wrapped_dek, cred.key_version, cred.refresh_ciphertext)
    elif account.encrypted_private_key is not None:
        source = "legacy_column"
        cred = None
        blob = None
    else:
        raise CredentialUnavailable(f"venue account {account.id} has no stored credential")

    ctx = audit.current_context()
    audit.record(
        db,
        AuditAction.CREDENTIAL_DECRYPT,
        target_type="venue_account",
        target_id=account.id,
        request_or_job_id=ctx.request_or_job_id or audit.new_id("adhoc"),
        details={
            "purpose": purpose,
            "source": source,
            "credential_id": cred.id if cred is not None else None,
            "key_version": cred.key_version if cred is not None else vault.LEGACY_VERSION,
        },
    )
    db.commit()

    if blob is None:
        secret = vault.decrypt_secret(account.encrypted_private_key)
        return OpenedCredential(secret, None, None, None)
    secret, refresh = vault.open_record(*blob)
    return OpenedCredential(secret, refresh, cred.account_ref, cred.expires_at)


def account_ref_for(db: Session, account: VenueAccount) -> str | None:
    """account_ref without decrypting anything (address-keyed venues)."""
    cred = active_credential(db, account)
    return cred.account_ref if cred is not None else None


def _erase(cred: VenueCredential) -> None:
    cred.ciphertext = None
    cred.wrapped_dek = None
    cred.refresh_ciphertext = None
    cred.revoked_at = utcnow()


def revoke(
    db: Session,
    account: VenueAccount,
    *,
    actor_user_id: int | None = None,
    reason: str = "user_disconnect",
) -> int:
    """Erase every stored secret for `account` now (ciphertext, wrapped key,
    refresh token, and the legacy column). Audited as credential.disconnect.
    Not committed. Returns the number of credential rows revoked."""
    creds = db.scalars(
        select(VenueCredential).where(
            VenueCredential.venue_account_id == account.id,
            VenueCredential.revoked_at.is_(None),
        )
    ).all()
    for cred in creds:
        _erase(cred)
    account.encrypted_private_key = None  # written, never read: web role safe
    audit.record(
        db,
        AuditAction.CREDENTIAL_DISCONNECT,
        actor_user_id=actor_user_id,
        target_type="venue_account",
        target_id=account.id,
        details={
            "reason": reason,
            "revoked_credential_ids": [c.id for c in creds],
        },
    )
    return len(creds)


def disconnect(
    db: Session,
    account: VenueAccount,
    *,
    actor_user_id: int | None,
    reason: str = "user_disconnect",
) -> None:
    """User disconnect: erase the key, keep the ledger (append-only). An OAuth
    grant is revoked at the venue first, so it does not outlive the account;
    an aggregator user (SnapTrade, wave2) is deleted at the aggregator.
    `reason` is "pending_abandoned" when housekeeping drops an unfinished
    connect (pending.abandon_stale)."""
    _revoke_oauth_grant(db, account)
    _delete_aggregator_user(account)
    revoke(db, account, actor_user_id=actor_user_id, reason=reason)
    account.status = VenueAccountStatus.DISCONNECTED
    db.commit()


def _delete_aggregator_user(account: VenueAccount) -> None:
    """wave2 (D20): one aggregator user per connection, so disconnecting
    deletes it (the aggregator drops the brokerage link and stops billing).
    Needs only the user id (key_id), never the secret. Best effort: the
    local credential is erased whether or not the aggregator answered."""
    from alphamarket.connectors.base import CredentialKind
    from alphamarket.connectors.registry import aggregator_delete_user

    if get_profile(account.venue).credential is not CredentialKind.AGGREGATOR:
        return
    if account.status == VenueAccountStatus.DISCONNECTED:
        return  # already released
    try:
        aggregator_delete_user(account.venue, account.key_id)
    except Exception as exc:  # noqa: BLE001 - erasing the credential must still happen
        logger.warning(
            "aggregator user delete failed for account %s: %s", account.id, type(exc).__name__
        )


def _revoke_oauth_grant(db: Session, account: VenueAccount) -> None:
    from alphamarket.connectors.base import CredentialKind
    from alphamarket.connectors.registry import get_profile

    if get_profile(account.venue).credential is not CredentialKind.OAUTH2:
        return
    from alphamarket.oauth import revoke_quietly  # oauth imports venues -> credentials

    try:
        opened = open_credential(db, account, purpose="oauth_revoke")
        revoke_quietly(account.venue, opened.secret)
    except Exception:  # noqa: BLE001 - erasing the credential must still happen
        logger.warning("oauth revoke failed for account %s", account.id)


def _adopt_legacy(db: Session, batch_size: int) -> int:
    """Accounts whose key only exists in the legacy column (connected by old
    code after migration 0012's copy ran) get a credential row."""
    has_cred = select(VenueCredential.venue_account_id).where(VenueCredential.revoked_at.is_(None))
    adopted = 0
    while True:
        accounts = db.scalars(
            select(VenueAccount)
            .where(
                VenueAccount.encrypted_private_key.is_not(None),
                VenueAccount.status.not_in(
                    [VenueAccountStatus.DISCONNECTED, VenueAccountStatus.SUSPENDED]
                ),
                VenueAccount.id.not_in(has_cred),
            )
            .order_by(VenueAccount.id)
            .limit(batch_size)
        ).all()
        if not accounts:
            return adopted
        for account in accounts:
            db.add(
                VenueCredential(
                    venue_account_id=account.id,
                    kind=str(get_profile(account.venue).credential),
                    ciphertext=account.encrypted_private_key,
                    wrapped_dek=None,
                    key_version=vault.LEGACY_VERSION,
                )
            )
            adopted += 1
        db.commit()


def rotate_all(db: Session, batch_size: int = 100) -> int:
    """Move every live credential to the current key version, in committed
    batches. Wrapped-DEK rows are re-wrapped (the credential is never
    decrypted); legacy rows (no DEK) are decrypted and re-sealed, which is
    audited as a decrypt. Safe to re-run and to run while the app serves:
    each row is readable under its own key_version before and after.
    Returns the number of rows rotated."""
    _adopt_legacy(db, batch_size)
    target = vault.current_version()
    job_id = audit.current_context().request_or_job_id or audit.new_id("key_rotate")
    rotated = 0
    last_id = 0
    while True:
        creds = db.scalars(
            select(VenueCredential)
            .where(
                VenueCredential.id > last_id,
                VenueCredential.revoked_at.is_(None),
                VenueCredential.ciphertext.is_not(None),
                VenueCredential.key_version != target,
            )
            .order_by(VenueCredential.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        if not creds:
            return rotated
        for cred in creds:
            last_id = cred.id
            old_version = cred.key_version
            if cred.wrapped_dek is None:  # legacy: must re-seal the plaintext
                audit.record(
                    db,
                    AuditAction.CREDENTIAL_DECRYPT,
                    target_type="venue_account",
                    target_id=cred.venue_account_id,
                    request_or_job_id=job_id,
                    details={
                        "purpose": "rotate",
                        "source": "venue_credentials",
                        "credential_id": cred.id,
                        "key_version": old_version,
                    },
                )
                secret, refresh = vault.open_record(
                    cred.ciphertext, None, old_version, cred.refresh_ciphertext
                )
                sealed = vault.seal(secret, refresh=refresh)
                cred.ciphertext = sealed.ciphertext
                cred.refresh_ciphertext = sealed.refresh_ciphertext
                cred.wrapped_dek = sealed.wrapped_dek
                cred.key_version = sealed.key_version
            else:
                cred.wrapped_dek, cred.key_version = vault.rewrap(cred.wrapped_dek, old_version)
            cred.rotated_at = utcnow()
            audit.record(
                db,
                AuditAction.CREDENTIAL_ROTATE,
                target_type="venue_credential",
                target_id=cred.id,
                request_or_job_id=job_id,
                details={"from_version": old_version, "to_version": cred.key_version},
            )
            rotated += 1
        db.commit()
        logger.info("rotated %d credentials so far (target %s)", rotated, target)
