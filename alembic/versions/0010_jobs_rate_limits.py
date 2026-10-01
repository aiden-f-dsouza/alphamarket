"""jobs queue, account schedules, outbound rate buckets, venue circuits (P2)

Revision ID: 0010
Revises: 0008
Create Date: 2026-09-26

None of these tables hold ledger facts: jobs are claimed, leased, retried and
finished in place, and buckets/circuits are counters. Invariant 3 does not
apply, so there are no append-only triggers here (see docs/worker.md).
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

ACTIVE = "status IN ('pending', 'running')"


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column(
            "venue_account_id", sa.Integer(), sa.ForeignKey("venue_accounts.id"), nullable=True
        ),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="pending"),
        _ts("run_after", nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("deferrals", sa.Integer(), nullable=False, server_default="0"),
        _ts("lease_until", nullable=True),
        sa.Column("locked_by", sa.String(80), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
        _ts("updated_at", nullable=False),
        _ts("done_at", nullable=True),
    )
    op.create_index("ix_jobs_venue_account_id", "jobs", ["venue_account_id"])
    op.create_index("ix_jobs_claim", "jobs", ["status", "priority", "run_after"])
    # One active job per dedupe key; enqueue() relies on it for ON CONFLICT.
    op.create_index(
        "uq_jobs_active_dedupe",
        "jobs",
        ["dedupe_key"],
        unique=True,
        postgresql_where=sa.text(ACTIVE),
        sqlite_where=sa.text(ACTIVE),
    )

    op.create_table(
        "account_schedules",
        sa.Column(
            "venue_account_id",
            sa.Integer(),
            sa.ForeignKey("venue_accounts.id"),
            primary_key=True,
        ),
        _ts("next_sync_at", nullable=True),
        sa.Column("resume_cursors", sa.JSON(), nullable=False),
        _ts("backfill_done_at", nullable=True),
        _ts("updated_at", nullable=False),
    )

    op.create_table(
        "rate_buckets",
        sa.Column("bucket_key", sa.String(200), primary_key=True),
        sa.Column("capacity_milli", sa.BigInteger(), nullable=False),
        sa.Column("refill_per_minute", sa.Integer(), nullable=False),
        sa.Column("level_milli", sa.BigInteger(), nullable=False),
        sa.Column("updated_ms", sa.BigInteger(), nullable=False),
        sa.Column("penalty_until_ms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("blocked_until_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )

    op.create_table(
        "venue_circuits",
        sa.Column("venue", sa.String(40), primary_key=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_until_ms", sa.BigInteger(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("venue_circuits")
    op.drop_table("rate_buckets")
    op.drop_table("account_schedules")
    op.drop_index("uq_jobs_active_dedupe", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_index("ix_jobs_venue_account_id", table_name="jobs")
    op.drop_table("jobs")
