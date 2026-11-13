#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this restore drill as root" >&2
  exit 1
fi

if [[ $# -ne 1 || ! -f $1 ]]; then
  echo "Usage: $0 /srv/thoughts/backups/thoughts-<timestamp>.dump" >&2
  exit 2
fi

DUMP=$(realpath "$1")
RESTORE_DATABASE="thoughts_restore_check_$$"
if [[ ! ${RESTORE_DATABASE} =~ ^thoughts_restore_check_[0-9]+$ ]]; then
  echo "Internal restore database name is unsafe" >&2
  exit 1
fi

cleanup() {
  runuser -u postgres -- dropdb --if-exists "$RESTORE_DATABASE"
}
trap cleanup EXIT

source /etc/thoughts/server.env
: "${THOUGHTS_OBJECT_ROOT:?THOUGHTS_OBJECT_ROOT must be configured}"

runuser -u postgres -- createdb --owner=thoughts "$RESTORE_DATABASE"
runuser -u thoughts -- pg_restore \
  --exit-on-error \
  --no-owner \
  --dbname="$RESTORE_DATABASE" \
  "$DUMP"

runuser -u thoughts -- env \
  THOUGHTS_DATABASE_URL="postgresql+psycopg:///${RESTORE_DATABASE}" \
  THOUGHTS_OBJECT_ROOT="$THOUGHTS_OBJECT_ROOT" \
  /srv/thoughts/venv/bin/thoughts-admin verify-objects

runuser -u thoughts -- psql --dbname="$RESTORE_DATABASE" --tuples-only --command \
  "SELECT 'operations=' || count(*) FROM operations UNION ALL SELECT 'objects=' || count(*) FROM objects UNION ALL SELECT 'publications=' || count(*) FROM publications;"

echo "Restore drill completed successfully; the temporary database will now be dropped."
