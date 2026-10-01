#!/usr/bin/env bash
# Restore drill (implementation_plan.md P3 item 11): prove a scripts/backup_db.sh
# output actually restores, on a schedule -- not the first time you find out is
# during a real incident.
#
# Decrypts one backup with an age identity file, pg_restores it into a
# throwaway scratch database on the same server as RESTORE_ADMIN_URL,
# sanity-checks row counts on the append-only ledger tables (fills,
# settlements, signals), then drops the scratch database. Exits non-zero on
# any failure, including "restored fine but a table came back empty" --
# whatever runs this on a schedule should alert on a non-zero exit.
#
# Env:
#   RESTORE_ADMIN_URL    - admin postgres URL (a role that can CREATE/DROP
#                          DATABASE) on the server to restore onto (required)
#   BACKUP_AGE_IDENTITY  - path to the age identity (private key) file (required)
#   ALLOW_EMPTY_RESTORE  - set to 1 to treat a 0-row table as a warning
#                          instead of a failure (e.g. a fresh, dataless env)
#
# Usage:
#   scripts/restore_drill.sh <backup-file.dump.age>
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: restore_drill.sh <backup-file.dump.age>" >&2
  exit 1
fi
backup_file="$1"

if [[ -z "${RESTORE_ADMIN_URL:-}" ]]; then
  echo "restore_drill.sh: RESTORE_ADMIN_URL is not set" >&2
  exit 1
fi
if [[ -z "${BACKUP_AGE_IDENTITY:-}" ]]; then
  echo "restore_drill.sh: BACKUP_AGE_IDENTITY is not set" >&2
  exit 1
fi
if [[ ! -f "$backup_file" ]]; then
  echo "restore_drill.sh: no such file: ${backup_file}" >&2
  exit 1
fi
if [[ ! -f "$BACKUP_AGE_IDENTITY" ]]; then
  echo "restore_drill.sh: identity file not found: ${BACKUP_AGE_IDENTITY}" >&2
  exit 1
fi

for cmd in age pg_restore psql; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "restore_drill.sh: ${cmd} not found on PATH" >&2
    exit 1
  fi
done

admin_url="${RESTORE_ADMIN_URL%/}"
base_url="${admin_url%/*}"
scratch_db="koine_restore_drill_$(date -u +%Y%m%d%H%M%S)_$$"
scratch_url="${base_url}/${scratch_db}"
tmp_dump="$(mktemp)"

cleanup() {
  rm -f "$tmp_dump"
  psql "$admin_url" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"${scratch_db}\" WITH (FORCE)" \
    >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "decrypting ${backup_file}..."
age --decrypt --identity "$BACKUP_AGE_IDENTITY" -o "$tmp_dump" "$backup_file"

echo "creating scratch database ${scratch_db}..."
psql "$admin_url" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"${scratch_db}\""

echo "restoring into ${scratch_db}..."
pg_restore --no-owner --no-privileges --dbname "$scratch_url" "$tmp_dump"

fail=0
for table in fills settlements signals; do
  count="$(psql "$scratch_url" -tAc "SELECT count(*) FROM ${table}")"
  echo "  ${table}: ${count} rows"
  if [[ "$count" -lt 1 ]]; then
    if [[ "${ALLOW_EMPTY_RESTORE:-0}" == "1" ]]; then
      echo "  warning: ${table} has 0 rows after restore (ALLOW_EMPTY_RESTORE=1)" >&2
    else
      echo "restore_drill.sh: ${table} has 0 rows after restore -- failing the drill" >&2
      fail=1
    fi
  fi
done

if [[ "$fail" -ne 0 ]]; then
  exit 1
fi

echo "restore drill OK: ${backup_file}"
