"""w3-perf: paper book checkpoints and mirror ledger indexes

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-04

paper_book_checkpoints is a CACHE of each paper allocation's book (cash,
holdings, perp book) folded from mirror_ledger_entries / mirror_asset_fills up
to stored ids, so the mirror pass folds only newer rows instead of replaying
the whole follower ledger (paper_book.book_state). It is derived, never the
source of truth: it is NOT in the append-only set (no trigger; preflight's
EXPECTED_APPEND_ONLY_TRIGGERS is unchanged), may be deleted at any time, is
rebuilt by the next mirror pass, and is used only after its row counts match
the ledger (a mismatch falls back to a full replay and is logged).

The three indexes serve the hot queries: (allocation_id, id) for the
checkpoint delta and count, (allocation_id, occurred_at) for the book's
newest event (paper_book.booked_through), and (allocation_id, instrument_id,
kind, occurred_at) for the funding watermark and booked refs
(paper_lifecycle). Building them blocks writes to mirror_ledger_entries; on
a large table, create them CONCURRENTLY by hand first under the same names
and this migration skips them (if_not_exists).
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_book_checkpoints",
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("allocations.id"), primary_key=True),
        sa.Column("through_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("entry_count", sa.BigInteger(), nullable=False),
        sa.Column("entries_through_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("through_fill_id", sa.BigInteger(), nullable=False),
        sa.Column("fill_count", sa.BigInteger(), nullable=False),
        sa.Column("fills_through_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_mirror_ledger_entries_alloc_id",
        "mirror_ledger_entries",
        ["allocation_id", "id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_mirror_ledger_entries_alloc_at",
        "mirror_ledger_entries",
        ["allocation_id", "occurred_at"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_mirror_ledger_entries_alloc_inst_kind_at",
        "mirror_ledger_entries",
        ["allocation_id", "instrument_id", "kind", "occurred_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mirror_ledger_entries_alloc_inst_kind_at", "mirror_ledger_entries")
    op.drop_index("ix_mirror_ledger_entries_alloc_at", "mirror_ledger_entries")
    op.drop_index("ix_mirror_ledger_entries_alloc_id", "mirror_ledger_entries")
    op.drop_table("paper_book_checkpoints")
