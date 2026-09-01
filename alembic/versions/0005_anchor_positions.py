"""anchor_positions on venue_accounts (M3)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("venue_accounts", sa.Column("anchor_positions", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("venue_accounts", "anchor_positions")
