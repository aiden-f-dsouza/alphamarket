"""account security: DB sessions, TOTP, recovery codes, email tokens, rate limits (P3)

Revision ID: 0011
Revises: 0008
Create Date: 2026-09-26

None of these tables is a ledger, so no append-only trigger applies
(invariant 3 covers fills, settlements, signals and mirror rows). Secrets are
stored only as SHA-256 hashes (session ids, recovery codes, email tokens) or
vault-encrypted (the TOTP shared secret).
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0008"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.add_column("users", _ts("email_verified_at", nullable=True))

    op.create_table(
        "sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        _ts("created_at", nullable=False),
        _ts("last_seen_at", nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("mfa_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("step_up_at", nullable=True),
        _ts("revoked_at", nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "user_totp",
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=False),
        _ts("enabled_at", nullable=True),
        sa.Column("last_used_step", sa.BigInteger(), nullable=True),
        _ts("locked_until", nullable=True),
        _ts("created_at", nullable=False),
    )

    op.create_table(
        "totp_recovery_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False, unique=True),
        _ts("created_at", nullable=False),
        _ts("used_at", nullable=True),
    )
    op.create_index("ix_totp_recovery_codes_user_id", "totp_recovery_codes", ["user_id"])

    op.create_table(
        "auth_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        _ts("created_at", nullable=False),
        _ts("expires_at", nullable=False),
        _ts("used_at", nullable=True),
    )
    op.create_index("ix_auth_tokens_user_id", "auth_tokens", ["user_id"])

    op.create_table(
        "rate_limit_counters",
        sa.Column("key", sa.String(200), nullable=False),
        sa.Column("window_start", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        _ts("expires_at", nullable=False),
        sa.PrimaryKeyConstraint("key", "window_start", name="pk_rate_limit_counters"),
    )
    op.create_index("ix_rate_limit_counters_expires_at", "rate_limit_counters", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_rate_limit_counters_expires_at", table_name="rate_limit_counters")
    op.drop_table("rate_limit_counters")
    op.drop_index("ix_auth_tokens_user_id", table_name="auth_tokens")
    op.drop_table("auth_tokens")
    op.drop_index("ix_totp_recovery_codes_user_id", table_name="totp_recovery_codes")
    op.drop_table("totp_recovery_codes")
    op.drop_table("user_totp")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_column("users", "email_verified_at")
