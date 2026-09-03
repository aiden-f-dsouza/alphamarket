"""Production preflight (M5.1): refuse to launch with an unsafe config.

Run:  uv run python scripts/preflight.py
Exit code 0 = safe to point real users at this deployment.
"""

import sys

from sqlalchemy import text

from alphamarket.config import get_settings
from alphamarket.db import engine


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

    try:
        with engine.connect() as conn:
            head = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            dialect = conn.dialect.name
            if dialect != "postgresql":
                problems.append(f"database is {dialect}, not postgresql")
            else:
                trig = conn.execute(
                    text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE '%_append_only'")
                ).scalar()
                if trig < 2:
                    problems.append("append-only triggers missing on fills/settlements")
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
