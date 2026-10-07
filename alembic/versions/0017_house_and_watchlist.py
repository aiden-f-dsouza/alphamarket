"""house strategies and private watchlists (docs/copy_strategies_plan.md)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-07

users.is_house marks Koine's own accounts: a normal user (verified email,
TOTP, step-up to connect), whose strategies carry the "Run by Koine" badge.
users.is_system marks the one user that owns shared watch accounts; it can
never log in.

A watched address is a venue account with is_watch set, owned by the system
user, never claimed, with one strategy of kind 'watch' that is never public
(invariant 4). The partial unique index makes two watchers of one address
share one account and one ingestion. `watches` links users to the watched
strategies (mutable: removing a watch deletes the row). `watch_optouts` is
append-only: an address owner who proved the address asked not to be tracked.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def _ts(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_house", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "users",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "strategies",
        sa.Column("kind", sa.String(10), nullable=False, server_default="creator"),
    )
    op.add_column(
        "venue_accounts",
        sa.Column("is_watch", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "uq_venue_accounts_watch_address",
        "venue_accounts",
        ["venue", "key_id"],
        unique=True,
        postgresql_where=sa.text("is_watch"),
        sqlite_where=sa.text("is_watch"),
    )

    op.create_table(
        "watches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("strategy_id", sa.Integer(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("label", sa.String(60), nullable=False, server_default=""),
        _ts("created_at"),
        sa.UniqueConstraint("user_id", "strategy_id", name="uq_watches_user_strategy"),
    )
    op.create_index("ix_watches_user_id", "watches", ["user_id"])
    op.create_index("ix_watches_strategy_id", "watches", ["strategy_id"])

    op.create_table(
        "watch_optouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("address", sa.String(120), nullable=False),
        sa.Column("proved_by_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("venue", "address", name="uq_watch_optouts_venue_address"),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER watch_optouts_append_only
            BEFORE UPDATE OR DELETE ON watch_optouts
            FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS watch_optouts_append_only ON watch_optouts;")
    op.drop_table("watch_optouts")
    op.drop_index("ix_watches_strategy_id", table_name="watches")
    op.drop_index("ix_watches_user_id", table_name="watches")
    op.drop_table("watches")
    op.drop_index("uq_venue_accounts_watch_address", table_name="venue_accounts")
    op.drop_column("venue_accounts", "is_watch")
    op.drop_column("strategies", "kind")
    op.drop_column("users", "is_system")
    op.drop_column("users", "is_house")
