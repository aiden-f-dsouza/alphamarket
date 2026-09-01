"""equity_snapshots, reconciliation anchor, and append-only ledger triggers

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

LEDGER_TABLES = ("fills", "settlements")


def upgrade() -> None:
    op.add_column(
        "venue_accounts", sa.Column("anchor_balance_micros", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "venue_accounts", sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True)
    )

    op.create_table(
        "equity_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("venue_balance_micros", sa.BigInteger(), nullable=False),
        sa.Column("computed_cash_micros", sa.BigInteger(), nullable=False),
        sa.Column("divergence_micros", sa.BigInteger(), nullable=False),
        sa.Column("tolerance_micros", sa.BigInteger(), nullable=False),
        sa.Column("reconciled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_equity_snapshots_strategy_id", "equity_snapshots", ["strategy_id"])
    op.create_index("ix_equity_snapshots_as_of", "equity_snapshots", ["as_of"])

    # Structural append-only enforcement (invariant #3). Triggers rather than
    # role-level REVOKE: they bind to the table, not to who is connecting, so
    # they hold for the table owner, for raw SQL, and for future app roles.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION forbid_ledger_mutation() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'ledger table % is append-only (no UPDATE/DELETE)', TG_TABLE_NAME;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        for table in LEDGER_TABLES:
            op.execute(
                f"""
                CREATE TRIGGER {table}_append_only
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
                """
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table in LEDGER_TABLES:
            op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table};")
        op.execute("DROP FUNCTION IF EXISTS forbid_ledger_mutation();")
    op.drop_table("equity_snapshots")
    op.drop_column("venue_accounts", "anchor_at")
    op.drop_column("venue_accounts", "anchor_balance_micros")
