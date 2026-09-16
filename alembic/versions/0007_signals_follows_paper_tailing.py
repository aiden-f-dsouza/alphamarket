"""market prices, signals, follows, paper allocations (M6)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

APPEND_ONLY_TABLES = ("signals", "mirror_fills", "mirror_settlements")


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "market_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("venue_env", sa.String(10), nullable=False),
        sa.Column("ticker", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("yes_bid_micros", sa.BigInteger(), nullable=False),
        sa.Column("yes_ask_micros", sa.BigInteger(), nullable=False),
        sa.Column("last_price_micros", sa.BigInteger(), nullable=False),
        sa.Column("result", sa.String(20), nullable=False, server_default=""),
        _ts("as_of", nullable=False),
        _ts("created_at", nullable=False),
    )
    op.create_index("ix_market_prices_ticker", "market_prices", ["ticker"])
    op.create_index("ix_market_prices_as_of", "market_prices", ["as_of"])

    op.create_table(
        "signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("fill_id", sa.Integer(), sa.ForeignKey("fills.id"), nullable=False, unique=True),
        sa.Column("ticker", sa.String(120), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("count_micros", sa.BigInteger(), nullable=False),
        sa.Column("price_micros", sa.BigInteger(), nullable=False),
        sa.Column("creator_equity_micros", sa.BigInteger(), nullable=False),
        sa.Column("position_after_micros", sa.BigInteger(), nullable=False),
        _ts("filled_at", nullable=False),
        _ts("emitted_at", nullable=False),
    )
    op.create_index("ix_signals_strategy_id", "signals", ["strategy_id"])
    op.create_index("ix_signals_ticker", "signals", ["ticker"])
    op.create_index("ix_signals_filled_at", "signals", ["filled_at"])

    op.create_table(
        "follows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("user_id", "strategy_id", name="uq_follows_user_strategy"),
    )
    op.create_index("ix_follows_user_id", "follows", ["user_id"])
    op.create_index("ix_follows_strategy_id", "follows", ["strategy_id"])

    op.create_table(
        "allocations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False, server_default="paper"),
        sa.Column("allocation_micros", sa.BigInteger(), nullable=False),
        sa.Column("drawdown_limit_bps", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("stop_reason", sa.String(40), nullable=True),
        sa.Column("high_water_micros", sa.BigInteger(), nullable=False),
        sa.Column("last_signal_id", sa.Integer(), nullable=False, server_default="0"),
        _ts("started_at", nullable=False),
        _ts("stopped_at", nullable=True),
    )
    op.create_index("ix_allocations_user_id", "allocations", ["user_id"])
    op.create_index("ix_allocations_strategy_id", "allocations", ["strategy_id"])

    op.create_table(
        "mirror_fills",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("allocations.id"), nullable=False),
        sa.Column("signal_id", sa.Integer(), sa.ForeignKey("signals.id"), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        sa.Column("ticker", sa.String(120), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("count_micros", sa.BigInteger(), nullable=False),
        sa.Column("price_micros", sa.BigInteger(), nullable=False),
        sa.Column("fee_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("venue_order_id", sa.String(120), nullable=True),
        _ts("filled_at", nullable=False),
        _ts("created_at", nullable=False),
    )
    op.create_index("ix_mirror_fills_allocation_id", "mirror_fills", ["allocation_id"])
    op.create_index("ix_mirror_fills_signal_id", "mirror_fills", ["signal_id"])
    op.create_index("ix_mirror_fills_ticker", "mirror_fills", ["ticker"])
    op.create_index("ix_mirror_fills_filled_at", "mirror_fills", ["filled_at"])

    op.create_table(
        "mirror_settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("allocations.id"), nullable=False),
        sa.Column("ticker", sa.String(120), nullable=False),
        sa.Column("market_result", sa.String(20), nullable=False),
        sa.Column("yes_count_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("no_count_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("revenue_micros", sa.BigInteger(), nullable=False),
        _ts("settled_at", nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("allocation_id", "ticker", name="uq_mirror_settlements_alloc_ticker"),
    )
    op.create_index("ix_mirror_settlements_allocation_id", "mirror_settlements", ["allocation_id"])
    op.create_index("ix_mirror_settlements_ticker", "mirror_settlements", ["ticker"])
    op.create_index("ix_mirror_settlements_settled_at", "mirror_settlements", ["settled_at"])

    op.create_table(
        "allocation_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("allocation_id", sa.Integer(), sa.ForeignKey("allocations.id"), nullable=False),
        _ts("as_of", nullable=False),
        sa.Column("cash_micros", sa.BigInteger(), nullable=False),
        sa.Column("open_cost_micros", sa.BigInteger(), nullable=False),
        sa.Column("open_value_micros", sa.BigInteger(), nullable=False),
        sa.Column("equity_micros", sa.BigInteger(), nullable=False),
        sa.Column("unmarked_cost_micros", sa.BigInteger(), nullable=False, server_default="0"),
        _ts("created_at", nullable=False),
    )
    op.create_index(
        "ix_allocation_snapshots_allocation_id", "allocation_snapshots", ["allocation_id"]
    )
    op.create_index("ix_allocation_snapshots_as_of", "allocation_snapshots", ["as_of"])

    # Same structural append-only guarantee as fills/settlements (migration
    # 0003): signals and mirror rows are ledger facts.
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
        "allocation_snapshots",
        "mirror_settlements",
        "mirror_fills",
        "allocations",
        "follows",
        "signals",
        "market_prices",
    ):
        op.drop_table(table)
