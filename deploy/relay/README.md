<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Public HTTPS relay

Clients reach the Fedora server at its public name (here `https://thoughts.example.org`) without a VPN.

```text
client ──TCP 443/80──▶ relay VPS (HAProxy, mode tcp)
                         │  HTTPS: PROXY protocol v2; HTTP: plain TCP
                         │  over WireGuard 10.77.0.1 → 10.77.0.2
                         ▼
                       Fedora: Caddy on 10.77.0.2 (TLS, Let's Encrypt) ──▶ API 127.0.0.1:8080
```

The VPS is a blind TCP relay: it holds no TLS key and cannot read traffic.
Fedora sits behind the home NAT and dials out to the VPS (`PersistentKeepalive = 25`).
HAProxy proxies in user space, so the VPS needs no IP forwarding or NAT.
The Tailscale Serve route stays active during the transition.

Both scripts report only, and change nothing, unless `--apply` is supplied.

## Requirements

VPS:

- Debian 12, or Ubuntu 22.04 or newer, with root SSH access.
- A static public IPv4 address. 1 vCPU and 512 MiB RAM suffice.
- Traffic allowance for the uploads and downloads (objects reach 512 MiB; every byte crosses the VPS twice).
- Any provider-level firewall or security group must allow 22/tcp (or your SSH port), 80/tcp, 443/tcp and 51820/udp inbound.
- Nothing else listening on 80 or 443.

DNS (for example a Cloudflare zone `example.org`):

- `A` record `thoughts` → the VPS IPv4 address (on Cloudflare: proxy status **DNS only**, grey cloud).
- No `AAAA` record: HAProxy listens on IPv4 only, and Let's Encrypt prefers IPv6 when it exists.
- If the zone has CAA records, one must allow `letsencrypt.org`.

Fedora:

- The server version that contains the authentication rate limiter and loopback-only forwarded-header trust is installed and healthy (`curl http://127.0.0.1:8080/v1/health`).
- Every token in `THOUGHTS_BOOTSTRAP_TOKENS` (if set in `/etc/thoughts/server.env`) has at least 32 characters, otherwise the API refuses to start.
- firewalld is active.

## Order of steps

1. Rent the VPS and note its public IPv4 address.
2. Create the DNS record above and check it: `dig +short thoughts.example.org A` returns the VPS address.
3. On Fedora, create the WireGuard key pair and note `fedora_public_key`:

   ```bash
   sudo ./deploy/fedora/configure-public-https.sh --public-host thoughts.example.org
   sudo ./deploy/fedora/configure-public-https.sh --public-host thoughts.example.org --key-only --apply
   ```

4. Copy this directory to the VPS and run the relay script there; note `relay_public_key` and `relay_endpoint`:

   ```bash
   scp -r deploy/relay root@<vps-ip>:/root/thoughts-relay
   ssh root@<vps-ip>
   /root/thoughts-relay/setup-relay.sh
   /root/thoughts-relay/setup-relay.sh --fedora-public-key <fedora_public_key> --apply
   ```

   Pass `--public-address <vps-ip>` if the printed endpoint is not the public address.

5. On Fedora, connect the tunnel, install Caddy, and wait for the certificate:

   ```bash
   sudo ./deploy/fedora/configure-public-https.sh --public-host thoughts.example.org \
     --relay-public-key <relay_public_key> --relay-endpoint <vps-ip>:51820
   sudo ./deploy/fedora/configure-public-https.sh --public-host thoughts.example.org \
     --relay-public-key <relay_public_key> --relay-endpoint <vps-ip>:51820 --apply
   ```

   The script records `THOUGHTS_PUBLIC_BASE_URL=https://thoughts.example.org` in `/etc/thoughts/server.env` (restart the services to use it) and ends with `https://thoughts.example.org/v1/health is served by this host with a valid certificate.`

6. Verify (below), then switch the clients to `https://thoughts.example.org` in their settings.

## Verify

On Fedora:

```bash
sudo wg show wg-thoughts                 # recent handshake with the relay
sudo firewall-cmd --get-zone-of-interface=wg-thoughts   # thoughts-wg
sudo ss -ltnp '( sport = :443 or sport = :80 )'         # caddy on 10.77.0.2 only
```

On the VPS:

```bash
wg show wg-thoughts
ufw status verbose
journalctl -u haproxy -n 20
```

From a network outside the tailnet (for example a phone on mobile data, or a laptop with Tailscale off):

```bash
curl -sS https://thoughts.example.org/v1/health
curl -sSI http://thoughts.example.org/ | head -n 3            # redirect to https
curl -sSI https://thoughts.example.org/v1/health | grep -i strict-transport-security
curl -s -o /dev/null -w '%{http_code}\n' https://thoughts.example.org/v2/library   # 401
```

`journalctl -u thoughts-api -n 20` on Fedora shows the caller's real public address, not `127.0.0.1` or `10.77.0.1`.
An existing Tailscale Serve address keeps working, for example `curl https://server.example.ts.net/v1/health`.

Ten failed authentications from one address within five minutes block that address with `429` for fifteen minutes; avoid testing this from the address your clients use.

## Rollback

Clients can switch back to the Tailscale address at any time; it is never touched.

Stop public access immediately (either is enough):

- delete the `thoughts` DNS record in Cloudflare, or
- on the VPS: `systemctl disable --now haproxy`.

Remove the Fedora side:

```bash
sudo systemctl disable --now caddy wg-quick@wg-thoughts
sudo firewall-cmd --permanent --delete-zone=thoughts-wg && sudo firewall-cmd --reload
sudo rm /etc/systemd/system/caddy.service.d/thoughts-wireguard.conf \
  /etc/NetworkManager/conf.d/thoughts-wireguard.conf
sudo systemctl daemon-reload && sudo systemctl reload NetworkManager
sudo setsebool -P httpd_can_network_relay off   # only if the dry run showed it off before
```

Earlier versions of replaced files are kept next to them as `*.<timestamp>.bak` (`/etc/caddy/Caddyfile`, `/etc/wireguard/wg-thoughts.conf`, and on the VPS `/etc/haproxy/haproxy.cfg`).
The WireGuard keys stay in `/etc/wireguard/wg-thoughts.key`; delete them only if the relay is retired for good.

Remove the VPS side with `systemctl disable --now haproxy wg-quick@wg-thoughts`, or simply delete the VPS.

To roll back the server code, reinstall the previous checkout with `deploy/fedora/install-native.sh`; the pre-deploy archives are in `/srv/thoughts/backups`.
