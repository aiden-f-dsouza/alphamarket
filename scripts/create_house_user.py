"""Create the user that owns Koine-run ("Run by Koine") strategies.

Run:  uv run python scripts/create_house_user.py --email ops@koine.example \\
          --display-name "Koine" [--password-env HOUSE_PASSWORD]

A house user (users.is_house) is a normal user. This script is NOT a shortcut
around verification: the account starts with an unverified email and no TOTP,
and connecting a venue still needs a verified email, TOTP and a fresh step-up
(invariant 11). Only read-only keys are ever accepted (invariants 4 and 8);
the agent that trades holds its own trading key outside Koine's platform.

The password comes from the environment variable named by --password-env, or
is prompted for (never a command-line argument, so it stays out of shell
history and the process list). Refuses if the email is already registered.
"""

import argparse
import getpass
import os
import sys

from sqlalchemy import select
from sqlalchemy.orm import Session

from alphamarket import audit
from alphamarket.auth import MAX_PASSWORD_BYTES, hash_password
from alphamarket.db import SessionLocal
from alphamarket.models import User

MIN_PASSWORD_CHARS = 10  # the same floor as POST /auth/signup

NEXT_STEPS = """\
House user created (id {id}, {email}). Remaining steps, all through the app:
  1. Log in at /login and follow the emailed verification link.
  2. Enroll TOTP in account settings, then do a fresh step-up.
  3. Create a READ-ONLY key on the venue (the trading key stays with the agent,
     outside Koine) and connect it at /dashboard/connect.
  4. After the first sync, list the strategy from the dashboard. It shows the
     "Run by Koine" badge, ranks like any other strategy and incubates 90 days.
"""


def create_house_user(db: Session, email: str, display_name: str, password: str) -> User:
    """Add the house user. Raises ValueError for a bad input or an existing email."""
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("not an email address")
    if len(display_name.strip()) < 2:
        raise ValueError("display name needs at least 2 characters")
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"password needs at least {MIN_PASSWORD_CHARS} characters")
    if len(password.encode()) > MAX_PASSWORD_BYTES:
        raise ValueError(f"password is longer than {MAX_PASSWORD_BYTES} bytes")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise ValueError(f"{email} is already registered")
    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name.strip(),
        is_house=True,
    )
    db.add(user)
    db.flush()
    audit.record(db, "house.user_created", target_type="user", target_id=user.id)
    db.commit()
    return user


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", required=True)
    parser.add_argument("--display-name", default="Koine")
    parser.add_argument(
        "--password-env", help="read the password from this environment variable (else prompt)"
    )
    args = parser.parse_args()
    if args.password_env:
        password = os.environ.get(args.password_env, "")
    else:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Repeat: ") != password:
            print("passwords differ", file=sys.stderr)
            return 1
    with SessionLocal() as db, audit.job_context("house_user"):
        try:
            user = create_house_user(db, args.email, args.display_name, password)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
        print(NEXT_STEPS.format(id=user.id, email=user.email))
    return 0


if __name__ == "__main__":
    sys.exit(main())
