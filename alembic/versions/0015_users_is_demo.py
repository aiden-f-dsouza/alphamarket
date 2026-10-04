"""w3-seed: users.is_demo (invariant 7)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-04

Rows owned by a demo user (scripts/seed_demo.py) are sample data: public
queries hide them unless DEMO_MODE is on, pages that show them carry the
"Sample data" banner, and scripts/preflight.py refuses to launch while any
demo user exists in the database.
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("users", "is_demo")
