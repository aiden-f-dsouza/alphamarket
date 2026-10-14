"""leaderboard snapshots for /watch/discover (copy strategies step 7)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-14

A cache of each venue's public leaderboard per period, refreshed by the
watch_discover job and read by the private /watch/discover page. Mutable
(each refresh replaces `rows`); not a ledger table, not append-only. Rows
hold addresses and the venue's own figures in integer micros, never a
display name or any other identity field.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "leaderboard_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.UniqueConstraint("venue", "period", name="uq_leaderboard_snapshots_venue_period"),
    )


def downgrade() -> None:
    op.drop_table("leaderboard_snapshots")
