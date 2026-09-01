"""venue_accounts, strategies, and the append-only ledger (fills, settlements)

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "venue_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("venue", sa.String(length=40), nullable=False),
        sa.Column("venue_env", sa.String(length=10), nullable=False),
        sa.Column("key_id", sa.String(length=120), nullable=False),
        sa.Column("encrypted_private_key", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("scope_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_venue_accounts_user_id", "venue_accounts", ["user_id"])

    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "venue_account_id",
            sa.Integer(),
            sa.ForeignKey("venue_accounts.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("style_tags", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("listed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_strategies_user_id", "strategies", ["user_id"])

    op.create_table(
        "fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("venue_fill_id", sa.String(length=120), nullable=False),
        sa.Column("ticker", sa.String(length=120), nullable=False),
        sa.Column("side", sa.String(length=10), nullable=False),
        sa.Column("action", sa.String(length=10), nullable=False),
        sa.Column("count_micros", sa.BigInteger(), nullable=False),
        sa.Column("price_micros", sa.BigInteger(), nullable=False),
        sa.Column("fee_micros", sa.BigInteger(), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_fills_strategy_id", "fills", ["strategy_id"])
    op.create_index("ix_fills_ticker", "fills", ["ticker"])
    op.create_index("ix_fills_filled_at", "fills", ["filled_at"])
    op.create_index("ix_fills_venue_fill_id", "fills", ["venue_fill_id"], unique=True)

    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("venue_settlement_id", sa.String(length=200), nullable=False),
        sa.Column("ticker", sa.String(length=120), nullable=False),
        sa.Column("market_result", sa.String(length=20), nullable=False),
        sa.Column("yes_count_micros", sa.BigInteger(), nullable=False),
        sa.Column("no_count_micros", sa.BigInteger(), nullable=False),
        sa.Column("revenue_micros", sa.BigInteger(), nullable=False),
        sa.Column("fee_micros", sa.BigInteger(), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_settlements_strategy_id", "settlements", ["strategy_id"])
    op.create_index("ix_settlements_ticker", "settlements", ["ticker"])
    op.create_index("ix_settlements_settled_at", "settlements", ["settled_at"])
    op.create_index(
        "ix_settlements_venue_settlement_id",
        "settlements",
        ["venue_settlement_id"],
        unique=True,
    )
    # M2 hardens append-only structurally: the app's DB role gets UPDATE/DELETE
    # revoked on fills/settlements. Deferred until the app role split exists.


def downgrade() -> None:
    op.drop_table("settlements")
    op.drop_table("fills")
    op.drop_table("strategies")
    op.drop_table("venue_accounts")
