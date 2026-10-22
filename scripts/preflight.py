"""Production preflight (M5.1): refuse to launch with an unsafe config.

Run:  uv run python scripts/preflight.py
Exit code 0 = safe to point real users at this deployment.
"""

import sys
from pathlib import Path

from sqlalchemy import text

from alphamarket.config import get_settings
from alphamarket.db import engine

# Invariant 3: every append-only table has a "<table>_append_only" trigger
# (migrations 0003, 0007, 0009, 0012, 0013, 0017).
EXPECTED_APPEND_ONLY_TRIGGERS = frozenset(
    f"{table}_append_only"
    for table in (
        "fills",
        "settlements",
        "signals",
        "mirror_fills",
        "mirror_settlements",
        "ledger_entries",
        "asset_fills",
        "ledger_quarantines",
        "audit_log",
        # wave2 (0013)
        "mirror_asset_fills",
        "mirror_ledger_entries",
        "venue_activity_flags",
        "consent_texts",
        "venue_consents",
        # watchlist (0017)
        "watch_optouts",
    )
)


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


def demo_problems(conn) -> list[str]:
    """Invariant 7 (w3-seed): a database holding demo users (scripts/
    seed_demo.py) is a demo database; real users must never be pointed at it."""
    try:
        n = conn.execute(text("SELECT count(*) FROM users WHERE is_demo")).scalar()
    except Exception as exc:  # noqa: BLE001
        return [f"could not check for demo users ({type(exc).__name__}; is 0015 applied?)"]
    if n:
        return [f"{n} demo user(s) in the database (seeded sample data; use a clean database)"]
    return []


def security_problems(s) -> list[str]:
    """P3 account-security settings (p3-auth)."""
    problems: list[str] = []
    if s.rate_limiter != "postgres":
        problems.append(f"RATE_LIMITER={s.rate_limiter} (per-process limits; use postgres in prod)")
    if s.email_backend != "smtp":
        problems.append(f"EMAIL_BACKEND={s.email_backend} (no email would reach users)")
    elif not s.smtp_host or s.email_from.endswith("@localhost>"):
        problems.append("SMTP_HOST / EMAIL_FROM not configured")
    if not s.hibp_enabled:
        problems.append("HIBP_ENABLED is false (breached passwords accepted)")
    if s.enable_unapproved_venues:
        problems.append(
            "ENABLE_UNAPPROVED_VENUES is true (venues awaiting written approval accept connects)"
        )
    return problems


ROOT = Path(__file__).resolve().parent.parent
TOS_SECTION = "## Terms-of-service position"
# A venue doc whose terms position carries one of these is not cleared for
# prod (docs/onchain_perps_plan.md S9): UNREAD until the terms are found and
# read, PENDING COUNSEL until counsel (or the venue, in writing) answers.
UNCLEARED_MARKERS = ("Position: UNREAD", "Position: PENDING COUNSEL")


def tos_position(doc_text: str) -> str | None:
    """The "Terms-of-service position" section of a venue doc (up to the next
    level-2 heading), or None when the doc has no such section."""
    lines = doc_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == TOS_SECTION:
            body = []
            for rest in lines[i + 1 :]:
                if rest.startswith("## "):
                    break
                body.append(rest)
            return "\n".join(body)
    return None


def watch_venue_problems(s, root: Path = ROOT) -> list[str]:
    """Every venue open for watching (WATCH_VENUES) needs a terms doc whose
    terms position is cleared: present, with a Terms-of-service position
    section, and no UNREAD / PENDING COUNSEL marker in it (invariant 10)."""
    from alphamarket.connectors.registry import PROFILES

    problems: list[str] = []
    for venue in [v.strip() for v in s.watch_venues.split(",") if v.strip()]:
        profile = PROFILES.get(venue)
        if profile is None:
            problems.append(f"WATCH_VENUES lists {venue!r}, which is not a known venue")
            continue
        # The profile's tos_doc, else the conventional path: Polymarket's doc
        # exists but is deliberately not on its profile (no consent paragraph
        # on its connect screen yet; docs/venues/polymarket.md).
        doc = profile.tos_doc or f"docs/venues/{venue}.md"
        path = root / doc
        if not path.is_file():
            problems.append(f"WATCH_VENUES lists {venue}, which has no terms doc ({doc})")
            continue
        section = tos_position(path.read_text(encoding="utf-8"))
        if section is None:
            problems.append(f"{doc} has no '{TOS_SECTION[3:]}' section ({venue})")
            continue
        for marker in UNCLEARED_MARKERS:
            if marker in section:
                problems.append(
                    f"WATCH_VENUES lists {venue}, whose terms position is '{marker}' "
                    f"({doc}); keep it out of prod until it is cleared"
                )
    return problems


def main() -> int:
    s = get_settings()
    problems: list[str] = []
    problems += security_problems(s)
    problems += watch_venue_problems(s)

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
                installed = set(
                    conn.execute(
                        text("SELECT tgname FROM pg_trigger WHERE tgname LIKE '%_append_only'")
                    ).scalars()
                )
                missing = sorted(EXPECTED_APPEND_ONLY_TRIGGERS - installed)
                if missing:
                    problems.append(f"append-only triggers missing: {', '.join(missing)}")
                problems.extend(demo_problems(conn))
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
