"""wave2 market-data: market_data_coverage

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01

One row per (source, kind, scope) remembering what the prices_daily job has
already asked a data source for, so a rerun (and weekends, when a venue has
no daily bar) makes no request:

    kind "closes"            scope = the instrument's symbol at `source` (its
                             venue); covered_from..covered_through is the day
                             range already fetched
    kind "corporate_actions" scope = the equity symbol; fetched_on is the last
                             UTC day it was fetched
    kind "unpriced"          scope = an asset no source prices (not listed by
                             `source`, not in the CoinGecko map); fetched_on is
                             the last UTC day that was checked

A plain mutable table: it is a cache of "asked", never a fact about prices.
Deleting a row only costs one repeat request.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_data_coverage",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("scope", sa.String(160), nullable=False),
        sa.Column("covered_from", sa.Date(), nullable=True),
        sa.Column("covered_through", sa.Date(), nullable=True),
        sa.Column("fetched_on", sa.Date(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "kind", "scope", name="uq_market_data_coverage_identity"),
    )


def downgrade() -> None:
    op.drop_table("market_data_coverage")
