"""strategy_stats (M3)

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategy_stats",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("track_days", sa.Integer(), nullable=False),
        sa.Column("n_fills", sa.Integer(), nullable=False),
        sa.Column("n_settlements", sa.Integer(), nullable=False),
        sa.Column("total_return", sa.Float(), nullable=True),
        sa.Column("max_drawdown", sa.Float(), nullable=False),
        sa.Column("sharpe", sa.Float(), nullable=True),
        sa.Column("sortino", sa.Float(), nullable=True),
        sa.Column("win_rate", sa.Float(), nullable=True),
        sa.Column("risk_score", sa.Integer(), nullable=False),
        sa.Column("equity_micros", sa.BigInteger(), nullable=False),
        sa.Column("realized_pnl_micros", sa.BigInteger(), nullable=False),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("extras", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_strategy_stats_strategy_id", "strategy_stats", ["strategy_id"])
    op.create_index("ix_strategy_stats_as_of", "strategy_stats", ["as_of"])


def downgrade() -> None:
    op.drop_table("strategy_stats")
