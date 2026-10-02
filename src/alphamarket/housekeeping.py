"""Nightly housekeeping (wave2, w2-account-security).

Contract: run(ctx) -> dict[str, int]
    ratelimit.prune_counters; delete sessions revoked or expired more than
    settings.session_retention_days ago; auth_tokens expired or used more
    than settings.token_retention_days ago; asset_quotes older than
    settings.quote_retention_days; pending.abandon_stale; and
    jobs.prune_finished (already here: the scaffold moved it out of
    worker.tick_nightly). Returns counts per category. Never touches an
    append-only table. Add categories inside run(); do not edit worker.py.
"""

from alphamarket.jobs import prune_finished


def run(ctx) -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["jobs"] = prune_finished(ctx.db, now=ctx.clock())
    return counts
