#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
# Publish the loopback API at https://<public host> through the relay VPS
# (deploy/relay).  WireGuard dials out to the relay, Caddy listens only on the
# WireGuard address and terminates TLS, and firewalld opens 80/443 only in the
# WireGuard zone.  Without --apply the script only reports what it found.
#
# The public host is --public-host, or else the host of THOUGHTS_PUBLIC_BASE_URL
# in /etc/thoughts/server.env.  --apply records it there as
# THOUGHTS_PUBLIC_BASE_URL, the origin of the links the server emits.
set -euo pipefail

RELAY_PUBLIC_KEY=""
RELAY_ENDPOINT=""
PUBLIC_HOST=""
ENV_FILE=/etc/thoughts/server.env
KEY_ONLY=false
APPLY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --relay-public-key)
      RELAY_PUBLIC_KEY=${2:?--relay-public-key requires the relay WireGuard public key}
      shift 2
      ;;
    --relay-endpoint)
      RELAY_ENDPOINT=${2:?--relay-endpoint requires host:port}
      shift 2
      ;;
    --public-host)
      PUBLIC_HOST=${2:?--public-host requires a DNS name}
      shift 2
      ;;
    --key-only)
      KEY_ONLY=true
      shift
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
CADDY_TEMPLATE="${SCRIPT_DIR}/caddy/Caddyfile"
CADDY_CONF=/etc/caddy/Caddyfile
CADDY_DROPIN=/etc/systemd/system/caddy.service.d/thoughts-wireguard.conf
NM_UNMANAGED=/etc/NetworkManager/conf.d/thoughts-wireguard.conf
WG_INTERFACE=wg-thoughts
WG_ADDRESS=10.77.0.2/24
WG_LOCAL_IP=10.77.0.2
WG_RELAY_ADDRESS=10.77.0.1/32
WG_KEY=/etc/wireguard/${WG_INTERFACE}.key
WG_PUBLIC=/etc/wireguard/${WG_INTERFACE}.pub
WG_CONF=/etc/wireguard/${WG_INTERFACE}.conf
FIREWALL_ZONE=thoughts-wg
API_HEALTH=http://127.0.0.1:8080/v1/health

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this script as root" >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ ${ID:-} != "fedora" && ${ID:-} != "fedora-asahi-remix" ]]; then
  echo "This script is restricted to Fedora" >&2
  exit 1
fi

if [[ -z ${PUBLIC_HOST} && -r ${ENV_FILE} ]]; then
  PUBLIC_HOST=$(sed -n 's#^THOUGHTS_PUBLIC_BASE_URL=https://\([^/:]*\).*$#\1#p' "${ENV_FILE}" | tail -n 1 || true)
  if [[ ${PUBLIC_HOST} == thoughts.example.org ]]; then
    PUBLIC_HOST=""
  fi
fi
if [[ -z ${PUBLIC_HOST} ]]; then
  echo "Name the public DNS name with --public-host (or set THOUGHTS_PUBLIC_BASE_URL in ${ENV_FILE})" >&2
  exit 2
fi
if [[ ! -f ${CADDY_TEMPLATE} ]]; then
  echo "Missing ${CADDY_TEMPLATE}" >&2
  exit 1
fi
if [[ ! ${PUBLIC_HOST} =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]]; then
  echo "--public-host must be a DNS name" >&2
  exit 2
fi
if [[ -n ${RELAY_PUBLIC_KEY} && ! ${RELAY_PUBLIC_KEY} =~ ^[A-Za-z0-9+/]{43}=$ ]]; then
  echo "--relay-public-key is not a WireGuard public key" >&2
  exit 2
fi
if [[ -n ${RELAY_ENDPOINT} && ! ${RELAY_ENDPOINT} =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}$ ]]; then
  echo "--relay-endpoint must be <IPv4 address or DNS name>:<port>" >&2
  exit 2
fi

# The built-in proxy_protocol listener wrapper exists since Caddy 2.7.
caddy_supported() {
  local version
  version=$(caddy version 2>/dev/null | awk '{ sub(/^v/, "", $1); print $1; exit }')
  [[ -n ${version} ]] || return 1
  [[ $(printf '%s\n%s\n' 2.7.0 "${version%%-*}" | sort -V | head -n 1) == 2.7.0 ]]
}

print_public_key() {
  echo "fedora_public_key=$(cat "${WG_PUBLIC}")"
}

ensure_wireguard_key() {
  install -d -o root -g root -m 0700 /etc/wireguard
  if [[ ! -f ${WG_KEY} ]]; then
    (umask 077 && wg genkey >"${WG_KEY}")
  fi
  chmod 0600 "${WG_KEY}"
  wg pubkey <"${WG_KEY}" >"${WG_PUBLIC}"
  chmod 0644 "${WG_PUBLIC}"
}

echo "os=${PRETTY_NAME:-unknown}"
echo "public_host=${PUBLIC_HOST}"
for package in wireguard-tools caddy firewalld; do
  if rpm -q "${package}" >/dev/null 2>&1; then
    echo "package $(rpm -q "${package}")"
  else
    echo "package ${package}=missing"
  fi
done
if command -v caddy >/dev/null; then
  echo "caddy_version=$(caddy version 2>/dev/null | awk '{ print $1; exit }')"
  if ! caddy_supported; then
    echo "Installed Caddy is older than 2.7 and lacks the proxy_protocol listener wrapper; see deploy/fedora/README.md"
  fi
else
  echo "caddy_candidate=$(dnf repoquery --latest-limit 1 --queryformat '%{version}' caddy 2>/dev/null | tail -n 1)"
fi
if [[ -f ${WG_PUBLIC} ]]; then
  print_public_key
else
  echo "wireguard_key=absent (run with --key-only --apply to create it)"
fi
echo "wireguard_service=$(systemctl is-active "wg-quick@${WG_INTERFACE}" 2>/dev/null || true)"
echo "caddy_service=$(systemctl is-active caddy 2>/dev/null || true)"
echo "firewalld_service=$(systemctl is-active firewalld 2>/dev/null || true)"
if systemctl is-active --quiet firewalld; then
  echo "wireguard_zone=$(firewall-cmd --permanent --get-zone-of-interface="${WG_INTERFACE}" 2>/dev/null || true)"
fi
if command -v getsebool >/dev/null; then
  getsebool httpd_can_network_relay 2>/dev/null || true
fi
if curl --fail --silent "${API_HEALTH}" >/dev/null; then
  echo "api_health=ok"
else
  echo "api_health=unreachable"
fi

if [[ ${APPLY} != true ]]; then
  echo "No changes made."
  echo "Use --key-only --apply to create the WireGuard key pair for setup-relay.sh."
  echo "Use --relay-public-key <key> --relay-endpoint <host:port> --apply to publish https://${PUBLIC_HOST}."
  exit 0
fi

if [[ ${KEY_ONLY} == true ]]; then
  dnf install -y wireguard-tools
  ensure_wireguard_key
  print_public_key
  echo "Pass this key to deploy/relay/setup-relay.sh --fedora-public-key on the relay VPS."
  exit 0
fi

if [[ -z ${RELAY_PUBLIC_KEY} || -z ${RELAY_ENDPOINT} ]]; then
  echo "--apply requires --relay-public-key and --relay-endpoint" >&2
  exit 2
fi
if ! systemctl is-active --quiet firewalld; then
  echo "firewalld is not active; enable it before publishing the server" >&2
  exit 1
fi
curl --fail --silent "${API_HEALTH}" >/dev/null

dnf install -y wireguard-tools caddy
if ! caddy_supported; then
  echo "Caddy $(caddy version 2>/dev/null | awk '{ print $1; exit }') lacks the proxy_protocol listener wrapper (2.7 or newer is required); see deploy/fedora/README.md" >&2
  exit 1
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
staged=$(mktemp -d)
trap 'rm -rf "${staged}"' EXIT

# WireGuard client: dial out to the relay and keep the NAT mapping open.
ensure_wireguard_key
cat >"${staged}/wg.conf" <<EOF
# Thoughts relay tunnel (installed by deploy/fedora/configure-public-https.sh).
# The private key stays in ${WG_KEY}.
[Interface]
Address = ${WG_ADDRESS}
PostUp = wg set %i private-key ${WG_KEY}

# Relay VPS: forwards public ports 80/443 to ${WG_LOCAL_IP}.  Only the relay's
# tunnel address is routed here; all other traffic keeps its normal route.
[Peer]
PublicKey = ${RELAY_PUBLIC_KEY}
Endpoint = ${RELAY_ENDPOINT}
AllowedIPs = ${WG_RELAY_ADDRESS}
PersistentKeepalive = 25
EOF
if [[ -f ${WG_CONF} ]] && ! cmp -s "${staged}/wg.conf" "${WG_CONF}"; then
  cp -p "${WG_CONF}" "${WG_CONF}.${stamp}.bak"
fi
install -o root -g root -m 0600 "${staged}/wg.conf" "${WG_CONF}"

# wg-quick owns the interface; keep NetworkManager from claiming it and its
# firewalld zone.
install -d -o root -g root -m 0755 /etc/NetworkManager/conf.d
printf '%s\n' '[keyfile]' "unmanaged-devices=interface-name:${WG_INTERFACE}" >"${staged}/nm.conf"
install -o root -g root -m 0644 "${staged}/nm.conf" "${NM_UNMANAGED}"
systemctl reload NetworkManager 2>/dev/null || true

systemctl enable "wg-quick@${WG_INTERFACE}"
systemctl restart "wg-quick@${WG_INTERFACE}"

# firewalld: HTTP and HTTPS only in the WireGuard zone.
if ! firewall-cmd --permanent --get-zones | tr ' ' '\n' | grep -qx "${FIREWALL_ZONE}"; then
  firewall-cmd --permanent --new-zone="${FIREWALL_ZONE}"
fi
current_zone=$(firewall-cmd --permanent --get-zone-of-interface="${WG_INTERFACE}" 2>/dev/null || true)
if [[ ${current_zone} == "no zone" ]]; then
  current_zone=""
fi
if [[ -n ${current_zone} && ${current_zone} != "${FIREWALL_ZONE}" ]]; then
  firewall-cmd --permanent --zone="${current_zone}" --remove-interface="${WG_INTERFACE}"
fi
if [[ ${current_zone} != "${FIREWALL_ZONE}" ]]; then
  firewall-cmd --permanent --zone="${FIREWALL_ZONE}" --add-interface="${WG_INTERFACE}"
fi
firewall-cmd --permanent --zone="${FIREWALL_ZONE}" --add-service=http
firewall-cmd --permanent --zone="${FIREWALL_ZONE}" --add-service=https
firewall-cmd --reload
firewall-cmd --zone="${FIREWALL_ZONE}" --list-all

# Caddy runs in the httpd_t SELinux domain; the API port 8080 is
# http_cache_port_t, which httpd_can_network_relay permits.
if command -v selinuxenabled >/dev/null && selinuxenabled; then
  setsebool -P httpd_can_network_relay on
fi

# Caddy needs the WireGuard address before it can bind.
install -d -o root -g root -m 0755 "$(dirname "${CADDY_DROPIN}")"
cat >"${staged}/caddy-dropin.conf" <<EOF
[Unit]
Requires=wg-quick@${WG_INTERFACE}.service
After=wg-quick@${WG_INTERFACE}.service thoughts-api.service
EOF
install -o root -g root -m 0644 "${staged}/caddy-dropin.conf" "${CADDY_DROPIN}"

sed "s/@PUBLIC_HOST@/${PUBLIC_HOST}/g" "${CADDY_TEMPLATE}" >"${staged}/Caddyfile"
chmod 0644 "${staged}/Caddyfile"
caddy validate --adapter caddyfile --config "${staged}/Caddyfile"
if [[ -f ${CADDY_CONF} ]] && ! cmp -s "${staged}/Caddyfile" "${CADDY_CONF}"; then
  cp -p "${CADDY_CONF}" "${CADDY_CONF}.${stamp}.bak"
fi
install -o root -g root -m 0644 "${staged}/Caddyfile" "${CADDY_CONF}"
restorecon "${CADDY_CONF}" 2>/dev/null || true

systemctl daemon-reload
systemctl enable caddy
# Always a restart: the Caddyfile turns the admin endpoint off, and a reload needs it.
systemctl restart caddy

# Links the server emits name this host (config.py THOUGHTS_PUBLIC_BASE_URL).
if [[ -f ${ENV_FILE} ]] && ! grep -qx "THOUGHTS_PUBLIC_BASE_URL=https://${PUBLIC_HOST}" "${ENV_FILE}"; then
  cp -p "${ENV_FILE}" "${ENV_FILE}.${stamp}.bak"
  sed -i '/^THOUGHTS_PUBLIC_BASE_URL=/d' "${ENV_FILE}"
  printf '%s\n' "THOUGHTS_PUBLIC_BASE_URL=https://${PUBLIC_HOST}" >>"${ENV_FILE}"
  echo "Set THOUGHTS_PUBLIC_BASE_URL=https://${PUBLIC_HOST} in ${ENV_FILE}; restart the services to use it:"
  echo "  sudo ${SCRIPT_DIR}/restart-services.py"
fi

echo
wg show "${WG_INTERFACE}" latest-handshakes || true
echo "Waiting for the certificate for ${PUBLIC_HOST} (DNS must already point to the relay)..."
for _ in {1..60}; do
  if curl --fail --silent --show-error --max-time 10 \
    --resolve "${PUBLIC_HOST}:443:${WG_LOCAL_IP}" \
    "https://${PUBLIC_HOST}/v1/health" >/dev/null 2>&1; then
    echo "https://${PUBLIC_HOST}/v1/health is served by this host with a valid certificate."
    print_public_key
    exit 0
  fi
  sleep 5
done
echo "No valid certificate yet. Check the DNS record, the relay, and: journalctl -u caddy -n 100" >&2
exit 1
