"""What the dashboard says about each venue account (wave2, w2-p8-pages):
importing history (no backfill_done_at + an active BACKFILL job + pages so
far from its payload), last sync error, activity flags, and the next action
(finish connecting, replace key). Read-only: nothing here writes.

Links (route paths are fixed across workstreams):
    PENDING account                          -> /dashboard/connect/snaptrade/{id}
    SUSPENDED or ERROR account, key venues   -> /dashboard/accounts/{id}/rotate
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import activity_flags
from alphamarket.connectors.base import CredentialKind
from alphamarket.connectors.registry import get_profile
from alphamarket.jobs import JobKind
from alphamarket.models import (
    ACTIVE_JOB_STATUSES,
    AccountSchedule,
    Job,
    VenueAccount,
    VenueAccountStatus,
)

# Credentials a user can replace by pasting a new one (plan D17: key-type only;
# an address has nothing to replace, an OAuth grant is renewed by re-consenting).
ROTATABLE_CREDENTIALS = (
    CredentialKind.RSA_PEM,
    CredentialKind.KEY_SECRET,
    CredentialKind.KEY_SECRET_PASSPHRASE,
)


@dataclass(frozen=True)
class AccountStatus:
    importing: bool = False
    pages: int | None = None  # pages imported so far (None until the first page)
    last_sync_error: str | None = None
    flags: dict = field(default_factory=lambda: {"count": 0, "by_reason": [], "latest": None})
    finish_url: str | None = None  # "Finish connecting"
    rotate_url: str | None = None  # "Replace key"

    @property
    def n_flags(self) -> int:
        return self.flags["count"]


def importing_pages(db: Session, account: VenueAccount) -> tuple[bool, int | None]:
    """(importing, pages): importing history means the backfill has not
    completed and a BACKFILL job for the account is pending or running; pages
    is the page counter the page runner keeps in the job payload."""
    sched = db.get(AccountSchedule, account.id)
    if sched is not None and sched.backfill_done_at is not None:
        return False, None
    job = db.scalar(
        select(Job)
        .where(
            Job.venue_account_id == account.id,
            Job.kind == JobKind.BACKFILL,
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
        .order_by(Job.id.desc())
        .limit(1)
    )
    if job is None:
        return False, None
    pages = (job.payload or {}).get("pages")
    return True, int(pages) if isinstance(pages, int | float) else None


def account_status(db: Session, account: VenueAccount) -> AccountStatus:
    profile = get_profile(account.venue)
    importing, pages = (False, None)
    if account.status not in (
        VenueAccountStatus.DISCONNECTED,
        VenueAccountStatus.SUSPENDED,
        VenueAccountStatus.PENDING,
    ):
        importing, pages = importing_pages(db, account)
    finish_url = None
    if account.status == VenueAccountStatus.PENDING and account.venue == "snaptrade":
        finish_url = f"/dashboard/connect/snaptrade/{account.id}"
    rotate_url = None
    if (
        account.status in (VenueAccountStatus.SUSPENDED, VenueAccountStatus.ERROR)
        and profile.credential in ROTATABLE_CREDENTIALS
    ):
        rotate_url = f"/dashboard/accounts/{account.id}/rotate"
    return AccountStatus(
        importing=importing,
        pages=pages,
        last_sync_error=account.last_sync_error,
        flags=activity_flags.summary_for(db, account.id),
        finish_url=finish_url,
        rotate_url=rotate_url,
    )
