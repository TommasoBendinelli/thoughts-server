#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script as root" >&2
  exit 1
fi

if ! command -v tailscale >/dev/null; then
  echo "Tailscale is not installed" >&2
  exit 1
fi

if ! systemctl is-active --quiet tailscaled; then
  echo "tailscaled is not active" >&2
  exit 1
fi

tailscale serve status || true

if [[ ${1:-} != "--apply" ]]; then
  echo "No changes made."
  echo "Use --apply to publish http://127.0.0.1:8080 privately with Tailscale Serve."
  exit 0
fi

curl --fail --silent http://127.0.0.1:8080/v1/health >/dev/null
tailscale serve --bg http://127.0.0.1:8080
tailscale serve status
