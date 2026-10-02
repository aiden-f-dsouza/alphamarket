"""Read-only proof: at connect time and re-checked nightly (platform_plan P3 item 8).

`prove(connector)` runs the venue's prove_read_only() and never raises: a
probe that errors is "undetermined" (read_only None) with the error class in
the details. `recheck_account` stores the evidence on the account, audits
the result, and when a key has become writable (read_only False) suspends
the account: the credential is erased on the spot, the account stops
syncing, and the user is alerted. Holding a key that can trade breaks
invariant 4, so there is no grace period; the user reconnects with a new
read-only key.
"""

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import alerts, audit, credentials
from alphamarket.audit import AuditAction
from alphamarket.connectors.base import ReadOnlyEvidence, ReadOnlyProof, VenueConnector
from alphamarket.ingest import connector_for
from alphamarket.models import VenueAccount, VenueAccountStatus

logger = logging.getLogger(__name__)

RECHECK_STATUSES = (VenueAccountStatus.ACTIVE, VenueAccountStatus.ERROR)


def prove(connector: VenueConnector) -> ReadOnlyEvidence:
    try:
        return connector.prove_read_only()
    except Exception as exc:  # network, parse: undetermined, never a pass
        return ReadOnlyEvidence(
            method=ReadOnlyProof.NONE,
            read_only=None,
            checked_at=datetime.now(UTC),
            details={"error": type(exc).__name__},
        )


def evidence_json(evidence: ReadOnlyEvidence) -> dict:
    return audit.scrub(evidence.model_dump(mode="json"))


def apply_evidence(account: VenueAccount, evidence: ReadOnlyEvidence) -> None:
    """Store the evidence; scope_checked_at only moves on a confirmed pass."""
    account.scope_evidence = evidence_json(evidence)
    if evidence.read_only is True:
        account.scope_checked_at = evidence.checked_at


def recheck_account(db: Session, account: VenueAccount) -> ReadOnlyEvidence:
    connector = connector_for(account, db, purpose="scope_recheck")
    evidence = prove(connector)
    apply_evidence(account, evidence)
    audit.record(
        db,
        AuditAction.SCOPE_CHECK,
        target_type="venue_account",
        target_id=account.id,
        details={"trigger": "nightly", "venue": account.venue, **evidence_json(evidence)},
    )
    if evidence.read_only is False:
        suspend(db, account, evidence)
    db.commit()
    return evidence


def suspend(db: Session, account: VenueAccount, evidence: ReadOnlyEvidence) -> None:
    # wave2: a writable aggregator connection (SnapTrade) is released at the
    # aggregator too, so the trade-capable brokerage link does not outlive
    # the erased credential (best effort, like disconnect).
    credentials.delete_aggregator_user(account)
    credentials.revoke(db, account, reason="scope_writable")
    account.status = VenueAccountStatus.SUSPENDED
    account.last_sync_error = (
        "Suspended: this key can now trade or withdraw. Koine erased it. "
        "Create a read-only key and connect again."
    )
    audit.record(
        db,
        AuditAction.ACCOUNT_SUSPEND,
        target_type="venue_account",
        target_id=account.id,
        details={"reason": "scope_writable", "venue": account.venue, "method": evidence.method},
    )
    db.commit()
    logger.warning("venue account %s suspended: key is no longer read-only", account.id)
    alerts.emit(
        alerts.SecurityAlert(
            kind="key_suspended",
            user_id=account.user_id,
            subject="Koine suspended a venue connection",
            body=(
                f"Your {account.venue} key gained trading or withdrawal permission, so "
                "Koine erased it and paused the connection. Your track record is "
                "unchanged. Connect a new read-only key to resume."
            ),
            details={"venue_account_id": account.id, "venue": account.venue},
        )
    )


def recheck_all(db: Session) -> dict[str, int]:
    """Nightly sweep. Returns counts by outcome."""
    counts = {"read_only": 0, "undetermined": 0, "suspended": 0, "failed": 0}
    accounts = db.scalars(
        select(VenueAccount).where(VenueAccount.status.in_(RECHECK_STATUSES))
    ).all()
    for account in accounts:
        try:
            evidence = recheck_account(db, account)
        except Exception:
            db.rollback()
            logger.exception("scope re-check failed for venue account %s", account.id)
            counts["failed"] += 1
            continue
        if evidence.read_only is False:
            counts["suspended"] += 1
        elif evidence.read_only is True:
            counts["read_only"] += 1
        else:
            counts["undetermined"] += 1
    return counts
