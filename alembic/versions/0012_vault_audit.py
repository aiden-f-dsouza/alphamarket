"""credentials v2 (venue_credentials), audit_log, scope evidence (P3)

Revision ID: 0012
Revises: 0008
Create Date: 2026-09-26

Data migration: every venue_accounts.encrypted_private_key is copied into
venue_credentials as a "legacy" row (ciphertext = the v1 Fernet token, no
wrapped data key). The copy is pure SQL, so the migration never needs
VAULT_KEY or KMS access; `credentials.rotate_all` then re-seals legacy rows
under envelope encryption. The old column stays for one release (read as a
fallback, never written) and is dropped by a later migration.
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0010"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), **kw)


def upgrade() -> None:
    op.create_table(
        "venue_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "venue_account_id", sa.Integer(), sa.ForeignKey("venue_accounts.id"), nullable=False
        ),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=True),  # NULL once revoked
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=True),  # NULL = legacy row
        sa.Column("key_version", sa.String(200), nullable=False),
        sa.Column("account_ref", sa.String(120), nullable=True),
        _ts("expires_at", nullable=True),
        sa.Column("refresh_ciphertext", sa.LargeBinary(), nullable=True),
        _ts("created_at", nullable=False),
        _ts("rotated_at", nullable=True),
        _ts("revoked_at", nullable=True),
    )
    op.create_index(
        "ix_venue_credentials_venue_account_id", "venue_credentials", ["venue_account_id"]
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),  # no FK: outlives the user
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.String(80), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("request_or_job_id", sa.String(80), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        _ts("at", nullable=False),
    )
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_at", "audit_log", ["at"])

    op.add_column("venue_accounts", sa.Column("scope_evidence", sa.JSON(), nullable=True))

    # Copy v1 keys. Only Kalshi held a secret before P3 (RSA PEM).
    op.execute(
        """
        INSERT INTO venue_credentials
            (venue_account_id, kind, ciphertext, wrapped_dek, key_version, created_at)
        SELECT id,
               CASE venue WHEN 'kalshi' THEN 'rsa_pem' ELSE 'key_secret' END,
               encrypted_private_key, NULL, 'legacy', CURRENT_TIMESTAMP
        FROM venue_accounts
        WHERE encrypted_private_key IS NOT NULL
        """
    )

    # audit_log is append-only like the ledger (invariant 3). Reuses the
    # function from migration 0003.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE TRIGGER audit_log_append_only
            BEFORE UPDATE OR DELETE ON audit_log
            FOR EACH ROW EXECUTE FUNCTION forbid_ledger_mutation();
            """
        )


def downgrade() -> None:
    # Put keys back where 0008 code reads them. Legacy rows go back verbatim.
    # Envelope rows are written as vault v2 "kv2." blobs (built in SQL, no key
    # needed), which vault.decrypt_secret reads; pre-P3 code cannot, so
    # downgrade the code only after re-running with KEY_WRAPPER=local and
    # re-connecting any account connected after this migration.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;")
        op.execute(
            """
            UPDATE venue_accounts va
            SET encrypted_private_key = CASE
                WHEN vc.wrapped_dek IS NULL THEN vc.ciphertext
                ELSE convert_to(
                    'kv2.'
                    || rtrim(translate(encode(convert_to(vc.key_version, 'UTF8'), 'base64'),
                                       E'+/\\n', '-_'), '=')
                    || '.'
                    || rtrim(translate(encode(vc.wrapped_dek, 'base64'), E'+/\\n', '-_'), '=')
                    || '.', 'UTF8') || vc.ciphertext
                END
            FROM venue_credentials vc
            WHERE vc.venue_account_id = va.id
              AND vc.revoked_at IS NULL
              AND vc.ciphertext IS NOT NULL
              AND va.encrypted_private_key IS NULL
            """
        )
    else:  # SQLite (dev only): legacy rows back verbatim
        op.execute(
            """
            UPDATE venue_accounts SET encrypted_private_key = (
                SELECT vc.ciphertext FROM venue_credentials vc
                WHERE vc.venue_account_id = venue_accounts.id AND vc.revoked_at IS NULL
                  AND vc.wrapped_dek IS NULL AND vc.ciphertext IS NOT NULL
                ORDER BY vc.id DESC LIMIT 1)
            WHERE encrypted_private_key IS NULL
            """
        )
    op.drop_column("venue_accounts", "scope_evidence")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_venue_credentials_venue_account_id", table_name="venue_credentials")
    op.drop_table("venue_credentials")
