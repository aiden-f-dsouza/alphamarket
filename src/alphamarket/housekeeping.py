"""Nightly housekeeping (wave2, w2-account-security).

STUB. Contract: run(ctx) -> dict[str, int]
    ratelimit.prune_counters; delete sessions revoked or expired more than
    settings.session_retention_days ago; auth_tokens expired or used more
    than settings.token_retention_days ago; asset_quotes older than
    settings.quote_retention_days; pending.abandon_stale; then
    jobs.prune_finished (moved here from worker.tick_nightly). Returns counts
    per category. Never touches an append-only table.
"""


def run(ctx) -> dict[str, int]:
    return {}
