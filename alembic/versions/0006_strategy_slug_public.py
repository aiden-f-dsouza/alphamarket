"""strategy slug and public flag (M4)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("strategies", sa.Column("slug", sa.String(length=140), nullable=True))
    op.add_column(
        "strategies",
        sa.Column("public", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE strategies SET slug = 'strategy-' || id WHERE slug IS NULL")
    op.alter_column("strategies", "slug", nullable=False)
    op.create_index("ix_strategies_slug", "strategies", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_strategies_slug", table_name="strategies")
    op.drop_column("strategies", "public")
    op.drop_column("strategies", "slug")
