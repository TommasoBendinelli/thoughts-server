#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

SOURCE=""
DESKTOP_SOURCE=""
APPLY=false
START_SERVICES=true
while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE=${2:?--source requires a path}
      shift 2
      ;;
    --desktop-source)
      DESKTOP_SOURCE=${2:?--desktop-source requires a path}
      shift 2
      ;;
    --apply)
      APPLY=true
      shift
      ;;
    --no-start)
      START_SERVICES=false
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ -z ${SOURCE} || ! -f ${SOURCE}/pyproject.toml ]]; then
  echo "--source must name a checked-out thoughts-server repository" >&2
  exit 2
fi
SOURCE=$(realpath "$SOURCE")
if [[ -z ${DESKTOP_SOURCE} || ! -f ${DESKTOP_SOURCE}/tools/thoughts-comment-sync.cpp ]]; then
  echo "--desktop-source must name a checked-out thoughts-desktop repository" >&2
  exit 2
fi
DESKTOP_SOURCE=$(realpath "$DESKTOP_SOURCE")
# Root runs the scripts and installs the unit files of these two copies. They must
# not be somewhere the service account can write: never under /srv/thoughts, and
# never owned by that account.
for staged_source in "${SOURCE}" "${DESKTOP_SOURCE}"; do
  if [[ ${staged_source} == /srv/thoughts || ${staged_source} == /srv/thoughts/* ]]; then
    echo "Deployment sources must not be under /srv/thoughts: ${staged_source}" >&2
    exit 2
  fi
  if [[ $(stat -c %U -- "${staged_source}") == thoughts ]]; then
    echo "Deployment sources must not belong to the thoughts account: ${staged_source}" >&2
    exit 2
  fi
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root" >&2
  exit 1
fi

source /etc/os-release
if [[ (${ID:-} != "fedora" && ${ID:-} != "fedora-asahi-remix") || $(uname -m) != "aarch64" ]]; then
  echo "This installer is restricted to Fedora aarch64" >&2
  exit 1
fi

if [[ ${APPLY} != true ]]; then
  echo "Dry description only; no host changes were made."
  echo "Source: $SOURCE"
  echo "Target: /srv/thoughts"
  echo "Desktop helper source: $DESKTOP_SOURCE"
  echo "Packages: PostgreSQL, Python, Node.js, Git, bubblewrap, qpdf, Poppler, Ghostscript, zstd, full TeX Live, CMake, Ninja, GCC C++, libzip, and SyncTeX development files"
  echo "Services: thoughts-api.service, thoughts-worker.service, thoughts-anchor-worker.service, thoughts-review-worker.service, thoughts-export-worker.service"
  echo "Run again with --apply after reviewing the preflight report."
  echo "Use --no-start for a staged cutover that keeps the existing service online."
  exit 0
fi

dnf --disablerepo=tailscale-stable install -y \
  postgresql-server postgresql-contrib \
  python3 python3-devel python3-pypdf nodejs npm git bubblewrap qpdf poppler-utils ghostscript zstd cmake ninja-build gcc-c++ restic \
  latexmk texlive-scheme-full texlive-xurl texlive-acmart texlive-cleveref texlive-hyperxmp \
  pkgconf-pkg-config qt6-qtbase-devel poppler-qt6-devel kf6-karchive-devel \
  texlive-lib-devel libzip-devel zlib-ng-compat-devel

for required_tex_input in dblfloatfix.sty docmute.sty makecell.sty; do
  if ! kpsewhich "${required_tex_input}" >/dev/null 2>&1; then
    echo "Required TeX input is unavailable after installation: ${required_tex_input}" >&2
    exit 1
  fi
done

if ! getent passwd thoughts >/dev/null; then
  useradd --system --home-dir /srv/thoughts --shell /usr/sbin/nologin thoughts
fi

install -d -o thoughts -g thoughts -m 0750 \
  /srv/thoughts \
  /srv/thoughts/backups \
  /srv/thoughts/objects \
  /srv/thoughts/state/review-home \
  /srv/thoughts/state/source-bundle \
  /srv/thoughts/state \
  /srv/thoughts/worktree
# The code the services run (application, virtual environment, helper programs)
# belongs to root: the services' group may read and execute it, never change it.
install -d -o root -g thoughts -m 0750 \
  /srv/thoughts/app \
  /srv/thoughts/bin

if [[ ! -f /var/lib/pgsql/data/PG_VERSION ]]; then
  postgresql-setup --initdb --unit postgresql
fi
systemctl enable --now postgresql

if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='thoughts'" | grep -q 1; then
  runuser -u postgres -- createuser thoughts
fi
if ! runuser -u postgres -- psql -tAc "SELECT 1 FROM pg_database WHERE datname='thoughts'" | grep -q 1; then
  runuser -u postgres -- createdb --owner=thoughts thoughts
fi

deployment_stamp=$(date -u +%Y%m%dT%H%M%SZ)
database_backup="/srv/thoughts/backups/pre-deploy-${deployment_stamp}.dump"
runuser -u postgres -- pg_dump --format=custom thoughts >"${database_backup}"
chown thoughts:thoughts "${database_backup}"
chmod 0600 "${database_backup}"
if [[ -f /srv/thoughts/app/pyproject.toml ]]; then
  application_backup="/srv/thoughts/backups/pre-deploy-app-${deployment_stamp}.tar.gz"
  tar --create --gzip --file="${application_backup}" --directory=/srv/thoughts/app .
  chown thoughts:thoughts "${application_backup}"
  chmod 0600 "${application_backup}"
fi

# Do not let either service import a half-replaced application tree. A failed
# deployment intentionally leaves them stopped for an explicit rollback.
systemctl stop thoughts-source-history.timer thoughts-source-history.service 2>/dev/null || true
systemctl stop thoughts-export-worker.service thoughts-review-worker.service thoughts-anchor-worker.service thoughts-worker.service thoughts-api.service 2>/dev/null || true

# Retire the removed worker during upgrades without deleting stored recordings or jobs.
systemctl disable --now thoughts-transcription-worker.service 2>/dev/null || true
rm -f /etc/systemd/system/thoughts-transcription-worker.service /srv/thoughts/venv/bin/thoughts-transcription-worker
# Thoughts Server never synchronizes Overleaf/Git remotes; retire the old provider worker.
systemctl disable --now thoughts-provider-worker.service 2>/dev/null || true
rm -rf /etc/systemd/system/thoughts-provider-worker.service /etc/systemd/system/thoughts-provider-worker.service.d /srv/thoughts/venv/bin/thoughts-provider-worker
systemctl daemon-reload

find /srv/thoughts/app -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
tar \
  --create \
  --file=- \
  --directory="$SOURCE" \
  --exclude='./.git' \
  --exclude='./.venv' \
  --exclude='./.pytest_cache' \
  --exclude='./.ruff_cache' \
  --exclude='./.coverage' \
  --exclude='._*' \
  --exclude='*/._*' \
  --exclude='*/__pycache__' \
  --exclude='./sdk/kotlin/.gradle' \
  --exclude='./sdk/kotlin/build' \
  --exclude='./sdk/cpp/build' \
  . | tar --extract --file=- --directory=/srv/thoughts/app
chown -R -h root:thoughts /srv/thoughts/app
# Readable like the staged copy was, but writable by root only (a staged copy may
# be group-writable). The directory itself stays closed to other accounts.
chmod -R u=rwX,go=rX /srv/thoughts/app
chmod 0750 /srv/thoughts/app

# Everything root runs or installs from here on comes from the staged source,
# never from /srv/thoughts.
"${SOURCE}/deploy/fedora/build-comment-helper.sh" --source "${DESKTOP_SOURCE}"
"${SOURCE}/deploy/fedora/build-source-artifact-worker.sh" --source "${SOURCE}"

# The service account builds the virtual environment, because installing packages
# can run their build code. It is handed to root below, once it is complete.
install -d -o thoughts -g thoughts -m 0750 /srv/thoughts/venv
chown -R -h thoughts:thoughts /srv/thoughts/venv
runuser -u thoughts -- python3 -m venv /srv/thoughts/venv
find /srv/thoughts/venv/lib /srv/thoughts/venv/lib64 \
  -path '*/site-packages/~thoughts_server*' -prune -exec rm -rf -- {} + 2>/dev/null || true
runuser -u thoughts -- /srv/thoughts/venv/bin/pip install --upgrade pip
runuser -u thoughts -- /srv/thoughts/venv/bin/pip install /srv/thoughts/app
# Markdown math renders through the locked MathJax runtime next to render.mjs.
# pip never installs node_modules, so a rebuilt venv loses it: install it every time.
MARKDOWN_MATH=$(runuser -u thoughts -- /srv/thoughts/venv/bin/python -P -c \
  'import pathlib, thoughts_server; print(pathlib.Path(thoughts_server.__file__).with_name("markdown_math"))')
runuser -u thoughts -- env HOME=/srv/thoughts/state/review-home \
  /usr/bin/npm ci --prefix "${MARKDOWN_MATH}" --omit=dev --no-audit --no-fund
# From here on the services can read and run their code but not change it. The
# files stay readable by others (the test instance copies this environment as
# root and runs the copy under its own account); the directories above them do not.
chown -R -h root:thoughts /srv/thoughts/venv /srv/thoughts/bin
chmod -R u=rwX,go=rX /srv/thoughts/venv
chmod 0750 /srv/thoughts/venv /srv/thoughts/bin

# Fedora labels files created below /srv as generic data.  systemd's init_t
# domain must only execute the two purpose-built launcher directories, so make
# that exception explicit and persistent without weakening SELinux globally.
semanage fcontext -a -t bin_t '/srv/thoughts/venv/bin(/.*)?' 2>/dev/null || \
  semanage fcontext -m -t bin_t '/srv/thoughts/venv/bin(/.*)?'
semanage fcontext -a -t bin_t '/srv/thoughts/bin(/.*)?' 2>/dev/null || \
  semanage fcontext -m -t bin_t '/srv/thoughts/bin(/.*)?'
restorecon -RF /srv/thoughts/venv/bin /srv/thoughts/bin

if [[ ! -f /etc/thoughts/server.env ]]; then
  install -d -o root -g thoughts -m 0750 /etc/thoughts
  install -o root -g thoughts -m 0640 \
    "${SOURCE}/deploy/fedora/server.env.example" \
    /etc/thoughts/server.env
fi
# The configuration as it was before this deploy. It holds credentials, so the copy
# stays beside the original, for root only, and outside /srv/thoughts/backups, which
# the backup uploads. The ten latest copies are kept.
environment_backup="/etc/thoughts/server.env.pre-deploy-${deployment_stamp}"
install -o root -g root -m 0600 /etc/thoughts/server.env "${environment_backup}"
find /etc/thoughts -maxdepth 1 -type f -name 'server.env.pre-deploy-*' -printf '%f\n' | sort -r | tail -n +11 | \
  while read -r old_copy; do rm -f -- "/etc/thoughts/${old_copy}"; done
sed -i '/^THOUGHTS_RENDER_SCRIPT=/d' /etc/thoughts/server.env
if ! grep -q '^THOUGHTS_SOURCE_BUNDLE_OUTPUT=' /etc/thoughts/server.env; then
  printf '%s\n' 'THOUGHTS_SOURCE_BUNDLE_OUTPUT=/srv/thoughts/state/source-bundle' >>/etc/thoughts/server.env
fi
if grep -q '^THOUGHTS_RENDERER_BACKEND=' /etc/thoughts/server.env; then
  sed -i 's/^THOUGHTS_RENDERER_BACKEND=.*/THOUGHTS_RENDERER_BACKEND=native/' /etc/thoughts/server.env
else
  printf '%s\n' 'THOUGHTS_RENDERER_BACKEND=native' >>/etc/thoughts/server.env
fi
if grep -q '^THOUGHTS_COMMENT_SYNC_BIN=' /etc/thoughts/server.env; then
  sed -i 's#^THOUGHTS_COMMENT_SYNC_BIN=.*#THOUGHTS_COMMENT_SYNC_BIN=/srv/thoughts/bin/thoughts-comment-sync#' /etc/thoughts/server.env
else
  printf '%s\n' 'THOUGHTS_COMMENT_SYNC_BIN=/srv/thoughts/bin/thoughts-comment-sync' >>/etc/thoughts/server.env
fi
# Links the server emits use THOUGHTS_PUBLIC_BASE_URL (default
# https://thoughts.example.org). An installation published with
# configure-public-https.sh before that script wrote the setting names its host
# only in the Caddyfile: record that host once, so links keep using it.
if ! grep -q '^THOUGHTS_PUBLIC_BASE_URL=' /etc/thoughts/server.env; then
  public_host=""
  if [[ -f /etc/caddy/Caddyfile ]] &&
    grep -q 'installed by deploy/fedora/configure-public-https.sh' /etc/caddy/Caddyfile; then
    public_host=$(sed -n 's/^\([A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9-]*\) {$/\1/p' /etc/caddy/Caddyfile | head -n 1 || true)
  fi
  if [[ -n ${public_host} ]]; then
    printf '%s\n' "THOUGHTS_PUBLIC_BASE_URL=https://${public_host}" >>/etc/thoughts/server.env
    echo "Added THOUGHTS_PUBLIC_BASE_URL=https://${public_host} (from /etc/caddy/Caddyfile) to /etc/thoughts/server.env"
  else
    echo "THOUGHTS_PUBLIC_BASE_URL is not set in /etc/thoughts/server.env; emitted links use https://thoughts.example.org" >&2
  fi
fi
runuser -u thoughts -- /usr/bin/bash -c '
  set -a
  source /etc/thoughts/server.env
  set +a
  cd /srv/thoughts/app
  /srv/thoughts/venv/bin/alembic -c alembic.ini upgrade head
  # Every further Library (deploy/fedora/LIBRARIES.md); nothing without a registry.
  /srv/thoughts/venv/bin/thoughts-admin upgrade-library-databases --alembic-config alembic.ini
'
runuser -u thoughts -- /usr/bin/bash -c '
  set -a
  source /etc/thoughts/server.env
  set +a
  /srv/thoughts/venv/bin/thoughts-admin recover-review-reanchors
'

install -o root -g root -m 0644 "${SOURCE}"/deploy/fedora/systemd/*.service /etc/systemd/system/
install -o root -g root -m 0644 "${SOURCE}"/deploy/fedora/systemd/*.timer /etc/systemd/system/
install -d -o root -g root -m 0755 /etc/systemd/logind.conf.d
install -o root -g root -m 0644 \
  "${SOURCE}/deploy/fedora/systemd/thoughts-no-sleep.conf" \
  /etc/systemd/logind.conf.d/thoughts-server.conf
install -d -o root -g root -m 0755 /etc/NetworkManager/conf.d
install -o root -g root -m 0644 \
  "${SOURCE}/deploy/fedora/NetworkManager/thoughts-server.conf" \
  /etc/NetworkManager/conf.d/thoughts-server.conf
active_wifi_uuid=$(nmcli --terse --fields UUID,TYPE connection show --active | \
  awk -F: '$2 == "802-11-wireless" { print $1; exit }')
if [[ -n ${active_wifi_uuid} ]]; then
  nmcli connection modify uuid "${active_wifi_uuid}" \
    connection.autoconnect yes \
    connection.autoconnect-retries 0 \
    802-11-wireless.powersave 2
fi
install -o root -g root -m 0755 "${SOURCE}/deploy/fedora/backup.sh" /usr/local/sbin/thoughts-backup
install -o root -g root -m 0755 "${SOURCE}/deploy/fedora/integrity-check.sh" /usr/local/sbin/thoughts-integrity-check
install -o root -g root -m 0755 "${SOURCE}/deploy/fedora/restore-check.sh" /usr/local/sbin/thoughts-restore-check

# Retire unit files left by the pre-Thoughts deployment. Keeping their timers
# enabled runs a second backup/integrity lifecycle against obsolete paths.
systemctl disable --now mj-okular-backup.timer mj-okular-integrity.timer 2>/dev/null || true
rm -f \
  /etc/systemd/system/mj-okular-api.service \
  /etc/systemd/system/mj-okular-backup.service \
  /etc/systemd/system/mj-okular-backup.timer \
  /etc/systemd/system/mj-okular-integrity.service \
  /etc/systemd/system/mj-okular-integrity.timer \
  /etc/systemd/system/mj-okular-worker.service

systemctl daemon-reload
systemctl reset-failed mj-okular-backup.service mj-okular-integrity.service 2>/dev/null || true
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
if [[ ${START_SERVICES} != true ]]; then
  echo "Native Thoughts services staged but not started."
  exit 0
fi

systemctl enable --now thoughts-api.service thoughts-worker.service thoughts-anchor-worker.service thoughts-review-worker.service thoughts-export-worker.service
systemctl enable --now thoughts-backup.timer thoughts-integrity.timer thoughts-source-history.timer

health_ready=false
for _ in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:8080/v1/health; then
    health_ready=true
    break
  fi
  sleep 1
done
if [[ ${health_ready} != true ]]; then
  echo "Thoughts API did not become healthy within 30 seconds" >&2
  systemctl --no-pager --full status thoughts-api.service >&2 || true
  exit 1
fi
# Every running service must have started from the code installed above.
"${SOURCE}/deploy/fedora/restart-services.py" --check
echo
echo "Native Thoughts services installed."
echo "Review /etc/thoughts/server.env before enabling the production renderer."
