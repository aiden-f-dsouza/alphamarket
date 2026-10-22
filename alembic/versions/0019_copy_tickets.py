"""copy tickets (docs/copy_tickets_plan.md)

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-22

A paper tail can turn on copy tickets: each new paper fill also becomes a
ticket, the same trade written as an order the user may place THEMSELVES on
the venue. Koine never places it (invariant 8). When the user picks their own
connected read-only account on that venue, their real fills are matched to
the tickets and compared with the paper fill.

`allocations` gains the settings (copy_tickets, copy_enabled_at, copy_email,
copy_account_id). `copy_tickets` holds one row per issued ticket. Its status
and match are updated in place, so it is NOT append-only (like mirror_skips);
the unique source ids make issuing idempotent, and the check constraint
keeps exactly one source per row.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.add_column(
        "allocations",
        sa.Column("copy_tickets", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("allocations", _ts("copy_enabled_at", nullable=True))
    op.add_column(
        "allocations",
        sa.Column("copy_email", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("allocations") as batch:  # SQLite needs a batch for the FK
        batch.add_column(sa.Column("copy_account_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_allocations_copy_account_id_venue_accounts",
            "venue_accounts",
            ["copy_account_id"],
            ["id"],
        )
    op.create_index("ix_allocations_copy_account_id", "allocations", ["copy_account_id"])

    big_id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
    op.create_table(
        "copy_tickets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("allocations.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("mirror_fill_id", sa.Integer(), sa.ForeignKey("mirror_fills.id"), nullable=True),
        sa.Column(
            "mirror_asset_fill_id",
            big_id,
            sa.ForeignKey("mirror_asset_fills.id"),
            nullable=True,
        ),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=True),
        sa.Column("ticker", sa.String(160), nullable=False),
        sa.Column("label", sa.String(160), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("qty_atoms", sa.BigInteger(), nullable=False),
        sa.Column("qty_decimals", sa.Integer(), nullable=False),
        sa.Column("paper_price", sa.String(64), nullable=False),
        sa.Column("creator_price", sa.String(64), nullable=False),
        sa.Column("stake_bps", sa.Integer(), nullable=True),
        _ts("created_at"),
        _ts("expires_at"),
        sa.Column("status", sa.String(12), nullable=False),
        _ts("status_at", nullable=True),
        sa.Column("matched_qty_atoms", sa.BigInteger(), nullable=True),
        sa.Column("matched_price", sa.String(64), nullable=True),
        _ts("matched_at", nullable=True),
        sa.Column("matched_refs", sa.JSON(), nullable=True),
        sa.UniqueConstraint("mirror_fill_id", name="uq_copy_tickets_mirror_fill_id"),
        sa.UniqueConstraint("mirror_asset_fill_id", name="uq_copy_tickets_mirror_asset_fill_id"),
        sa.CheckConstraint(
            "(mirror_fill_id IS NULL) <> (mirror_asset_fill_id IS NULL)",
            name="ck_copy_tickets_one_source",
        ),
        sa.CheckConstraint(
            "qty_decimals >= 0 AND qty_decimals <= 12", name="ck_copy_tickets_qty_decimals"
        ),
    )
    op.create_index("ix_copy_tickets_allocation_id", "copy_tickets", ["allocation_id"])
    op.create_index("ix_copy_tickets_user_id", "copy_tickets", ["user_id"])
    op.create_index("ix_copy_tickets_status_expires", "copy_tickets", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_copy_tickets_status_expires", table_name="copy_tickets")
    op.drop_index("ix_copy_tickets_user_id", table_name="copy_tickets")
    op.drop_index("ix_copy_tickets_allocation_id", table_name="copy_tickets")
    op.drop_table("copy_tickets")
    op.drop_index("ix_allocations_copy_account_id", table_name="allocations")
    with op.batch_alter_table("allocations") as batch:
        batch.drop_constraint("fk_allocations_copy_account_id_venue_accounts", type_="foreignkey")
        batch.drop_column("copy_account_id")
    op.drop_column("allocations", "copy_email")
    op.drop_column("allocations", "copy_enabled_at")
    op.drop_column("allocations", "copy_tickets")
