"""Re-wrap every live venue credential under the current key version.

Rotation runbook (docs/security.md "Credential storage"):
  local wrapper: VAULT_KEY="<new>,<old>"  -> run this -> VAULT_KEY="<new>"
  AWS KMS:       KMS_KEY_ID=<new key>     -> run this -> disable the old key
                 (KMS automatic annual rotation needs no re-wrap at all)

Run:  uv run python scripts/rotate_credentials.py [--batch-size 100]
Safe to re-run; safe while the app and worker serve traffic.
"""

import argparse
import logging
import sys

from alphamarket import audit, credentials, redaction, vault
from alphamarket.db import SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    redaction.install()
    target = vault.current_version()
    with SessionLocal() as db, audit.job_context("key_rotate") as job_id:
        n = credentials.rotate_all(db, batch_size=args.batch_size)
    print(f"rotated {n} credential(s) to {target} (job {job_id})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
