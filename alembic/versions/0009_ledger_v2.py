"""ledger v2: instruments, legs, asset fills, anchors, daily prices (P1)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26

New tables only; no existing row is touched (invariant 3). Binary legs for
fills/settlements already in the ledger are written by ingest on the next
sync (ingest.write_binary_legs), not here, so this migration stays pure DDL.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# forbid_ledger_mutation() was created in 0003. Trigger names end in
# "_append_only" so scripts/preflight.py finds them.
APPEND_ONLY_TABLES = ("ledger_entries", "asset_fills", "ledger_quarantines")

MAX_DECIMALS = 12


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _decimals_check(column: str, table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} >= 0 AND {column} <= {MAX_DECIMALS}", name=f"ck_{table}_{column}"
    )


def _account_fk() -> sa.Column:
    return sa.Column(
        "venue_account_id", sa.Integer(), sa.ForeignKey("venue_accounts.id"), nullable=False
    )


def upgrade() -> None:
    op.create_table(
        "instruments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("symbol", sa.String(160), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("base_asset", sa.String(120), nullable=False),
        sa.Column("quote_asset", sa.String(40), nullable=False),
        sa.Column("qty_decimals", sa.Integer(), nullable=False),
        sa.Column("qty_step_atoms", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("min_qty_atoms", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("min_notional", sa.String(64), nullable=True),
        sa.Column("multiplier", sa.Integer(), nullable=False, server_default="1"),
        _ts("expiry", nullable=True),
        _ts("first_seen_at", nullable=False),
        sa.UniqueConstraint("venue", "symbol", name="uq_instruments_venue_symbol"),
        _decimals_check("qty_decimals", "instruments"),
    )

    op.create_table(
        "symbol_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("symbol", sa.String(160), nullable=False),
        _ts("valid_from", nullable=False),
        _ts("valid_to", nullable=True),
    )
    op.create_index("ix_symbol_history_instrument_id", "symbol_history", ["instrument_id"])

    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _account_fk(),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=True),
        sa.Column("asset", sa.String(120), nullable=False),
        sa.Column("delta_atoms", sa.BigInteger(), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("venue_ref", sa.String(255), nullable=False),
        sa.Column("group_ref", sa.String(300), nullable=False),
        sa.Column("balance_after_atoms", sa.BigInteger(), nullable=True),
        _ts("occurred_at", nullable=False),
        _ts("ingested_at", nullable=False),
        sa.UniqueConstraint(
            "venue_account_id", "venue_ref", "kind", "asset", name="uq_ledger_entries_identity"
        ),
        _decimals_check("decimals", "ledger_entries"),
    )
    op.create_index(
        "ix_ledger_entries_account_group", "ledger_entries", ["venue_account_id", "group_ref"]
    )
    op.create_index(
        "ix_ledger_entries_account_asset", "ledger_entries", ["venue_account_id", "asset"]
    )
    op.create_index("ix_ledger_entries_occurred_at", "ledger_entries", ["occurred_at"])

    op.create_table(
        "asset_fills",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _account_fk(),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("venue_fill_id", sa.String(255), nullable=False),
        sa.Column("order_id", sa.String(255), nullable=True),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("qty_atoms", sa.BigInteger(), nullable=False),
        sa.Column("qty_decimals", sa.Integer(), nullable=False),
        sa.Column("price", sa.String(64), nullable=False),
        sa.Column("liquidity", sa.String(10), nullable=True),
        _ts("filled_at", nullable=False),
        _ts("ingested_at", nullable=False),
        sa.UniqueConstraint(
            "venue_account_id", "venue_fill_id", name="uq_asset_fills_account_fill"
        ),
        _decimals_check("qty_decimals", "asset_fills"),
    )
    op.create_index("ix_asset_fills_venue_account_id", "asset_fills", ["venue_account_id"])
    op.create_index("ix_asset_fills_instrument_id", "asset_fills", ["instrument_id"])
    op.create_index("ix_asset_fills_filled_at", "asset_fills", ["filled_at"])

    op.create_table(
        "anchor_balances",
        sa.Column("id", sa.Integer(), primary_key=True),
        _account_fk(),
        sa.Column("asset", sa.String(120), nullable=False),
        sa.Column("atoms", sa.BigInteger(), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.String(64), nullable=True),
        sa.Column("mark_price", sa.String(64), nullable=True),
        _ts("as_of", nullable=False),
        sa.UniqueConstraint(
            "venue_account_id", "asset", "as_of", name="uq_anchor_balances_asset_at"
        ),
        _decimals_check("decimals", "anchor_balances"),
    )
    op.create_index("ix_anchor_balances_venue_account_id", "anchor_balances", ["venue_account_id"])

    op.create_table(
        "prices_daily",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("close", sa.String(64), nullable=False),
        sa.Column("usd_micros_per_unit", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("instrument_id", "day", name="uq_prices_daily_inst_day"),
    )
    op.create_index("ix_prices_daily_instrument_id", "prices_daily", ["instrument_id"])

    op.create_table(
        "ledger_sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        _account_fk(),
        sa.Column("stream", sa.String(20), nullable=False),
        sa.Column("resume_cursor", sa.Text(), nullable=True),
        _ts("updated_at", nullable=False),
        sa.UniqueConstraint("venue_account_id", "stream", name="uq_ledger_sync_state_stream"),
    )

    op.create_table(
        "ledger_quarantines",
        sa.Column("id", sa.Integer(), primary_key=True),
        _account_fk(),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ref", sa.String(300), nullable=True),
        _ts("created_at", nullable=False),
    )
    op.create_index(
        "ix_ledger_quarantines_venue_account_id", "ledger_quarantines", ["venue_account_id"]
    )

    op.create_table(
        "asset_reconciliations",
        sa.Column("id", sa.Integer(), primary_key=True),
        _account_fk(),
        _ts("as_of", nullable=False),
        sa.Column("asset", sa.String(120), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("venue_atoms", sa.BigInteger(), nullable=False),
        sa.Column("computed_atoms", sa.BigInteger(), nullable=False),
        sa.Column("divergence_atoms", sa.BigInteger(), nullable=False),
        sa.Column("tolerance_atoms", sa.BigInteger(), nullable=False),
        sa.Column("reconciled", sa.Boolean(), nullable=False),
        _ts("created_at", nullable=False),
    )
    op.create_index(
        "ix_asset_reconciliations_venue_account_id", "asset_reconciliations", ["venue_account_id"]
    )
    op.create_index("ix_asset_reconciliations_as_of", "asset_reconciliations", ["as_of"])

    # Same structural append-only guarantee as fills/settlements (0003) and
    # signals/mirror rows (0007): legs and asset fills are ledger facts, and
    # the quarantine log is an audit trail.
    if op.get_bind().dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
                """
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in APPEND_ONLY_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
    for table in (
        "asset_reconciliations",
        "ledger_quarantines",
        "ledger_sync_state",
        "prices_daily",
        "anchor_balances",
        "asset_fills",
        "ledger_entries",
        "symbol_history",
        "instruments",
    ):
        op.drop_table(table)
