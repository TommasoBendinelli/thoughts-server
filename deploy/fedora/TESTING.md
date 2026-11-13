<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Fedora test environment

`thoughts-test-api.service` runs a separate instance on loopback port 8081, exposed privately with Tailscale Serve at `https://<host>.<tailnet>.ts.net:8443` (for example `https://server.example.ts.net:8443`; `status` reports it).
It runs as Unix user `thoughts-test`, uses PostgreSQL database `thoughts_test`, and stores files beneath `/srv/thoughts-test`.
Its runtime and source are copied separately so it does not need access to `/srv/thoughts` or `/etc/thoughts`.
Its credentials are generated independently and production must reject them.
No real documents, production tokens, or AI credentials are copied into the test environment.
`thoughts-test-worker.service` runs the build worker with the native renderer against `/srv/thoughts-test/worktree`, so builds publish PDFs and Project readings as in production. AI workers are not started in this environment.
The credentials file holds a token for each test device (`thoughts-android-test`, `thoughts-mac-test`, and `thoughts-mac-source-test` for the Mac's source publisher, which like production's has its own operation sequence).

The operator is the account that runs the script with sudo. To install, stage the application source at `~/thoughts-test-stage/src/thoughts_server` in the operator's home (or under `/srv/thoughts-test/app/src/thoughts_server`), then run this repository's `deploy/fedora/test-environment.py install` with sudo. Install refreshes the test runtime from production's virtual environment and copies the comment-sync helper.
To test another server revision later, stage it again and run `sudo -n /usr/local/sbin/thoughts-test-environment update`; it accepts only plain files owned by the operator and restarts the test API and worker.
Install also copies `restart-services.py` to `/usr/local/sbin/thoughts-restart-services`; reset, update, and install use it to stop the worker before the API and start the API before the worker, so the two never race to create an empty schema. Production uses the same script (see README.md) and the test instance exercises it on every reset.
The installer creates a dedicated account, database, service, Tailscale listener, and a narrowly scoped sudo rule that lets the operator control the test environment.
The application source must be staged from the same revision being tested.

The device-test wrapper invokes these commands over SSH:

```sh
sudo -n /usr/local/sbin/thoughts-test-environment status
sudo -n /usr/local/sbin/thoughts-test-environment reset
```

Reset checks the fixed database name and owner, stops only the test services, recreates only its schema and disposable file directories, restarts the test services, and reports the empty state.
It does not modify the production database, files, listener, or service.
The `credentials` command returns only the test credential and must be captured privately rather than printed to logs.
Reset is performed before and after each integration-test run, including failed runs.
A reset before the next run recovers test data left by a killed process or lost connection.
The environment is shared by test runs, so serialize access and never reset it while another run is active.
