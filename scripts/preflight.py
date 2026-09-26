"""Production preflight (M5.1): refuse to launch with an unsafe config.

Run:  uv run python scripts/preflight.py
Exit code 0 = safe to point real users at this deployment.
"""

import sys

from sqlalchemy import text

from alphamarket.config import get_settings
from alphamarket.db import engine

# Every append-only trigger the migrations install (invariant 3).
EXPECTED_APPEND_ONLY_TRIGGERS = {
    "fills_append_only",
    "settlements_append_only",
    "signals_append_only",
    "mirror_fills_append_only",
    "mirror_settlements_append_only",
    "audit_log_append_only",  # p3-vault
}


def vault_problems(s) -> list[str]:
    """p3-vault: prod wraps credential data keys with KMS, and the wrapper
    must round-trip for real (a live GenerateDataKey + Decrypt)."""
    from alphamarket import vault

    problems: list[str] = []
    if s.key_wrapper == "local" and s.kalshi_env == "prod":
        problems.append("KEY_WRAPPER=local in prod (credential data keys must be KMS-wrapped)")
    if s.key_wrapper == "aws_kms" and not s.kms_key_id:
        problems.append("KEY_WRAPPER=aws_kms but KMS_KEY_ID is not set")
    if s.key_wrapper not in ("local", "aws_kms"):
        problems.append(f"KEY_WRAPPER={s.key_wrapper!r} is not local | aws_kms")
    if not problems:
        try:
            vault.self_test()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"vault self-test failed: {type(exc).__name__}")
    return problems


def main() -> int:
    s = get_settings()
    problems: list[str] = []

    if s.secret_key == "dev-only-change-me" or len(s.secret_key) < 32:
        problems.append("SECRET_KEY is the dev default or too short")
    if not s.vault_key:
        problems.append("VAULT_KEY is not set (credential vault cannot operate)")
    if s.kalshi_env == "prod" and not s.secure_cookies:
        problems.append("KALSHI_ENV=prod but SECURE_COOKIES is false (session cookie over HTTP)")
    if s.kalshi_env == "prod" and s.public_base_url.startswith("http://"):
        problems.append("PUBLIC_BASE_URL is http:// in prod")
    if s.demo_mode:
        problems.append("DEMO_MODE is on (sample pages would be served to real users)")
    problems.extend(vault_problems(s))

    try:
        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            dialect = conn.dialect.name
            if dialect != "postgresql":
                problems.append(f"database is {dialect}, not postgresql")
            else:
                found = set(
                    conn.execute(
                        text("SELECT tgname FROM pg_trigger WHERE tgname LIKE '%_append_only'")
                    ).scalars()
                )
                missing = sorted(EXPECTED_APPEND_ONLY_TRIGGERS - found)
                if missing:
                    problems.append(f"append-only triggers missing: {', '.join(missing)}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"database unreachable: {type(exc).__name__}")
        head = None

    print(f"env={s.kalshi_env} db_head={head} secure_cookies={s.secure_cookies}")
    if problems:
        print("NOT READY:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("preflight OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
