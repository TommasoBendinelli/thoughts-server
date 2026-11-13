#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

BACKUP_ROOT=/srv/thoughts/backups
STAMP=$(date --utc +%Y%m%dT%H%M%SZ)
DATABASE_DUMP=${BACKUP_ROOT}/thoughts-${STAMP}.dump

install -d -m 0700 "$BACKUP_ROOT"
pg_dump --format=custom --file="$DATABASE_DUMP" thoughts
sha256sum "$DATABASE_DUMP" >"${DATABASE_DUMP}.sha256"

# Further Libraries (THOUGHTS_LIBRARIES_FILE): "<id> <database> <root>" per line.
library_roots=()
if [[ -n ${THOUGHTS_LIBRARIES_FILE:-} && -f ${THOUGHTS_LIBRARIES_FILE} ]]; then
  while read -r library database root; do
    [[ -n ${library} ]] || continue
    dump=${BACKUP_ROOT}/thoughts-lib-${library}-${STAMP}.dump
    pg_dump --format=custom --file="$dump" "$database"
    sha256sum "$dump" >"${dump}.sha256"
    library_roots+=("${root}/objects" "${root}/worktree")
  done < <(/srv/thoughts/venv/bin/python -c '
import json, sys
from sqlalchemy.engine import make_url
for entry in json.load(open(sys.argv[1]))["libraries"]:
    if entry.get("enabled", True):
        print(entry["id"], make_url(entry["database_url"]).database, entry["root"])
' "$THOUGHTS_LIBRARIES_FILE")
fi

find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'thoughts-*.dump' -mtime +14 -delete
find "$BACKUP_ROOT" -maxdepth 1 -type f -name 'thoughts-*.dump.sha256' -mtime +14 -delete

if [[ -n ${RESTIC_REPOSITORY:-} ]]; then
  history_paths=()
  if [[ -d /srv/thoughts/state/source-history.git ]]; then
    history_paths+=(/srv/thoughts/state/source-history.git)
    exec {history_lock}>/srv/thoughts/state/.source-history.git.lock
    flock "$history_lock"
  fi
  # Copies of server.env that earlier deploys left in the backup folder hold
  # credentials and are not uploaded (deploys now keep them in /etc/thoughts).
  restic backup "${history_paths[@]}" \
    --exclude='/srv/thoughts/backups/pre-deploy-env-*' \
    /srv/thoughts/backups \
    /srv/thoughts/objects \
    /srv/thoughts/worktree \
    "${library_roots[@]}"
fi
