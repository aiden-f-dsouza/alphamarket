"""on-chain perps scaffold: asymmetric funding, non-copyable instruments

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-22

docs/onchain_perps_plan.md S7. Two nullable/defaulted columns, no data
rewrite:
- funding_rates.short_rate: what shorts pay per interval on venues whose two
  sides differ (GMX). NULL keeps the symmetric rule (shorts receive `rate`).
- instruments.copyable: FALSE makes paper copying skip the instrument
  ("not_copyable") while its signals are still recorded. Every existing row
  is copyable (server default TRUE).
Neither table is append-only, so no trigger changes; preflight's append-only
set is unchanged.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("funding_rates", sa.Column("short_rate", sa.String(64), nullable=True))
    op.add_column(
        "instruments",
        sa.Column("copyable", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("instruments", "copyable")
    op.drop_column("funding_rates", "short_rate")
