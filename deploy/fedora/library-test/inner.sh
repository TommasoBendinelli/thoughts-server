#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Runs inside the container started by run.sh, as root.  /src is the checkout.
# A fake production server is installed from the checkout; create-library.sh
# then adds the Library "sam" to it, and every check below talks to the one
# API on port 8080 with each Library's own tokens.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
cd /

step() { printf '\n### %s\n' "$*"; }
pass() { printf 'PASS: %s\n' "$*"; }
fail() {
  printf 'FAIL: %s\n' "$*" >&2
  for log in /run/stub-systemd/*.log; do
    [[ -f ${log} ]] && printf -- '--- %s\n%s\n' "${log}" "$(tail -n 40 "${log}")" >&2
  done
  exit 1
}
health() { curl --fail --silent --max-time 5 "http://127.0.0.1:8080/v1/health" >/dev/null; }
wait_health() {
  for _ in {1..90}; do
    health && return 0
    sleep 1
  done
  fail "no API on port 8080"
}
status() { # TOKEN PATH -> HTTP status
  curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $1" "http://127.0.0.1:8080$2"
}
projects() { # TOKEN -> project names
  curl --fail --silent --max-time 10 -H "Authorization: Bearer $1" \
    "http://127.0.0.1:8080/v1/library/projects" |
    python3 -c 'import json, sys; print(sorted(p.get("name", "") for p in json.load(sys.stdin).get("projects", [])))'
}
create_project() { # TOKEN NAME
  curl --fail --silent --max-time 30 -H "Authorization: Bearer $1" -H 'Content-Type: application/json' \
    --data "{\"action\":\"create\",\"project_id\":\"new:$2\",\"operation_id\":\"$(cat /proc/sys/kernel/random/uuid)\",\"name\":\"$2\"}" \
    "http://127.0.0.1:8080/v1/library/projects" >/dev/null
}
upload_object() { # TOKEN FILE -> sha256
  local sha
  sha=$(sha256sum "$2" | cut -d' ' -f1)
  curl --fail --silent -H "Authorization: Bearer $1" -H "X-Content-SHA256: ${sha}" \
    --data-binary @"$2" "http://127.0.0.1:8080/v1/objects" >/dev/null
  echo "${sha}"
}
token_of() { # FILE DEVICE
  python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["devices"][sys.argv[2]])' "$1" "$2"
}
host_snapshot() { # everything a report-only run could touch
  find /etc /srv /root /usr/local /var/lib/pgsql/data/global -xdev \
    -path /etc/ld.so.cache -prune -o -path /srv/thoughts/state -prune -o \
    -printf '%p %s %T@ %u %g %m\n' 2>/dev/null | sort
  runuser -u postgres -- psql -XAtc 'SELECT rolname FROM pg_roles ORDER BY 1' -c 'SELECT datname FROM pg_database ORDER BY 1'
}
as_prod() {
  runuser -u thoughts -- bash -c 'set -a; . /etc/thoughts/server.env; set +a; cd /srv/thoughts/app; "$@"' -- "$@"
}

install -m 0755 /src/deploy/fedora/library-test/systemctl /usr/local/sbin/systemctl

step "PostgreSQL $(rpm -q --qf '%{version}' postgresql-server), Python $(python3 -c 'import platform; print(platform.python_version())')"
install -d -o postgres -g postgres -m 0755 /run/postgresql
runuser -u postgres -- initdb -D /var/lib/pgsql/data --encoding=UTF8 --locale=C.UTF-8 --auth-local=peer --auth-host=scram-sha-256 >/dev/null
runuser -u postgres -- pg_ctl -D /var/lib/pgsql/data -l /var/lib/pgsql/server.log -w start >/dev/null ||
  { cat /var/lib/pgsql/server.log; fail "PostgreSQL did not start"; }

step "Fake production server from this checkout"
useradd --system --home-dir /srv/thoughts --shell /usr/sbin/nologin thoughts
install -d -o thoughts -g thoughts -m 0750 /srv/thoughts /srv/thoughts/app /srv/thoughts/backups \
  /srv/thoughts/bin /srv/thoughts/objects /srv/thoughts/state /srv/thoughts/state/source-bundle /srv/thoughts/worktree
tar -C /src --exclude=./.git --exclude=./.venv --exclude='*/__pycache__' -cf - . | tar -C /srv/thoughts/app -xf -
# As install-native.sh leaves them: the code belongs to root and the services only read it.
chown -R -h root:thoughts /srv/thoughts/app
chmod -R u=rwX,go=rX /srv/thoughts/app
runuser -u thoughts -- python3 -m venv /srv/thoughts/venv
runuser -u thoughts -- /srv/thoughts/venv/bin/pip install --quiet --disable-pip-version-check /srv/thoughts/app
chown -R -h root:thoughts /srv/thoughts/venv
chmod -R u=rwX,go=rX /srv/thoughts/venv
runuser -u thoughts -- bash -c \
  '[[ ! -w /srv/thoughts/venv/bin && ! -w /srv/thoughts/venv/pyvenv.cfg && ! -w /srv/thoughts/app/alembic.ini ]]' ||
  fail "the services can change their own code"
printf '#!/bin/sh\nexit 0\n' >/srv/thoughts/bin/thoughts-comment-sync
chmod 0755 /srv/thoughts/bin/thoughts-comment-sync
install -d -o root -g thoughts -m 0750 /etc/thoughts
{
  cat /src/deploy/fedora/server.env.example
  echo 'OPENAI_API_KEY=sk-production-secret-never-printed'
} >/tmp/prod.env
install -o root -g thoughts -m 0640 /tmp/prod.env /etc/thoughts/server.env
runuser -u postgres -- createuser thoughts
runuser -u postgres -- createdb --owner=thoughts thoughts
install -m 0644 /src/deploy/fedora/systemd/*.service /src/deploy/fedora/systemd/*.timer /etc/systemd/system/
install -m 0755 /src/deploy/fedora/backup.sh /usr/local/sbin/thoughts-backup
as_prod /srv/thoughts/venv/bin/alembic -c alembic.ini upgrade head >/dev/null
# Production was migrated to the "default" folder segment on 2026-10-18.
as_prod /srv/thoughts/venv/bin/thoughts-admin record-default-folder-segment >/dev/null
systemctl start thoughts-api.service thoughts-worker.service thoughts-export-worker.service \
  thoughts-anchor-worker.service thoughts-review-worker.service
wait_health
PROD_TOKEN=$(as_prod /srv/thoughts/venv/bin/thoughts-admin create-device thoughts-mac |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')
create_project "${PROD_TOKEN}" ProductionOnly
printf 'production object\n' >/tmp/prod-object
PROD_OBJECT=$(upload_object "${PROD_TOKEN}" /tmp/prod-object)
[[ $(projects "${PROD_TOKEN}") == "['ProductionOnly']" ]] || fail "production project"
pass "production serves one Library with a device, a project and an object"

step "Report only: nothing changes"
host_snapshot >/tmp/host.before
/src/deploy/fedora/create-library.sh sam | tee /tmp/report.out
host_snapshot >/tmp/host.after
diff /tmp/host.before /tmp/host.after || fail "the report-only run changed the host"
grep -q 'never-printed' /tmp/report.out && fail "the report printed a secret"
pass "report-only run changed no file, role or database"

step "Create Library sam"
/src/deploy/fedora/create-library.sh sam --apply | tee /tmp/apply.out
wait_health
TOKENS=/root/thoughts-library-sam-devices.json
[[ $(stat -c '%U:%G %a' "${TOKENS}") == "root:root 600" ]] || fail "token file permissions"
[[ $(stat -c '%U:%G %a' /etc/thoughts/libraries.json) == "root:thoughts 640" ]] || fail "registry permissions"
[[ $(stat -c '%U:%G %a' /srv/thoughts/libraries/sam) == "thoughts:thoughts 700" ]] || fail "Library root permissions"
grep -qx 'THOUGHTS_LIBRARIES_FILE=/etc/thoughts/libraries.json' /etc/thoughts/server.env || fail "server.env"
grep -q 'never-printed' /tmp/apply.out && fail "the apply run printed a secret"
python3 - "${TOKENS}" <<'PY' || fail "token file content"
import json, sys
record = json.load(open(sys.argv[1]))
assert record["server_url"] == "https://thoughts.example.org"
assert sorted(record["devices"]) == ["sam-android", "sam-windows", "sam-windows-source-sync"]
assert all(len(token) >= 32 for token in record["devices"].values())
PY
grep -q '^sam' <<<"$(as_prod env THOUGHTS_LIBRARIES_FILE=/etc/thoughts/libraries.json /srv/thoughts/venv/bin/thoughts-admin list-libraries)" ||
  fail "list-libraries"
pass "database, files, registry, server.env and three devices created; tokens root-only"

step "One address, separate Libraries"
SAM_TOKEN=$(token_of "${TOKENS}" sam-windows)
PHONE_TOKEN=$(token_of "${TOKENS}" sam-android)
[[ $(projects "${SAM_TOKEN}") == "[]" ]] || fail "sam sees production's projects"
create_project "${SAM_TOKEN}" SamOnly
[[ $(projects "${PHONE_TOKEN}") == "['SamOnly']" ]] || fail "sam's phone does not see their project"
[[ $(projects "${PROD_TOKEN}") == "['ProductionOnly']" ]] || fail "production sees sam's project"
[[ $(status "${SAM_TOKEN}" "/v1/objects/${PROD_OBJECT}") == 404 ]] || fail "sam can read production's object"
printf 'sam object\n' >/tmp/sam-object
SAM_OBJECT=$(upload_object "${SAM_TOKEN}" /tmp/sam-object)
[[ $(status "${PROD_TOKEN}" "/v1/objects/${SAM_OBJECT}") == 404 ]] || fail "production can read sam's object"
[[ $(status "${SAM_TOKEN}" "/v1/objects/${SAM_OBJECT}") == 200 ]] || fail "sam cannot read their object"
[[ -f /srv/thoughts/libraries/sam/objects/${SAM_OBJECT:0:2}/${SAM_OBJECT} ]] ||
  find /srv/thoughts/libraries/sam/objects -name "${SAM_OBJECT}*" | grep -q . || fail "object not in sam's store"
find /srv/thoughts/objects -name "${SAM_OBJECT}*" | grep -q . && fail "sam's object landed in production's store"
[[ $(status "$(printf 'x%.0s' {1..40})" /v1/library/projects) == 401 ]] || fail "unknown token"
[[ $(status "" /v1/library/projects) == 401 ]] || fail "no token"
pass "each token reaches only its own Library (projects, objects, files); unknown tokens get 401"

step "Workers: one per Library"
for worker in thoughts-worker thoughts-export-worker thoughts-anchor-worker thoughts-review-worker; do
  libraries=$(for pid in $(pgrep -f "/srv/thoughts/venv/bin/${worker}\$"); do
    # The workers run as thoughts; a rootless container's root may not read their environment.
    runuser -u thoughts -- cat /proc/${pid}/environ | tr '\0' '\n' | sed -n 's/^THOUGHTS_DEFAULT_LIBRARY_ID=//p'
  done | sort | tr '\n' ' ') || fail "${worker}: cannot read the workers' environment"
  [[ ${libraries} == "default sam " ]] || fail "${worker}: children for [${libraries}]"
done
pass "every worker service runs one child for each Library, each with its own environment"

step "Running again changes nothing"
/src/deploy/fedora/create-library.sh sam --apply >/tmp/again.out
wait_health
grep -q 'sam-windows: exists' /tmp/again.out || fail "rerun recreated devices"
[[ $(projects "${SAM_TOKEN}") == "['SamOnly']" && $(projects "${PROD_TOKEN}") == "['ProductionOnly']" ]] ||
  fail "rerun lost data"
pass "rerun keeps devices and data"

step "Restart"
systemctl restart thoughts-api.service
wait_health
[[ $(projects "${SAM_TOKEN}") == "['SamOnly']" && $(projects "${PROD_TOKEN}") == "['ProductionOnly']" ]] ||
  fail "after restart"
pass "both Libraries served after an API restart (migrations of every Library ran first)"

step "A device created while running works without restart"
LATE=$(as_prod env THOUGHTS_LIBRARIES_FILE=/etc/thoughts/libraries.json \
  /srv/thoughts/venv/bin/thoughts-admin --library sam create-device sam-laptop |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["token"])')
sleep 6
[[ $(projects "${LATE}") == "['SamOnly']" ]] || fail "new device not routed"
pass "new device routed within the directory's refresh interval"

step "Backups include every Library"
runuser -u thoughts -- bash -c 'set -a; . /etc/thoughts/server.env; set +a; /usr/local/sbin/thoughts-backup'
ls /srv/thoughts/backups/thoughts-lib-sam-*.dump >/dev/null || fail "no dump of sam's database"
ls /srv/thoughts/backups/thoughts-2*.dump >/dev/null || fail "no dump of production's database"
pass "backup.sh dumps production's and sam's databases"

printf '\nALL PASSED\n'
