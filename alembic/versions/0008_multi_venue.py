"""multi-venue: fill notional, ownership claim (M7)

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Venue-reported gross fill value when price x count is not exact in
    # micros (Polymarket). NULL = price x count, exactly. Adding a nullable
    # column touches no existing ledger row (invariant #3).
    op.add_column("fills", sa.Column("notional_micros", sa.BigInteger(), nullable=True))
    # Ownership proof for credential-less venues. Every existing account was
    # connected with a read-only key, which proves ownership, so backfill
    # claimed_at from created_at.
    op.add_column("venue_accounts", sa.Column("claim_code", sa.String(40), nullable=True))
    op.add_column(
        "venue_accounts", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.execute("UPDATE venue_accounts SET claimed_at = created_at WHERE claimed_at IS NULL")


def downgrade() -> None:
    op.drop_column("venue_accounts", "claimed_at")
    op.drop_column("venue_accounts", "claim_code")
    op.drop_column("fills", "notional_micros")
