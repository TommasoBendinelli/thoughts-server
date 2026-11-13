<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Native Fedora Asahi deployment

These files deploy the service directly on Fedora ARM64 with no container runtime.

Run `preflight.sh` first after SSH access and review its output before running any installation command.

`install-native.sh` refuses to change the host unless `--apply` is supplied.

The installation creates a locked `thoughts` service account, PostgreSQL role and database, Python virtual environment, application directories, and hardened systemd services.

Every applied install first writes a PostgreSQL dump and an application-tree archive under `/srv/thoughts/backups`, then stops the API and workers before replacing code.

Alembic runs as an API `ExecStartPre`, so the worker cannot start against the previous schema.

It also masks system sleep and hibernation and disables logind idle/lid-triggered sleep because this host is the always-on repository authority.

The API binds only to `127.0.0.1:8080`.

After the local health check succeeds, `configure-tailscale.sh --apply` publishes it privately through Tailscale Serve.

The current Tailscale command is deliberately isolated in that script so it can be checked against the installed client before use.

The headless Okular helper and complete TeX toolchain are validated separately because their exact Fedora package set depends on the available Fedora Asahi release.

The installer builds the comment helper from the matching staged Desktop checkout and builds the source-artifact worker from this server checkout.

## Deployment order

```bash
./deploy/fedora/preflight.sh
sudo ./deploy/fedora/install-native.sh \
  --source "$PWD" \
  --desktop-source ~/thoughts-desktop-deploy \
  --apply
./deploy/fedora/restart-services.py --check
curl --fail http://127.0.0.1:8080/v1/health
sudo ./deploy/fedora/configure-tailscale.sh --apply
```

## Routine deploys

`install-deploy-access.sh` (once, with sudo from the deploy account) installs `thoughts-deploy` as `/usr/local/sbin/thoughts-deploy` and lets that account run it and `thoughts-restart-services` without a password.
A deploy then stages a server copy and the paired desktop copy in the deploy account's home, named after their commits, and installs them:

```bash
git archive --prefix=thoughts-server-<sha>/ <sha> | ssh <server> 'tar -x -C ~'
ssh <server> sudo -n /usr/local/sbin/thoughts-deploy ~/thoughts-server-<sha> ~/thoughts-desktop-deploy-<desktop sha>
```

`thoughts-deploy` accepts only real directories owned by the account that ran sudo, directly in its home, named `thoughts-server-<sha>` and `thoughts-desktop-deploy-<sha>`; it runs `install-native.sh --apply` and checks the restarted services and `/v1/health`.

## Public HTTPS

`configure-public-https.sh --public-host <DNS name>` publishes the same loopback API at `https://<DNS name>` without a VPN, through the blind TCP relay in `deploy/relay`; follow the order of steps in `deploy/relay/README.md`.
Without `--public-host` it uses the host of `THOUGHTS_PUBLIC_BASE_URL` in `/etc/thoughts/server.env`; with `--apply` it records the host there as `THOUGHTS_PUBLIC_BASE_URL`, the origin of the links the server emits (default `https://thoughts.example.org`).

It refuses to change the host unless `--apply` is supplied, and `--key-only --apply` only creates the WireGuard key pair whose public key the relay script needs.

With `--apply` it configures the `wg-thoughts` WireGuard client (`10.77.0.2`, dialing out to the relay at `10.77.0.1`), installs Caddy with `caddy/Caddyfile`, opens 80/443 only in the `thoughts-wg` firewalld zone, and waits until the certificate is served locally.

Caddy listens only on `10.77.0.2`, accepts PROXY protocol v2 headers only from `10.77.0.1`, obtains its own Let's Encrypt certificate, sends HSTS, and proxies to `127.0.0.1:8080`.
Its admin endpoint is off (`admin off`): by default any local process could reconfigure the proxy through `127.0.0.1:2019`. A changed `Caddyfile` is therefore applied with `systemctl restart caddy`; `systemctl reload caddy` needs the admin endpoint and no longer works.

The API trusts `X-Forwarded-For` only from `127.0.0.1`, so it sees the real client address for requests through Caddy and the tailnet address for requests through Tailscale Serve.

The built-in `proxy_protocol` listener wrapper requires Caddy 2.7 or newer; Fedora 43 and 44 ship Caddy 2.10.
If `dnf` offers an older Caddy, the script stops before changing Caddy; install Caddy from the upstream Fedora COPR (`dnf copr enable @caddy/caddy`) and run it again.

The Tailscale Serve route stays active until it is retired separately.

## Server code updates

The installed code (`/srv/thoughts/app`, `/srv/thoughts/venv`, `/srv/thoughts/bin`) belongs to root; the services can read and run it but not change it, so a file copied into it must be copied by root. The installer runs scripts and installs unit files only from the staged source it was given, never from `/srv/thoughts`, and refuses a staged source that the `thoughts` account owns. `/srv/thoughts` itself still belongs to the `thoughts` account (it is that account's home directory), so root must keep running nothing from below it.

The API and every worker share `/srv/thoughts/venv`. After server code changes by any route (this installer or a copied file), restart all of them together; restarting only `thoughts-api` leaves the workers on the old code:

```bash
sudo ./deploy/fedora/restart-services.py
./deploy/fedora/restart-services.py --check
```

The script discovers every enabled Thoughts service, stops the workers before the API, starts the API first, and reports any service that did not restart. `--check` needs no root and lists services that started well before the API.

Create device credentials only after saving the database and object-store backup destinations:

```bash
sudo -u thoughts /srv/thoughts/venv/bin/thoughts-admin create-device thoughts-mac
sudo -u thoughts /srv/thoughts/venv/bin/thoughts-admin create-device thoughts-android
```

## Renderer activation

Keep `THOUGHTS_ALLOW_DEVELOPMENT_BUILDS=false` in production.

Production builds use the server-native backend and the installed comment helper:

```text
THOUGHTS_RENDERER_BACKEND=native
THOUGHTS_COMMENT_SYNC_BIN=/srv/thoughts/bin/thoughts-comment-sync
```

Restart the build worker after changing renderer configuration and the anchor worker after changing the comment helper.

## Restore drill

After the first backup, validate it without touching the production database:

```bash
sudo /usr/local/sbin/thoughts-restore-check \
  /srv/thoughts/backups/thoughts-<timestamp>.dump
```

The root wrapper creates and drops only a validated, uniquely named temporary database.

The dump restore, object verification, and row-count queries run as the locked `thoughts` database role.

## Inspecting a source file's server state

`thoughts-source-status` prints a source file's server revision and its recent source operations, read-only; `--content` also prints the server's current copy.
It is installed as `/usr/local/sbin/thoughts-source-status`, and `/etc/sudoers.d/thoughts-source-status` lets the deploy account run it without a password:

```bash
sudo install -o root -g root -m 0755 deploy/fedora/thoughts-source-status /usr/local/sbin/thoughts-source-status
echo "${USER} ALL=(root) NOPASSWD: /usr/local/sbin/thoughts-source-status" | sudo tee /etc/sudoers.d/thoughts-source-status >/dev/null
sudo chmod 0440 /etc/sudoers.d/thoughts-source-status && sudo visudo -cf /etc/sudoers.d/thoughts-source-status
sudo thoughts-source-status docs-example/markdown/source/notes.md --content
```

It reads the database in a read-only transaction as the `thoughts` role and prints files only from inside the worktree.
