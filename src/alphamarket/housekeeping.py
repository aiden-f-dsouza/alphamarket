"""Nightly housekeeping (wave2, w2-account-security).

Contract: run(ctx) -> dict[str, int]
    ratelimit.prune_counters; delete sessions revoked or expired more than
    settings.session_retention_days ago; auth_tokens expired or used more
    than settings.token_retention_days ago; asset_quotes older than
    settings.quote_retention_days; pending.abandon_stale; and
    jobs.prune_finished (the scaffold moved it out of worker.tick_nightly).
    Returns counts per category. Never touches an append-only table.

Every table pruned here is coordination or convenience state, not evidence:
- rate_limit_counters: windows that have ended.
- sessions: a session "expired" when the first of its limits passed
  (sessions.is_live): idle timeout after last_seen_at, absolute limit after
  created_at, or 10 minutes for a password-only (mfa_pending) session. Dead
  sessions are kept `session_retention_days` so the sessions page and the
  new-sign-in alert can still see recent ones, then deleted, except each
  user's newest full session (prune_sessions says why).
- auth_tokens: only hashes are stored; a token used or expired more than
  `token_retention_days` ago can never be redeemed again.
- asset_quotes: signals copy the quote they used, so no evidence lives only
  in this table; marks older than `quote_retention_days` are long stale.
- jobs: finished rows (jobs.prune_finished: done 7 d, dead 30 d).
Each category is deleted and committed on its own, so a failure in one
leaves the others done.
"""

from datetime import timedelta

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from alphamarket import pending, ratelimit, sessions
from alphamarket.jobs import prune_finished
from alphamarket.models import AssetQuote, AuthToken, UserSession


def prune_sessions(db: Session, now, retention: timedelta) -> int:
    cutoff = now - retention
    # Each user's newest full session is kept whatever its age: the
    # new-sign-in alert fires only for a user who has signed in before
    # (sessions.has_prior_sessions), so pruning every row of a dormant
    # account would silence the alert for exactly the login it matters for.
    newest_full = (
        select(func.max(UserSession.id))
        .where(UserSession.mfa_pending.is_(False))
        .group_by(UserSession.user_id)
    )
    result = db.execute(
        delete(UserSession).where(
            or_(
                UserSession.revoked_at < cutoff,
                UserSession.mfa_pending.is_(True)
                & (UserSession.created_at < cutoff - sessions.PENDING_TTL),
                UserSession.last_seen_at < cutoff - sessions.idle_timeout(),
                UserSession.created_at < cutoff - sessions.absolute_timeout(),
            ),
            UserSession.id.not_in(newest_full),
        )
    )
    db.commit()
    return result.rowcount or 0


def prune_tokens(db: Session, now, retention: timedelta) -> int:
    cutoff = now - retention
    result = db.execute(
        delete(AuthToken).where(or_(AuthToken.used_at < cutoff, AuthToken.expires_at < cutoff))
    )
    db.commit()
    return result.rowcount or 0


def prune_quotes(db: Session, now, retention: timedelta) -> int:
    result = db.execute(delete(AssetQuote).where(AssetQuote.as_of < now - retention))
    db.commit()
    return result.rowcount or 0


def run(ctx) -> dict[str, int]:
    db, now, settings = ctx.db, ctx.clock(), ctx.settings
    counts: dict[str, int] = {}
    counts["rate_limit_counters"] = ratelimit.prune_counters(db, now)
    counts["sessions"] = prune_sessions(db, now, timedelta(days=settings.session_retention_days))
    counts["auth_tokens"] = prune_tokens(db, now, timedelta(days=settings.token_retention_days))
    counts["asset_quotes"] = prune_quotes(db, now, timedelta(days=settings.quote_retention_days))
    counts["pending_accounts"] = pending.abandon_stale(db, now)
    db.commit()
    counts["jobs"] = prune_finished(db, now=now)
    return counts
