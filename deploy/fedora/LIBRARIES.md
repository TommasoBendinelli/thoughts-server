<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Several Libraries on one server

One server serves several people. Each person has their own Library, with their own sources, PDFs, comments, review cards and devices. Everyone uses the same address, the server's `THOUGHTS_PUBLIC_BASE_URL` (here `https://thoughts.example.org`); a device's token decides which Library it reaches.

## How it works

- **The default Library** is production's: the database, folders and tokens configured in `/etc/thoughts/server.env`. Without a registry, the server behaves exactly as a single-Library server.
- **Further Libraries** are listed in `/etc/thoughts/libraries.json` (`THOUGHTS_LIBRARIES_FILE`). Each one has:
  - its own PostgreSQL database, `thoughts_lib_<name>`, owned by the `thoughts` role;
  - its own folders under `/srv/thoughts/libraries/<name>/` (`objects`, `state`, `worktree`), laid out like production's.
- **The API** (`thoughts_server.multi_library`) runs one complete application per Library in the same process.
  - A request goes to the Library whose device table contains its token's SHA-256 digest.
  - A request without a token, or with an unknown token, goes to the default Library. That covers `/v1/health`, and an unknown token gets a 401 from the default Library, including its failed-authentication lockout.
  - A token registered in two Libraries works in neither.
  - A device created while the server runs is routed within 5 seconds.
- **Each worker service** (build, export, anchor, review) starts one child process per Library (`thoughts_server.library_supervisor`) and restarts any child that exits. Each child's environment names only its own Library's database and folders, so it is a single-Library worker in every respect, renderer and helpers included.
- **The API's start** (`ExecStartPre`) runs `alembic upgrade head` for production, then `thoughts-admin upgrade-library-databases` for every other Library.
- **Shared** between Libraries: the code, the toolchain, the limits, the agent keys and the address. Nothing a Library stores is readable through another Library's token: separate database, separate object store, separate folders.
- **Build sandbox.** Every Library, the default one included, compiles in a sandbox (`thoughts_server.build_sandbox`). All Libraries are compiled by the same Unix account, so without a sandbox a LaTeX document could read any file that account can (TeX opens absolute paths) or run code: latexmk executes a project's `latexmkrc`, and code can reach the database socket. latexmk therefore runs in bubblewrap with:
  - no network, no `/proc`, its own process-ID namespace, no environment variables except TeX's own, and `-norc`;
  - read-only access to the TeX installation and the server's own code;
  - read-write access to the Library's own document folder only.

  This applies to builds, proposal previews, folder imports and HTML editions. If `bwrap` is missing, a build fails; it never falls back to compiling without the sandbox.
  - `THOUGHTS_SANDBOX_BUILDS=false` in `server.env` turns the sandbox off for the default Library only. Do this only on a server whose single user is its operator: every document and every agent proposal then compiles with the server account's full access. Other Libraries are always sandboxed.
  - latexmk gets `-norc` with or without the sandbox, so a `latexmkrc` in a document folder, in the account's home or in `/etc` is never read.
  - Inside the sandbox TeX cannot run programs at all (`shell_escape=f`), so a document that relies on restricted shell escape, for example on-the-fly EPS conversion, must ship the converted figure.
- **No agents for other Libraries.** They get no agent features that run tools with the server's API key: no Codex build repair and no review-card agents (their review cards re-anchor deterministically). Their worker processes receive no `OPENAI_API_KEY` or restic credentials.
- **Sandbox tests.** `tests/test_build_sandbox.py` compiles a hostile document in the sandbox: an absolute `\input` of an outside file, a `latexmkrc`, and `\write18`. It runs wherever bubblewrap works (Fedora; the builder's kernel refuses user namespaces). `pytest --sandbox-builds` runs every compile test sandboxed.
- **Default folder segment.** Every Library must use the same default-folder segment (`default`). The API refuses to start if any Library differs. New Libraries are created with `default`.

## Add a Library

From a staged checkout of the deployed server version (for example `~/thoughts-server-<sha>`):

```bash
sudo ./deploy/fedora/create-library.sh sam            # report: what it would do
sudo ./deploy/fedora/create-library.sh sam --apply    # do it
```

The script does the following:
- refuses unless the installed server has Library support and production is healthy;
- creates the database and folders;
- adds the registry entry and `THOUGHTS_LIBRARIES_FILE` to `server.env` (with a `.bak` copy);
- migrates the schema and records the `default` folder segment;
- creates the devices `<name>-windows`, `<name>-windows-source-sync` and `<name>-android`;
- restarts the API and the workers, which means a few seconds of downtime for everyone.

The tokens go to `/root/thoughts-library-<name>-devices.json` (root only). The script never prints them. Running it again completes a partial run and keeps existing data and devices.

There is no DNS, Caddy or relay change.

## Hand the person their tokens

Print one token at a time:

```bash
sudo python3 -c 'import json; print(json.load(open("/root/thoughts-library-sam-devices.json"))["devices"]["sam-windows"])'
```

Send it through an end-to-end encrypted channel that expires, such as a Vaultwarden Send with a password and a one-day expiry, or a Signal disappearing message.

- **Windows**: install Thoughts from the `windows-*` GitHub release, then:

  ```powershell
  $ctl = "$env:LOCALAPPDATA\Programs\Thoughts\bin\thoughtsctl.exe"
  & $ctl set-server https://thoughts.example.org
  Read-Host 'Desktop token (sam-windows)' | & $ctl store-secret --account thoughts-windows
  Read-Host 'Publisher token (sam-windows-source-sync)' | & $ctl store-secret --account thoughts-source-sync
  ```

  Skip the guide's "export the source folders" step: the Library starts empty.
- **Android**: Settings › Server: address `https://thoughts.example.org`, token `sam-android`.

Once the person has stored the tokens, delete the file: `sudo shred -u /root/thoughts-library-sam-devices.json`.

## Administer a Library

Every `thoughts-admin` command takes `--library <name>`. It runs with that Library's environment, as if it were the only Library:

```bash
sudo runuser -u thoughts -- bash -c 'set -a; . /etc/thoughts/server.env; set +a
  /srv/thoughts/venv/bin/thoughts-admin --library sam create-device sam-laptop'
sudo runuser -u thoughts -- bash -c 'set -a; . /etc/thoughts/server.env; set +a
  /srv/thoughts/venv/bin/thoughts-admin --library sam rotate-device sam-android'
sudo runuser -u thoughts -- bash -c 'set -a; . /etc/thoughts/server.env; set +a
  /srv/thoughts/venv/bin/thoughts-admin list-libraries'
```

- **Workers.** `THOUGHTS_LIBRARY=<name>` does the same for a worker entry point.
- **Backups.** `backup.sh` dumps every Library's database (`thoughts-lib-<name>-<stamp>.dump`) and adds each Library's `objects` and `worktree` to restic.
- **Disable a Library** without deleting it: set `"enabled": false` in the registry, then restart the API and the workers.

## Not covered yet

- **Agent paths.** The source-history timer, `integrity-check.sh` and agent features that read production's `/srv/thoughts` paths directly (the review agent's home, the Codex CLI) serve the default Library only.
- **App-link fallback page.** `/open/document/<id>` has no token, so its web fallback page looks only in the default Library. On a phone, the app opens such links itself.
- **Removing a Library.** There is no script yet. To remove one by hand:
  1. disable it in the registry;
  2. restart the API and the workers;
  3. `pg_dump` its database, then drop it;
  4. move its folder away.

## Test

`deploy/fedora/library-test/run.sh` runs the whole flow in a disposable Fedora 44 container with PostgreSQL 18. It installs a fake production server from this checkout, then:
- checks that a report-only run changes nothing;
- creates a Library and checks that each token sees only its own projects and objects;
- checks one worker child per Library;
- reruns the script and restarts the API;
- checks that a device created at runtime is routed;
- checks the backups.
