"""Venue activity flags (wave2): activity a connector skipped instead of
guessing at (VenueConnector.drain_unsupported), persisted append-only so the
account and its public page disclose it. Reconciliation still catches any
real balance effect; a flag is a disclosure, not a quarantine."""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket.connectors.base import UnsupportedActivity
from alphamarket.jobs import aware
from alphamarket.models import VenueAccount, VenueActivityFlag

KIND_UNSUPPORTED = "unsupported_activity"


def persist(db: Session, account: VenueAccount, items: Sequence[UnsupportedActivity]) -> int:
    """Add a flag per new (venue_ref) item; already-flagged refs are skipped.
    Flushes, does not commit (the page commit carries it). Returns rows added."""
    if not items:
        return 0
    refs = {item.venue_ref for item in items}
    seen = set(
        db.scalars(
            select(VenueActivityFlag.venue_ref).where(
                VenueActivityFlag.venue_account_id == account.id,
                VenueActivityFlag.kind == KIND_UNSUPPORTED,
                VenueActivityFlag.venue_ref.in_(refs),
            )
        ).all()
    )
    added = 0
    for item in items:
        if item.venue_ref in seen:
            continue
        seen.add(item.venue_ref)
        db.add(
            VenueActivityFlag(
                venue_account_id=account.id,
                venue_ref=item.venue_ref,
                kind=KIND_UNSUPPORTED,
                activity_type=item.activity_type[:60],
                reason=item.reason[:40],
                symbol=item.symbol,
                occurred_at=aware(item.occurred_at),
                description=item.description,
            )
        )
        added += 1
    db.flush()
    return added


def flags_for(db: Session, account_id: int) -> list[VenueActivityFlag]:
    return list(
        db.scalars(
            select(VenueActivityFlag)
            .where(VenueActivityFlag.venue_account_id == account_id)
            .order_by(VenueActivityFlag.occurred_at, VenueActivityFlag.id)
        ).all()
    )


# ---- wave2:p8-pages: read helpers for the dashboard and the public page ----------------

REASON_LABELS: dict[str, str] = {
    "short_sale": "short sale",
    "option": "option trade",
    "crypto": "crypto activity",
    "stock_dividend": "stock dividend",
    "canceled": "canceled activity",
    "unsupported_type": "unsupported activity type",
}


def reason_label(reason: str) -> str:
    return REASON_LABELS.get(reason, reason.replace("_", " "))


def summarize(flags: Sequence[VenueActivityFlag]) -> dict:
    """{"count", "by_reason": [(label, n)] most common first, "latest": iso | None}.
    Reasons and counts only: what a flag is about is disclosed, the account's
    activity detail (symbols, descriptions) is not put on a public page."""
    counts: dict[str, int] = {}
    for flag in flags:
        counts[flag.reason] = counts.get(flag.reason, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    latest = max((aware(f.occurred_at) for f in flags), default=None)
    return {
        "count": len(flags),
        "by_reason": [(reason_label(reason), n) for reason, n in ordered],
        "latest": latest.isoformat() if latest else None,
    }


def summary_for(db: Session, account_id: int) -> dict:
    return summarize(flags_for(db, account_id))
