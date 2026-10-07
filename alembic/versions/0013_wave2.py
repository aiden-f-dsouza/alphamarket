"""wave2: signals v2, paper asset ledger, market data, consent, activity flags

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-01

Signals gain asset columns and lose NOT NULL on the binary-only ones (DDL
only: ALTER TABLE does not fire the append-only row trigger, and no row is
touched). New tables for the asset paper book (mirror_asset_fills,
mirror_ledger_entries, mirror_skips), market data (asset_quotes,
funding_rates, corporate_actions), venue activity flags and consent
(invariant 10). Indexes for housekeeping and the security page.

Workstream migrations, if a stream needs one (unused numbers stay unused).
Each: file `00NN_w2_<label>.py`, revision "00NN", down_revision "0013";
they are re-chained in merge order at integration:
    0014 market-data   0015 stats-v2       0016 p6-signals     0017 p6-engine
    0018 p6-lifecycle  0019 p8-connect     0020 p8-pages       0021 account-security
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# forbid_ledger_mutation() was created in 0003. Trigger names end in
# "_append_only" so scripts/preflight.py finds them.
APPEND_ONLY_TABLES = (
    "mirror_asset_fills",
    "mirror_ledger_entries",
    "venue_activity_flags",
    "consent_texts",
    "venue_consents",
)

MAX_DECIMALS = 12


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def _decimals_check(column: str, table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} >= 0 AND {column} <= {MAX_DECIMALS}", name=f"ck_{table}_{column}"
    )


def _fk(name: str, target: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.Integer(), sa.ForeignKey(target), nullable=nullable)


def upgrade() -> None:
    # ---- signals v2 ----
    with op.batch_alter_table("signals") as batch:
        batch.alter_column("fill_id", existing_type=sa.Integer(), nullable=True)
        batch.alter_column("count_micros", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column("price_micros", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column("position_after_micros", existing_type=sa.BigInteger(), nullable=True)
        batch.alter_column(
            "ticker", existing_type=sa.String(120), type_=sa.String(160), existing_nullable=False
        )
        batch.add_column(
            sa.Column("asset_fill_id", sa.BigInteger(), sa.ForeignKey("asset_fills.id"))
        )
        batch.add_column(sa.Column("instrument_id", sa.Integer(), sa.ForeignKey("instruments.id")))
        batch.add_column(sa.Column("qty_atoms", sa.BigInteger()))
        batch.add_column(sa.Column("qty_decimals", sa.Integer()))
        batch.add_column(sa.Column("price", sa.String(64)))
        batch.add_column(sa.Column("notional_micros", sa.BigInteger()))
        batch.add_column(sa.Column("position_before_atoms", sa.BigInteger()))
        batch.add_column(sa.Column("position_after_atoms", sa.BigInteger()))
        batch.add_column(sa.Column("equity_basis_day", sa.Date()))
        batch.add_column(_ts("observed_at"))
        batch.add_column(sa.Column("quote_bid", sa.String(64)))
        batch.add_column(sa.Column("quote_ask", sa.String(64)))
        batch.add_column(sa.Column("quote_last", sa.String(64)))
        batch.add_column(_ts("quote_at"))
        batch.add_column(sa.Column("quote_source", sa.String(40)))
        batch.create_unique_constraint("signals_asset_fill_id_key", ["asset_fill_id"])
        batch.create_check_constraint(
            "ck_signals_one_source", "(fill_id IS NULL) <> (asset_fill_id IS NULL)"
        )
    op.create_index("ix_signals_instrument_id", "signals", ["instrument_id"])

    # ---- paper asset book ----
    op.create_table(
        "mirror_asset_fills",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _fk("allocation_id", "allocations.id"),
        _fk("signal_id", "signals.id", nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("mode", sa.String(10), nullable=False),
        _fk("instrument_id", "instruments.id"),
        sa.Column("action", sa.String(10), nullable=False),
        sa.Column("qty_atoms", sa.BigInteger(), nullable=False),
        sa.Column("qty_decimals", sa.Integer(), nullable=False),
        sa.Column("price", sa.String(64), nullable=False),
        sa.Column("notional_micros", sa.BigInteger(), nullable=False),
        sa.Column("fee_micros", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("price_basis", sa.String(30), nullable=False),
        sa.Column("penalty_bps", sa.Integer(), nullable=True),
        _ts("quote_at", nullable=True),
        sa.Column("venue_order_id", sa.String(120), nullable=True),
        _ts("filled_at", nullable=False),
        _ts("created_at", nullable=False),
        _decimals_check("qty_decimals", "mirror_asset_fills"),
    )
    op.create_index("ix_mirror_asset_fills_allocation_id", "mirror_asset_fills", ["allocation_id"])
    op.create_index("ix_mirror_asset_fills_signal_id", "mirror_asset_fills", ["signal_id"])
    op.create_index("ix_mirror_asset_fills_filled_at", "mirror_asset_fills", ["filled_at"])
    op.create_index(
        "uq_mirror_asset_fills_alloc_signal",
        "mirror_asset_fills",
        ["allocation_id", "signal_id"],
        unique=True,
        postgresql_where=sa.text("signal_id IS NOT NULL"),
        sqlite_where=sa.text("signal_id IS NOT NULL"),
    )

    op.create_table(
        "mirror_ledger_entries",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _fk("allocation_id", "allocations.id"),
        _fk("instrument_id", "instruments.id", nullable=True),
        sa.Column("asset", sa.String(120), nullable=False),
        sa.Column("delta_atoms", sa.BigInteger(), nullable=False),
        sa.Column("decimals", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ref", sa.String(255), nullable=False),
        sa.Column("group_ref", sa.String(300), nullable=False),
        _ts("occurred_at", nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint(
            "allocation_id", "ref", "kind", "asset", name="uq_mirror_ledger_entries_identity"
        ),
        _decimals_check("decimals", "mirror_ledger_entries"),
    )
    op.create_index(
        "ix_mirror_ledger_entries_alloc_asset", "mirror_ledger_entries", ["allocation_id", "asset"]
    )
    op.create_index(
        "ix_mirror_ledger_entries_occurred_at", "mirror_ledger_entries", ["occurred_at"]
    )

    op.create_table(
        "mirror_skips",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("allocation_id", "allocations.id"),
        _fk("signal_id", "signals.id"),
        sa.Column("reason", sa.String(40), nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("allocation_id", "signal_id", name="uq_mirror_skips_alloc_signal"),
    )
    op.create_index("ix_mirror_skips_allocation_id", "mirror_skips", ["allocation_id"])

    # ---- market data ----
    op.create_table(
        "asset_quotes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        _fk("instrument_id", "instruments.id"),
        sa.Column("bid", sa.String(64), nullable=True),
        sa.Column("ask", sa.String(64), nullable=True),
        sa.Column("last", sa.String(64), nullable=True),
        _ts("as_of", nullable=False),
        sa.Column("source", sa.String(40), nullable=False),
        _ts("created_at", nullable=False),
    )
    op.create_index("ix_asset_quotes_inst_as_of", "asset_quotes", ["instrument_id", "as_of"])

    op.create_table(
        "funding_rates",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("instrument_id", "instruments.id"),
        _ts("at", nullable=False),
        sa.Column("rate", sa.String(64), nullable=False),
        sa.Column("mark_price", sa.String(64), nullable=True),
        sa.Column("source", sa.String(40), nullable=False),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint("instrument_id", "at", name="uq_funding_rates_inst_at"),
    )
    op.create_index("ix_funding_rates_instrument_id", "funding_rates", ["instrument_id"])
    op.create_index("ix_funding_rates_at", "funding_rates", ["at"])

    op.create_table(
        "corporate_actions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("symbol", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("ex_date", sa.Date(), nullable=False),
        sa.Column("pay_date", sa.Date(), nullable=True),
        sa.Column("ratio_new", sa.Integer(), nullable=True),
        sa.Column("ratio_old", sa.Integer(), nullable=True),
        sa.Column("cash_per_share", sa.String(64), nullable=True),
        sa.Column("new_symbol", sa.String(40), nullable=True),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint(
            "source", "symbol", "kind", "ex_date", name="uq_corporate_actions_identity"
        ),
    )
    op.create_index("ix_corporate_actions_symbol", "corporate_actions", ["symbol"])
    op.create_index("ix_corporate_actions_ex_date", "corporate_actions", ["ex_date"])

    with op.batch_alter_table("instruments") as batch:
        batch.add_column(sa.Column("margin_tiers", sa.JSON(), nullable=True))
        batch.add_column(_ts("margin_tiers_at", nullable=True))

    with op.batch_alter_table("equity_snapshots") as batch:
        batch.add_column(sa.Column("venue_equity_micros", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("computed_equity_micros", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("equity_tolerance_micros", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("equity_reconciled", sa.Boolean(), nullable=True))

    # ---- activity flags and consent ----
    op.create_table(
        "venue_activity_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("venue_account_id", "venue_accounts.id"),
        sa.Column("venue_ref", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("activity_type", sa.String(60), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("symbol", sa.String(160), nullable=True),
        _ts("occurred_at", nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        _ts("created_at", nullable=False),
        sa.UniqueConstraint(
            "venue_account_id", "venue_ref", "kind", name="uq_venue_activity_flags_identity"
        ),
    )
    op.create_index(
        "ix_venue_activity_flags_venue_account_id", "venue_activity_flags", ["venue_account_id"]
    )

    op.create_table(
        "consent_texts",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("venue", sa.String(40), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _ts("created_at", nullable=False),
    )
    op.create_table(
        "venue_consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        _fk("user_id", "users.id"),
        sa.Column("venue", sa.String(40), nullable=False),
        _fk("venue_account_id", "venue_accounts.id", nullable=True),
        sa.Column(
            "text_sha256", sa.String(64), sa.ForeignKey("consent_texts.sha256"), nullable=False
        ),
        sa.Column("text_version", sa.String(40), nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        _ts("accepted_at", nullable=False),
    )
    op.create_index("ix_venue_consents_user_id", "venue_consents", ["user_id"])
    op.create_index("ix_venue_consents_venue_account_id", "venue_consents", ["venue_account_id"])

    # ---- account security ----
    op.add_column("auth_tokens", sa.Column("new_email", sa.String(320), nullable=True))
    op.create_index("ix_auth_tokens_expires_at", "auth_tokens", ["expires_at"])
    op.create_index("ix_sessions_created_at", "sessions", ["created_at"])
    op.create_index("ix_sessions_last_seen_at", "sessions", ["last_seen_at"])
    op.create_index("ix_sessions_revoked_at", "sessions", ["revoked_at"])
    op.create_index("ix_audit_log_target", "audit_log", ["target_type", "target_id"])

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

    op.drop_index("ix_audit_log_target", table_name="audit_log")
    op.drop_index("ix_sessions_revoked_at", table_name="sessions")
    op.drop_index("ix_sessions_last_seen_at", table_name="sessions")
    op.drop_index("ix_sessions_created_at", table_name="sessions")
    op.drop_index("ix_auth_tokens_expires_at", table_name="auth_tokens")
    op.drop_column("auth_tokens", "new_email")

    for table in (
        "venue_consents",
        "consent_texts",
        "venue_activity_flags",
        "corporate_actions",
        "funding_rates",
        "asset_quotes",
        "mirror_skips",
        "mirror_ledger_entries",
        "mirror_asset_fills",
    ):
        op.drop_table(table)

    with op.batch_alter_table("equity_snapshots") as batch:
        for column in (
            "equity_reconciled",
            "equity_tolerance_micros",
            "computed_equity_micros",
            "venue_equity_micros",
        ):
            batch.drop_column(column)
    with op.batch_alter_table("instruments") as batch:
        batch.drop_column("margin_tiers_at")
        batch.drop_column("margin_tiers")

    # Asset signals cannot survive the downgrade: binary columns are NOT NULL
    # again. Deleting them needs the append-only trigger out of the way, which
    # is acceptable only because the rows did not exist before 0013. Their
    # dependants (mirror_skips, mirror_asset_fills) are already dropped;
    # follows/feeds hold no FK and allocations.last_signal_id is a plain int.
    # DISABLE TRIGGER needs table ownership, which the migration role has
    # (docs/security_roles.sql); do not swap in session_replication_role.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE signals DISABLE TRIGGER signals_append_only;")
    op.execute("DELETE FROM signals WHERE asset_fill_id IS NOT NULL;")
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE signals ENABLE TRIGGER signals_append_only;")
    op.drop_index("ix_signals_instrument_id", table_name="signals")
    with op.batch_alter_table("signals") as batch:
        batch.drop_constraint("ck_signals_one_source", type_="check")
        batch.drop_constraint("signals_asset_fill_id_key", type_="unique")
        for column in (
            "quote_source",
            "quote_at",
            "quote_last",
            "quote_ask",
            "quote_bid",
            "observed_at",
            "equity_basis_day",
            "position_after_atoms",
            "position_before_atoms",
            "notional_micros",
            "price",
            "qty_decimals",
            "qty_atoms",
            "instrument_id",
            "asset_fill_id",
        ):
            batch.drop_column(column)
        batch.alter_column(
            "ticker", existing_type=sa.String(160), type_=sa.String(120), existing_nullable=False
        )
        batch.alter_column("position_after_micros", existing_type=sa.BigInteger(), nullable=False)
        batch.alter_column("price_micros", existing_type=sa.BigInteger(), nullable=False)
        batch.alter_column("count_micros", existing_type=sa.BigInteger(), nullable=False)
        batch.alter_column("fill_id", existing_type=sa.Integer(), nullable=False)
