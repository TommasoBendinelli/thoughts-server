#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# One-time setup: let the deploy account deploy staged Thoughts copies and
# restart the services without a password, so routine deploys need no password
# prompt.
#
# Usage: sudo ./install-deploy-access.sh [ACCOUNT]
#   ACCOUNT defaults to the account that runs sudo.
#
# Installs, as root-owned files:
#   /usr/local/sbin/thoughts-deploy              (from this directory)
#   /etc/sudoers.d/thoughts-ACCOUNT-deploy       ACCOUNT may run, without a password:
#       /usr/local/sbin/thoughts-deploy
#       /usr/local/sbin/thoughts-restart-services
#
# Security: thoughts-deploy runs the staged installer as root, so this grants
# root to anyone who can act as ACCOUNT (for example with its SSH key).
# Remove the access again with:
#   sudo rm /etc/sudoers.d/thoughts-ACCOUNT-deploy /usr/local/sbin/thoughts-deploy
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script with sudo" >&2
  exit 1
fi
DEPLOY_USER=${1:-${SUDO_USER:-}}
if [[ ! ${DEPLOY_USER} =~ ^[a-z_][a-z0-9_-]{0,31}$ || ${DEPLOY_USER} == root ]] ||
  ! getent passwd "${DEPLOY_USER}" >/dev/null; then
  echo "Name the deploy account (not root): sudo $0 ACCOUNT" >&2
  exit 2
fi
HERE=$(dirname -- "$(realpath -- "${BASH_SOURCE[0]}")")
if [[ ! -x /usr/local/sbin/thoughts-restart-services ]]; then
  echo "/usr/local/sbin/thoughts-restart-services is missing; run install-native.sh first" >&2
  exit 1
fi

install -o root -g root -m 0755 "${HERE}/thoughts-deploy" /usr/local/sbin/thoughts-deploy
touch /var/log/thoughts-deploy.log
chmod 0644 /var/log/thoughts-deploy.log

RULE=/etc/sudoers.d/thoughts-${DEPLOY_USER}-deploy
TEMPORARY=$(mktemp)
trap 'rm -f "${TEMPORARY}"' EXIT
cat >"${TEMPORARY}" <<EOF
# Thoughts: routine deploys and restarts without a password (install-deploy-access.sh).
${DEPLOY_USER} ALL=(root) NOPASSWD: /usr/local/sbin/thoughts-deploy, /usr/local/sbin/thoughts-restart-services
EOF
visudo -cf "${TEMPORARY}"
install -o root -g root -m 0440 "${TEMPORARY}" "${RULE}"
visudo -c >/dev/null
echo "Installed /usr/local/sbin/thoughts-deploy and ${RULE}"
