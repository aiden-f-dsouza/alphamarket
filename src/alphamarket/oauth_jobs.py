"""OAuth token refresh in the worker (wave2, w2-account-security).

tick(session_factory) -> int
    Every 15 min (worker.main): enqueue OAUTH_REFRESH for each ACTIVE OAUTH2
    account whose live venue_credentials.expires_at <= now + 2 x
    oauth.REFRESH_MARGIN. The column is written at connect and at every
    refresh, so nothing is decrypted to find what is due. The default dedupe
    key (oauth_refresh:<account>) keeps one job per account in flight.

run(ctx) -> None
    The OAUTH_REFRESH handler. Re-reads the credential first: a token that a
    sync already refreshed (expires_at past the tick horizon again) is left
    alone, so each due token is refreshed exactly once. Otherwise: decrypt
    (audited), refresh at the venue's token endpoint, credentials.store the
    new record with its expires_at, audit CREDENTIAL_REFRESH. A refusal
    (invalid_grant and other OAuth errors) sets the account to ERROR with a
    "Reconnect ..." message and alerts the owner once; a 5xx/429 or a network
    error is re-raised so the job backs off and retries.

refresh_if_due(db, account, opened) -> str | None
    The lazy path, called by ingest.connector_for before building an OAUTH2
    connector: oauth.ensure_fresh (5 min margin plus jitter), and when it
    refreshed, store and commit the new record and return its secret; None
    when nothing was due. A refusal marks the account as above and raises
    ReconnectRequired, so the sync fails with the reconnect message instead
    of a 401 from the venue.

The refresh token lives inside the vault-encrypted token JSON (oauth.token_record);
nothing here logs it, and the audit rows carry only the error code.
"""

import json
import logging
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from alphamarket import alerts, audit, credentials, email, oauth
from alphamarket.audit import AuditAction
from alphamarket.connectors import registry
from alphamarket.connectors.base import CredentialKind
from alphamarket.connectors.registry import PROFILES, get_profile
from alphamarket.jobs import JobKind, enqueue
from alphamarket.models import VenueAccount, VenueAccountStatus, VenueCredential

logger = logging.getLogger(__name__)

# tick() looks this far ahead. The tick runs every 15 min, so a token found
# here is refreshed before it expires even when the next tick is late.
TICK_HORIZON = 2 * oauth.REFRESH_MARGIN
# In every reconnect message; a failed sync's "<Exc>: <message>" keeps it.
RECONNECT_MARK = "no longer accepts Koine's access"

email.ALERT_SUBJECTS.setdefault("oauth_reconnect", "Reconnect a venue on your Koine account")
email.ALERT_LEADS.setdefault(
    "oauth_reconnect",
    "A venue stopped accepting Koine's read-only access, so syncing has paused. "
    "Reconnect it from your dashboard to resume.",
)


class ReconnectRequired(RuntimeError):
    """The venue refused the refresh for good: only a new grant helps."""


def _aware(dt: datetime | None) -> datetime | None:
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def _oauth_venues() -> list[str]:
    return [v for v, p in PROFILES.items() if p.credential is CredentialKind.OAUTH2]


def _expires_at(secret: str) -> datetime | None:
    from alphamarket.venues import oauth_expires_at  # venues imports ingest -> this module

    return oauth_expires_at(secret)


def reconnect_message(account: VenueAccount) -> str:
    name = get_profile(account.venue).display_name
    return (
        f"Reconnect {name}: it {RECONNECT_MARK} (the grant was revoked or expired). "
        "Disconnect and connect it again; your track record is kept."
    )


def _error_code(exc: Exception) -> str:
    """The OAuth error code from an OAuthError message ("... refused: <code>")."""
    text = str(exc)
    return text.rsplit(": ", 1)[-1][:60] if ": " in text else type(exc).__name__


def is_transient(exc: Exception) -> bool:
    """Network trouble, or the token endpoint answering 5xx/429: retry later.
    Anything else from the endpoint (invalid_grant, invalid_client, a write
    scope on refresh) is a refusal."""
    if isinstance(exc, httpx.HTTPError):
        return True
    code = _error_code(exc)
    return code.isdigit() and (int(code) >= 500 or int(code) == 429)


# ---- tick ---------------------------------------------------------------------------


def due_account_ids(db: Session, now: datetime) -> list[int]:
    venues = _oauth_venues()
    if not venues:
        return []
    return list(
        db.scalars(
            select(VenueAccount.id)
            .join(VenueCredential, VenueCredential.venue_account_id == VenueAccount.id)
            .where(
                VenueAccount.venue.in_(venues),
                VenueAccount.status == VenueAccountStatus.ACTIVE,
                VenueCredential.revoked_at.is_(None),
                VenueCredential.expires_at.is_not(None),
                VenueCredential.expires_at <= now + TICK_HORIZON,
            )
            .order_by(VenueAccount.id)
            .distinct()
        ).all()
    )


def tick(session_factory: sessionmaker, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    queued = 0
    with session_factory() as db:
        for account_id in due_account_ids(db, now):
            if enqueue(db, JobKind.OAUTH_REFRESH, account_id=account_id, now=now) is not None:
                queued += 1
        db.commit()
    return queued


# ---- refresh ------------------------------------------------------------------------


def _store(
    db: Session, account: VenueAccount, secret: str, account_ref: str | None, trigger: str
) -> None:
    expires_at = _expires_at(secret)
    cred = credentials.store(
        db,
        account,
        secret,
        kind=CredentialKind.OAUTH2,
        account_ref=account_ref,
        expires_at=expires_at,
    )
    audit.record(
        db,
        AuditAction.CREDENTIAL_REFRESH,
        target_type="venue_account",
        target_id=account.id,
        details={
            "trigger": trigger,
            "venue": account.venue,
            "outcome": "refreshed",
            "credential_id": cred.id,
            "expires_at": expires_at,
        },
    )


def mark_reconnect(db: Session, account: VenueAccount, exc: Exception, trigger: str) -> None:
    """ERROR + "Reconnect ..." on the account, audited and committed; the
    owner is alerted only on the transition (a sync retrying the dead grant
    must not email every few minutes)."""
    already = account.status == VenueAccountStatus.ERROR and RECONNECT_MARK in (
        account.last_sync_error or ""
    )
    account.status = VenueAccountStatus.ERROR
    account.last_sync_error = reconnect_message(account)
    audit.record(
        db,
        AuditAction.CREDENTIAL_REFRESH,
        target_type="venue_account",
        target_id=account.id,
        details={
            "trigger": trigger,
            "venue": account.venue,
            "outcome": "refused",
            "error": _error_code(exc),
        },
    )
    db.commit()
    logger.warning("venue account %s: oauth refresh refused (%s)", account.id, _error_code(exc))
    if already:
        return
    name = get_profile(account.venue).display_name
    alerts.emit(
        alerts.SecurityAlert(
            kind="oauth_reconnect",
            user_id=account.user_id,
            subject=f"Reconnect {name} on Koine",
            body=reconnect_message(account),
            details={"venue_account_id": account.id, "venue": name},
        )
    )


def run(ctx) -> None:
    db, account = ctx.db, ctx.account
    if account is None:
        return
    if get_profile(account.venue).credential is not CredentialKind.OAUTH2:
        return
    if account.status != VenueAccountStatus.ACTIVE:
        return
    now = ctx.clock()
    cred = credentials.active_credential(db, account)
    expires_at = _aware(cred.expires_at) if cred is not None else None
    if expires_at is None or expires_at > now + TICK_HORIZON:
        return  # refreshed meanwhile (a sync's lazy path), or never expires
    provider = registry.get_oauth_provider(account.venue)
    if provider is None:
        return
    opened = credentials.open_credential(db, account, purpose="oauth_refresh")
    try:
        record = json.loads(opened.secret)
    except ValueError:
        return
    if not isinstance(record, dict) or not record.get("refresh_token"):
        return
    try:
        fresh = oauth.refresh_token(provider, record, now=now)
    except (oauth.OAuthError, httpx.HTTPError) as exc:
        if is_transient(exc):
            raise
        mark_reconnect(db, account, exc, trigger="job")
        return
    secret = json.dumps(fresh)
    _store(db, account, secret, opened.account_ref, trigger="job")
    db.commit()
    logger.info("venue account %s: oauth token refreshed", account.id)


def refresh_if_due(
    db: Session, account: VenueAccount, opened, now: datetime | None = None
) -> str | None:
    """`now` is a test seam (an optional keyword; the contract call passes three)."""
    try:
        secret = oauth.ensure_fresh(account.venue, opened.secret, now=now)
    except (oauth.OAuthError, httpx.HTTPError) as exc:
        if is_transient(exc):
            raise
        mark_reconnect(db, account, exc, trigger="sync")
        raise ReconnectRequired(reconnect_message(account)) from None
    if secret is None:
        return None
    _store(db, account, secret, opened.account_ref, trigger="sync")
    db.commit()
    return secret
