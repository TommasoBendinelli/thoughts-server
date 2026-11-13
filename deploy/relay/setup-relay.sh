#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Configure a small Debian/Ubuntu VPS as the blind TCP relay in front of the
# Fedora Thoughts server.  See deploy/relay/README.md for the complete order of
# steps.  Without --apply the script only reports what it found.
set -euo pipefail

FEDORA_PUBLIC_KEY=""
PUBLIC_ADDRESS=""
APPLY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --fedora-public-key)
      FEDORA_PUBLIC_KEY=${2:?--fedora-public-key requires the Fedora WireGuard public key}
      shift 2
      ;;
    --public-address)
      PUBLIC_ADDRESS=${2:?--public-address requires the VPS public IPv4 address}
      shift 2
      ;;
    --apply)
      APPLY=true
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
HAPROXY_TEMPLATE="${SCRIPT_DIR}/haproxy.cfg"
WG_INTERFACE=wg-thoughts
WG_PORT=51820
WG_RELAY_ADDRESS=10.77.0.1/24
WG_FEDORA_ADDRESS=10.77.0.2/32
WG_KEY=/etc/wireguard/${WG_INTERFACE}.key
WG_PUBLIC=/etc/wireguard/${WG_INTERFACE}.pub
WG_CONF=/etc/wireguard/${WG_INTERFACE}.conf
HAPROXY_CONF=/etc/haproxy/haproxy.cfg
AUTO_UPGRADES=/etc/apt/apt.conf.d/20auto-upgrades
PACKAGES=(wireguard-tools haproxy ufw unattended-upgrades)

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script as root" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]] || ! command -v apt-get >/dev/null || ! command -v dpkg >/dev/null; then
  echo "Unsupported host: Debian 12 or Ubuntu 22.04 or newer with apt is required" >&2
  exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
supported=false
case "${ID:-}" in
  debian)
    if [[ -n ${VERSION_ID:-} ]] && dpkg --compare-versions "${VERSION_ID}" ge 12; then
      supported=true
    fi
    ;;
  ubuntu)
    if [[ -n ${VERSION_ID:-} ]] && dpkg --compare-versions "${VERSION_ID}" ge 22.04; then
      supported=true
    fi
    ;;
esac
if [[ ${supported} != true ]]; then
  echo "Unsupported host: ${PRETTY_NAME:-unknown}; Debian 12 or Ubuntu 22.04 or newer is required" >&2
  exit 1
fi

if [[ ! -f ${HAPROXY_TEMPLATE} ]]; then
  echo "Missing ${HAPROXY_TEMPLATE}; copy the whole deploy/relay directory to this host" >&2
  exit 1
fi

if [[ -n ${FEDORA_PUBLIC_KEY} && ! ${FEDORA_PUBLIC_KEY} =~ ^[A-Za-z0-9+/]{43}=$ ]]; then
  echo "--fedora-public-key is not a WireGuard public key" >&2
  exit 2
fi

if [[ -z ${PUBLIC_ADDRESS} ]]; then
  PUBLIC_ADDRESS=$(ip -4 route get 1.1.1.1 2>/dev/null |
    awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }')
fi

# Every port sshd listens on plus the port of the current session, so enabling
# the firewall cannot lock out the administrator.
ssh_ports() {
  {
    sshd -T 2>/dev/null | awk '$1 == "port" { print $2 }' || true
    if [[ -n ${SSH_CONNECTION:-} ]]; then
      awk '{ print $4 }' <<<"${SSH_CONNECTION}"
    fi
  } | grep -E '^[0-9]+$' | sort -un
}
mapfile -t SSH_PORTS < <(ssh_ports)
if [[ ${#SSH_PORTS[@]} -eq 0 ]]; then
  SSH_PORTS=(22)
fi

# Listeners on 80/443 that are not HAProxy would block the relay.
foreign_listeners() {
  ss -H -ltnp '( sport = :80 or sport = :443 )' 2>/dev/null | grep -v '"haproxy"' || true
}

echo "os=${PRETTY_NAME:-unknown}"
echo "public_address=${PUBLIC_ADDRESS:-unknown}"
echo "ssh_ports=${SSH_PORTS[*]}"
for package in "${PACKAGES[@]}"; do
  if dpkg-query -W -f='${Status}' "${package}" 2>/dev/null | grep -q 'install ok installed'; then
    echo "package ${package}=installed"
  else
    echo "package ${package}=missing"
  fi
done
if [[ -f ${WG_KEY} ]]; then
  echo "wireguard_key=present"
else
  echo "wireguard_key=absent (generated with --apply)"
fi
if [[ -f ${WG_CONF} ]]; then
  echo "wireguard_config=${WG_CONF} present"
fi
echo "wireguard_service=$(systemctl is-active "wg-quick@${WG_INTERFACE}" 2>/dev/null || true)"
echo "haproxy_service=$(systemctl is-active haproxy 2>/dev/null || true)"
if [[ -f ${HAPROXY_CONF} ]] && cmp -s "${HAPROXY_TEMPLATE}" "${HAPROXY_CONF}"; then
  echo "haproxy_config=current"
else
  echo "haproxy_config=differs (replaced with --apply; the old file is kept as a backup)"
fi
if command -v ufw >/dev/null; then
  ufw status verbose || true
fi
blocking=$(foreign_listeners)
if [[ -n ${blocking} ]]; then
  echo "Other services listen on port 80 or 443:"
  echo "${blocking}"
fi

if [[ ${APPLY} != true ]]; then
  echo "No changes made."
  echo "Use --fedora-public-key <key> --apply to install WireGuard, HAProxy, the firewall, and unattended security upgrades."
  exit 0
fi

if [[ -z ${FEDORA_PUBLIC_KEY} ]]; then
  echo "--apply requires --fedora-public-key" >&2
  exit 2
fi
if [[ -n ${blocking} ]]; then
  echo "Stop the services listening on port 80 or 443 before installing the relay" >&2
  exit 1
fi
if [[ -z ${PUBLIC_ADDRESS} ]]; then
  echo "Cannot determine the public address; pass --public-address" >&2
  exit 1
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y "${PACKAGES[@]}"

# WireGuard: keep an existing key pair; never print the private key.
install -d -o root -g root -m 0700 /etc/wireguard
if [[ ! -f ${WG_KEY} ]]; then
  (umask 077 && wg genkey >"${WG_KEY}")
fi
chmod 0600 "${WG_KEY}"
wg pubkey <"${WG_KEY}" >"${WG_PUBLIC}"
chmod 0644 "${WG_PUBLIC}"

wg_config=$(mktemp)
trap 'rm -f "${wg_config}"' EXIT
cat >"${wg_config}" <<EOF
# Thoughts relay tunnel (installed by deploy/relay/setup-relay.sh).
# The private key stays in ${WG_KEY}.
[Interface]
Address = ${WG_RELAY_ADDRESS}
ListenPort = ${WG_PORT}
PostUp = wg set %i private-key ${WG_KEY}

# Fedora Thoughts server; it dials out from behind its NAT.
[Peer]
PublicKey = ${FEDORA_PUBLIC_KEY}
AllowedIPs = ${WG_FEDORA_ADDRESS}
EOF
if [[ -f ${WG_CONF} ]] && ! cmp -s "${wg_config}" "${WG_CONF}"; then
  cp -p "${WG_CONF}" "${WG_CONF}.${stamp}.bak"
fi
install -o root -g root -m 0600 "${wg_config}" "${WG_CONF}"
systemctl enable "wg-quick@${WG_INTERFACE}"
systemctl restart "wg-quick@${WG_INTERFACE}"

# HAProxy: validate before replacing the running configuration.
haproxy -c -f "${HAPROXY_TEMPLATE}"
if [[ -f ${HAPROXY_CONF} ]] && ! cmp -s "${HAPROXY_TEMPLATE}" "${HAPROXY_CONF}"; then
  cp -p "${HAPROXY_CONF}" "${HAPROXY_CONF}.${stamp}.bak"
fi
install -o root -g root -m 0644 "${HAPROXY_TEMPLATE}" "${HAPROXY_CONF}"
systemctl enable haproxy
systemctl restart haproxy

# Firewall: SSH, the relayed web ports, and WireGuard only.  Existing rules
# are kept; review `ufw status` if this host was configured before.
ufw default deny incoming
ufw default allow outgoing
for port in "${SSH_PORTS[@]}"; do
  ufw allow "${port}/tcp" comment 'SSH'
done
ufw allow 80/tcp comment 'Thoughts HTTP relay'
ufw allow 443/tcp comment 'Thoughts HTTPS relay'
ufw allow "${WG_PORT}/udp" comment 'Thoughts WireGuard'
ufw --force enable
ufw status verbose

# Unattended security upgrades (the distribution default origins are
# security updates only).
cat >"${AUTO_UPGRADES}" <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
chmod 0644 "${AUTO_UPGRADES}"
systemctl enable --now unattended-upgrades

echo
echo "Relay configured."
echo "relay_public_key=$(cat "${WG_PUBLIC}")"
echo "relay_endpoint=${PUBLIC_ADDRESS}:${WG_PORT}"
echo
echo "On the Fedora server run:"
echo "  sudo ./deploy/fedora/configure-public-https.sh --public-host <public DNS name> --relay-public-key $(cat "${WG_PUBLIC}") --relay-endpoint ${PUBLIC_ADDRESS}:${WG_PORT} --apply"
