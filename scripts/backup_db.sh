#!/usr/bin/env bash
# Encrypted nightly database backup (implementation_plan.md P3 item 11).
#
# pg_dump --format=custom, piped straight through `age -r "$BACKUP_AGE_RECIPIENT"`
# so plaintext never touches disk. Fails CLOSED: with no recipient configured
# it refuses to run rather than writing an unencrypted dump.
#
# Env:
#   DATABASE_URL          - postgres connection string to back up (required)
#   BACKUP_AGE_RECIPIENT  - age public key (age1...) to encrypt to (required)
#   BACKUP_DIR            - where to write the .dump.age file (default: ./backups)
#
# Usage:
#   scripts/backup_db.sh [label]
#
# See scripts/restore_drill.sh to verify a file this script wrote actually
# restores; run that regularly, not only during a real incident.
set -euo pipefail

if [[ -z "${BACKUP_AGE_RECIPIENT:-}" ]]; then
  echo "backup_db.sh: BACKUP_AGE_RECIPIENT is not set; refusing to write an unencrypted backup" >&2
  exit 1
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "backup_db.sh: DATABASE_URL is not set" >&2
  exit 1
fi

for cmd in pg_dump age; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "backup_db.sh: ${cmd} not found on PATH" >&2
    exit 1
  fi
done

backup_dir="${BACKUP_DIR:-./backups}"
mkdir -p "$backup_dir"

label="${1:-nightly}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out_file="${backup_dir}/koine_${label}_${timestamp}.dump.age"

echo "backing up to ${out_file}..."
pg_dump --format=custom --no-owner --no-privileges --dbname "$DATABASE_URL" \
  | age -r "$BACKUP_AGE_RECIPIENT" -o "$out_file"

echo "wrote ${out_file}"
