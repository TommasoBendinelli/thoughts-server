#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
set -euo pipefail

if [[ ! -r /etc/os-release ]]; then
  echo "Unsupported host: /etc/os-release is missing" >&2
  exit 1
fi

source /etc/os-release

echo "os_id=${ID:-unknown}"
echo "os_version=${VERSION_ID:-unknown}"
echo "os_variant=${VARIANT_ID:-unknown}"
echo "architecture=$(uname -m)"
echo "kernel=$(uname -r)"
echo "hostname=$(hostname --fqdn 2>/dev/null || hostname)"
echo "selinux=$(getenforce 2>/dev/null || echo unavailable)"
echo "systemd=$(systemctl --version | head -n 1)"
echo "python=$(python3 --version 2>&1 || true)"
echo "git=$(git --version 2>&1 || true)"
echo "tailscale=$(tailscale version 2>/dev/null | head -n 1 || echo unavailable)"
echo "zstd=$(zstd --version 2>/dev/null | head -n 1 || echo unavailable)"

if [[ ${ID:-} != "fedora" && ${ID:-} != "fedora-asahi-remix" ]]; then
  echo "Unsupported host: Fedora is required" >&2
  exit 1
fi

if [[ $(uname -m) != "aarch64" ]]; then
  echo "Unsupported host: aarch64 is required for the planned Fedora Asahi deployment" >&2
  exit 1
fi

echo
df -hT / /srv 2>/dev/null || df -hT /
echo
free -h || true
echo
systemctl is-active postgresql 2>/dev/null || true
systemctl is-active tailscaled 2>/dev/null || true
echo
firewall-cmd --list-all 2>/dev/null || true
ss -ltnp 2>/dev/null || true
echo
dnf --quiet list --available \
  postgresql-server python3 python3-devel git qpdf ghostscript zstd cmake ninja-build gcc-c++ restic \
  latexmk texlive-scheme-full \
  2>/dev/null || true
