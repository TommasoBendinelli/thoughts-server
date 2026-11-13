#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Add a Library to the production server: its own database and files, served
# by the same processes at the same address. Its devices' tokens decide that
# their requests reach it (thoughts_server.multi_library).
#
#   sudo ./deploy/fedora/create-library.sh NAME            # report only
#   sudo ./deploy/fedora/create-library.sh NAME --apply    # create (or complete) it
#
# Creates, when missing: PostgreSQL database thoughts_lib_<name> owned by the
# thoughts role, /srv/thoughts/libraries/<name>/{objects,state,worktree}, the
# registry entry in /etc/thoughts/libraries.json, THOUGHTS_LIBRARIES_FILE in
# /etc/thoughts/server.env, the schema, the "default" folder segment, and the
# devices <name>-windows, <name>-windows-source-sync and <name>-android, whose
# tokens go to /root/thoughts-library-<name>-devices.json (root only, never
# printed). Then restarts the API and workers so they serve the new Library.
# Running it again completes a partial run and never touches existing data.
set -euo pipefail

NAME=${1:-}
APPLY=false
[[ ${2:-} == --apply ]] && APPLY=true
if [[ ! ${NAME} =~ ^[a-z][a-z0-9-]{1,31}$ || ${NAME} == default ]]; then
  echo "usage: $0 NAME [--apply]   (NAME: 2-32 lowercase letters, digits or '-', not 'default')" >&2
  exit 2
fi
if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (sudo)." >&2
  exit 2
fi

PROD_ROOT=/srv/thoughts
ENV_FILE=/etc/thoughts/server.env
REGISTRY=/etc/thoughts/libraries.json
LIBRARIES_ROOT=${PROD_ROOT}/libraries
ROOT=${LIBRARIES_ROOT}/${NAME}
DATABASE=thoughts_lib_${NAME//-/_}
DATABASE_URL=postgresql+psycopg:///${DATABASE}
TOKEN_FILE=/root/thoughts-library-${NAME}-devices.json
DEVICES=("${NAME}-windows" "${NAME}-windows-source-sync" "${NAME}-android")
SERVICES=(thoughts-api thoughts-worker thoughts-export-worker thoughts-anchor-worker thoughts-review-worker)
PYTHON=${PROD_ROOT}/venv/bin/python
ADMIN=${PROD_ROOT}/venv/bin/thoughts-admin
# Root never runs the server's own interpreter or code: the small JSON edits below
# use the system Python in isolated mode, everything else runs as the thoughts account.
SYSTEM_PYTHON=/usr/bin/python3

# Run a command as the thoughts account with production's environment.
as_thoughts() {
  (
    set -a
    # shellcheck disable=SC1090
    . "${ENV_FILE}"
    set +a
    export THOUGHTS_LIBRARIES_FILE=${REGISTRY} HOME=${PROD_ROOT}/state
    cd "${PROD_ROOT}/app"
    runuser -u thoughts -p -- "$@"
  )
}

psql_value() {
  runuser -u postgres -- psql -X -qAt -d postgres -c "$1"
}

echo "== Checks"
for path in "${PYTHON}" "${ADMIN}" "${ENV_FILE}" "${PROD_ROOT}/app/alembic.ini" "${SYSTEM_PYTHON}"; do
  [[ -e ${path} ]] || { echo "missing: ${path}" >&2; exit 1; }
done
if ! runuser -u thoughts -- "${PYTHON}" -P -c 'import thoughts_server.libraries, thoughts_server.multi_library' 2>/dev/null; then
  echo "The installed server has no Library support yet; deploy it first." >&2
  exit 1
fi
for service in "${SERVICES[@]}"; do
  unit=/etc/systemd/system/${service}.service
  if [[ -f ${unit} ]] && ! grep -q -- '/srv/thoughts/libraries' "${unit}"; then
    echo "${unit} cannot write ${LIBRARIES_ROOT}; deploy the server with Library support first." >&2
    exit 1
  fi
done
if ! curl -fsS --max-time 10 http://127.0.0.1:8080/v1/health >/dev/null; then
  echo "Production is not healthy; not changing anything." >&2
  exit 1
fi

database_exists=$(psql_value "SELECT 1 FROM pg_database WHERE datname = '${DATABASE}'")
registered=$("${SYSTEM_PYTHON}" -I -c '
import json, os, sys
path, name = sys.argv[1:3]
entries = json.load(open(path))["libraries"] if os.path.exists(path) else []
print("yes" if any(entry["id"] == name for entry in entries) else "no")' "${REGISTRY}" "${NAME}")
env_has_registry=$(grep -c '^THOUGHTS_LIBRARIES_FILE=' "${ENV_FILE}" || true)

echo "Library:   ${NAME}"
echo "Database:  ${DATABASE} ($([[ ${database_exists} == 1 ]] && echo exists || echo 'to create'))"
echo "Files:     ${ROOT} ($([[ -d ${ROOT} ]] && echo exist || echo 'to create'))"
echo "Registry:  ${REGISTRY} ($([[ ${registered} == yes ]] && echo 'lists it' || echo 'to add'))"
echo "server.env: THOUGHTS_LIBRARIES_FILE $([[ ${env_has_registry} -gt 0 ]] && echo set || echo 'to add')"
echo "Devices:   ${DEVICES[*]} (tokens to ${TOKEN_FILE})"
echo "Restart:   ${SERVICES[*]} (a few seconds of downtime for every Library)"
if [[ ${APPLY} != true ]]; then
  echo
  echo "Report only. Run again with --apply to make these changes."
  exit 0
fi

echo "== Database"
if [[ ${database_exists} != 1 ]]; then
  runuser -u postgres -- createdb --owner=thoughts "${DATABASE}"
  psql_value "REVOKE CONNECT ON DATABASE ${DATABASE} FROM PUBLIC" >/dev/null
  echo "created ${DATABASE}"
fi

echo "== Files"
install -d -o thoughts -g thoughts -m 0700 "${LIBRARIES_ROOT}" "${ROOT}" \
  "${ROOT}/objects" "${ROOT}/state" "${ROOT}/worktree"
command -v restorecon >/dev/null && restorecon -R "${LIBRARIES_ROOT}" || true

echo "== Registry"
umask 027
"${SYSTEM_PYTHON}" -I - "${REGISTRY}" "${NAME}" "${ROOT}" "${DATABASE_URL}" <<'PY'
import json, os, sys
path, name, root, url = sys.argv[1:5]
document = json.load(open(path)) if os.path.exists(path) else {"libraries": []}
if not any(entry["id"] == name for entry in document["libraries"]):
    document["libraries"].append({"id": name, "name": name, "root": root, "database_url": url})
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(document, output, indent=2)
        output.write("\n")
    os.replace(temporary, path)
    print("added", name)
PY
chown root:thoughts "${REGISTRY}"
chmod 0640 "${REGISTRY}"
command -v restorecon >/dev/null && restorecon "${REGISTRY}" || true
if [[ ${env_has_registry} -eq 0 ]]; then
  cp -p "${ENV_FILE}" "${ENV_FILE}.bak-$(date --utc +%Y%m%dT%H%M%SZ)"
  printf '\n# Further Libraries served by this server (deploy/fedora/create-library.sh).\nTHOUGHTS_LIBRARIES_FILE=%s\n' \
    "${REGISTRY}" >>"${ENV_FILE}"
  echo "THOUGHTS_LIBRARIES_FILE added to ${ENV_FILE}"
fi

echo "== Schema"
as_thoughts "${ADMIN}" upgrade-library-databases --alembic-config "${PROD_ROOT}/app/alembic.ini"
# A new, empty Library starts with the "default" folder segment, like
# production since its 2026-10-18 migration (every Library must agree).
as_thoughts "${ADMIN}" --library "${NAME}" record-default-folder-segment

echo "== Devices"
# Every Library shares the server's public address (THOUGHTS_PUBLIC_BASE_URL).
SERVER_URL=$(as_thoughts "${PYTHON}" -P -c '
import os
from thoughts_server.config import DEFAULT_PUBLIC_BASE_URL, normalize_public_base_url
print(normalize_public_base_url(os.environ.get("THOUGHTS_PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL)))')
created=""
known=$(as_thoughts "${ADMIN}" --library "${NAME}" list-devices || true)
for device in "${DEVICES[@]}"; do
  if grep -q "\"id\": *\"${device}\"" <<<"${known}"; then
    echo "${device}: exists"
    continue
  fi
  created+=$(as_thoughts "${ADMIN}" --library "${NAME}" create-device "${device}" --name "${device}")$'\n'
  echo "${device}: created"
done
if [[ -n ${created} ]]; then
  printf '%s' "${created}" | (umask 077 && "${SYSTEM_PYTHON}" -I -c '
import datetime, json, os, sys
path, name, server_url = sys.argv[1:4]
record = {"library": name, "server_url": server_url, "devices": {}}
if os.path.exists(path):
    record = json.load(open(path, encoding="utf-8"))
for line in sys.stdin:
    if line.strip():
        device = json.loads(line)
        record["devices"][device["device_id"]] = device["token"]
record["updated_at"] = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
temporary = path + ".tmp"
with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
    json.dump(record, output, indent=2, sort_keys=True)
    output.write("\n")
os.replace(temporary, path)
' "${TOKEN_FILE}" "${NAME}" "${SERVER_URL}")
  chown root:root "${TOKEN_FILE}"
  chmod 0600 "${TOKEN_FILE}"
  echo "New tokens were written to ${TOKEN_FILE} (root only)."
fi

echo "== Restart"
systemctl restart "${SERVICES[@]/%/.service}"
for _ in $(seq 1 60); do
  curl -fsS --max-time 5 http://127.0.0.1:8080/v1/health >/dev/null && break
  sleep 2
done
curl -fsS --max-time 5 http://127.0.0.1:8080/v1/health >/dev/null || { echo "The API did not come back" >&2; exit 1; }
as_thoughts "${ADMIN}" list-libraries
echo
echo "Library ${NAME} is served at ${SERVER_URL}; its devices' tokens are in ${TOKEN_FILE}."
