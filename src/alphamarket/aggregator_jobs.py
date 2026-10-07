"""Deleting an aggregator user (SnapTrade, D20) until it is done.

One aggregator user per Koine connection (D20); disconnecting, suspending a
writable link or abandoning a half-finished connect must delete that user, or
Koine keeps paying for it and the aggregator keeps the brokerage link. The
local credential is erased either way (invariant 4), so a delete that fails
must be retried here, not logged and forgotten.

release_user(db, account, *, limiter, cause) -> str
    Called by credentials.delete_aggregator_user, so every caller path (user
    disconnect, scopecheck.suspend, pending.abandon_stale) gets it. One
    immediate attempt through the outbound limiter; web callers pass a limiter
    and fail fast (venue_limits.call_venue). Any failure, including "SnapTrade
    is busy", queues an AGGREGATOR_DELETE_USER job in the caller's transaction,
    so the retry is committed atomically with the disconnect itself and cannot
    be lost. Returns the outcome and audits it: "deleted", "already_gone" or
    "retry_queued".

run(ctx) -> None
    The job handler (registered by worker.py). The job keeps the account's id
    and jobs.RUNS_ON_INACTIVE_ACCOUNT lets it run although the account is
    DISCONNECTED or SUSPENDED (see the comment there for why the job is
    account-linked rather than account-less). The runner then does the rest:
    an open SnapTrade circuit or an exhausted bucket defers it, a 429 backs the
    buckets off, a 5xx or timeout counts an attempt, and after
    DELETE_MAX_ATTEMPTS it dead-letters (the hook below audits "gave_up" and
    logs at error).

What counts as done: the delete is accepted (2xx), or SnapTrade answers 404/410
for the user (already deleted: connectors.snaptrade.SnapTradeUserNotFound,
which registry.aggregator_delete_user turns into False). Everything else is a
failure: 429/418, 5xx and transport errors are transient (the runner backs
off, and an outage opens the circuit so jobs are deferred without spending
attempts); other 4xx (401/403: a wrong consumer key or signature) are
permanent until Koine's own configuration is fixed, so they run out their
attempts and dead-letter where an operator sees them.

Never in a payload or a log: the user SECRET. Only the user id is needed (it is
venue_accounts.key_id, an identifier Koine chose, not a secret), read from the
account row; the payload carries only the cause label.
"""

import logging
from datetime import timedelta

from sqlalchemy.orm import Session

from alphamarket import audit
from alphamarket.audit import AuditAction
from alphamarket.config import get_settings
from alphamarket.connectors import registry
from alphamarket.connectors.base import CredentialKind, Throttle
from alphamarket.jobs import JobContext, JobKind, enqueue, register_dead_letter_hook, utcnow
from alphamarket.models import Job, VenueAccount, VenueAccountStatus
from alphamarket.venue_limits import VenueLimiter, call_venue, credential_ref

logger = logging.getLogger(__name__)

# Backoff is base x 2^(n-1) from 30 s, capped at 1 h (settings), and an outage
# defers without counting, so 12 failed attempts is roughly five hours of
# SnapTrade being down or refusing before the job gives up. The default of 5
# (about 8 minutes) is too short for a call whose failure costs money every
# month and leaves a brokerage link live.
DELETE_MAX_ATTEMPTS = 12

# The only account states in which an aggregator user may be deleted: the
# connection has already been released locally. A live account's user is never
# touched, whatever a job says.
RELEASED_STATUSES = (VenueAccountStatus.DISCONNECTED, VenueAccountStatus.SUSPENDED)

DELETED = "deleted"
ALREADY_GONE = "already_gone"
RETRY_QUEUED = "retry_queued"
GAVE_UP = "gave_up"


def _audit(db: Session, account: VenueAccount, outcome: str, cause: str, **extra) -> None:
    # "outcome" is what the user's audit trail shows; "cause" stays in the row.
    audit.record(
        db,
        AuditAction.AGGREGATOR_USER_DELETE,
        target_type="venue_account",
        target_id=account.id,
        details={"venue": account.venue, "outcome": outcome, "cause": cause, **extra},
    )


def delete_call(venue: str, user_id: str, throttle: Throttle | None) -> bool | None:
    """One deleteUser request. True: deleted now. False: SnapTrade did not know
    the user (already gone). Raises on any other failure."""
    # A throttle keyword only when there is one: callers and tests that replace
    # the registry wrapper with a two-argument function keep working.
    kwargs = {"throttle": throttle} if throttle is not None else {}
    return registry.aggregator_delete_user(venue, user_id, **kwargs)


def enqueue_delete(db: Session, account: VenueAccount, *, cause: str) -> int | None:
    """Queue the retry (one per account: the default dedupe key is
    "aggregator_delete_user:<account id>", so calling this twice, or from two
    paths, still leaves exactly one pending or running job; None when one
    exists). Flushes, does not commit: the caller owns the transaction. The
    first run waits one backoff base so it does not repeat the failed attempt
    in the same instant."""
    now = utcnow()
    return enqueue(
        db,
        JobKind.AGGREGATOR_DELETE_USER,
        account_id=account.id,
        payload={"cause": cause},
        run_after=now + timedelta(seconds=get_settings().worker_backoff_base_seconds),
        max_attempts=DELETE_MAX_ATTEMPTS,
        now=now,
    )


def release_user(
    db: Session,
    account: VenueAccount,
    *,
    limiter: VenueLimiter | None = None,
    cause: str = "user_disconnect",
) -> str:
    """Delete the account's aggregator user now; if that fails for any reason
    queue the retry job. Never raises for a failed delete: the caller still has
    to erase the local credential. Adds the audit row and the job to `db`
    without committing."""
    venue, user_id = account.venue, account.key_id
    try:
        deleted = call_venue(
            limiter,
            venue,
            credential_ref(venue, user_id),
            lambda throttle: delete_call(venue, user_id, throttle),
        )
    except Exception as exc:  # noqa: BLE001 - erasing the credential must still happen
        enqueue_delete(db, account, cause=cause)
        logger.warning(
            "aggregator user delete failed for account %s (%s); retry queued",
            account.id,
            type(exc).__name__,
        )
        outcome = RETRY_QUEUED
    else:
        outcome = ALREADY_GONE if deleted is False else DELETED
    _audit(db, account, outcome, cause)
    return outcome


def run(ctx: JobContext) -> None:
    db, account = ctx.db, ctx.account
    if account is None:
        return
    if registry.get_profile(account.venue).credential is not CredentialKind.AGGREGATOR:
        return
    if account.status not in RELEASED_STATUSES:
        logger.warning(
            "account %s is %s, not released: its aggregator user is kept",
            account.id,
            account.status,
        )
        return
    throttle = (
        ctx.limiter.throttle(account.venue, ctx.credential_id) if ctx.limiter is not None else None
    )
    # BudgetExhausted, RateLimited, a 429 and every other failure propagate to
    # the runner, which defers or backs off (class docstring above).
    deleted = delete_call(account.venue, account.key_id, throttle)
    cause = str((ctx.job.payload or {}).get("cause") or "retry")
    _audit(db, account, ALREADY_GONE if deleted is False else DELETED, cause, via="retry_job")
    db.commit()
    logger.info("aggregator user for account %s deleted by the retry job", account.id)


def _on_dead_letter(db: Session, job: Job, attempts: int, error: str) -> None:
    account = db.get(VenueAccount, job.venue_account_id) if job.venue_account_id else None
    logger.error(
        "aggregator user for account %s could not be deleted after %d attempts (%s): "
        "Koine is still billed for it and the brokerage link is still live; delete it by hand",
        job.venue_account_id,
        attempts,
        error.split(":", 1)[0][:60],
    )
    if account is not None:
        _audit(
            db,
            account,
            GAVE_UP,
            str((job.payload or {}).get("cause") or "retry"),
            attempts=attempts,
            error=error.split(":", 1)[0][:60],
        )


register_dead_letter_hook(JobKind.AGGREGATOR_DELETE_USER, _on_dead_letter)
