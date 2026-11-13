<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Load test: many Libraries on one server process

`loadtest.py` simulates the clients of N Libraries against one Thoughts server
and reports, per route, how long the answers took, how far long polls and
build waits overshot, and how long the server's event loop was stalled.
It is meant for before/after numbers: the same command against two versions
of the server source.

It needs nothing beyond the server's own dependencies (`httpx`, `uvicorn`,
`sqlalchemy`, ...). It never installs anything.

## Local mode

Generates N Libraries on sqlite, starts the server as a subprocess on a free
port of `127.0.0.1`, runs the load, stops the server and deletes everything it
wrote.

```sh
PYTHONPATH=src python tools/loadtest/loadtest.py --local --libraries 1  --duration 30
PYTHONPATH=src python tools/loadtest/loadtest.py --local --libraries 5  --duration 30
PYTHONPATH=src python tools/loadtest/loadtest.py --local --libraries 20 --duration 40 --json-out after.json
```

The server source that is measured is the `thoughts_server` package on
`PYTHONPATH` (printed at the top as `server source`). For the "before" numbers
point `PYTHONPATH` at the `src` folder of the other version; the tool itself
stays the same.

What happens:

1. `seed.py` creates each Library in a process of its own: the default
   Library configured by the environment plus N-1 Libraries of a generated
   registry file (`THOUGHTS_LIBRARIES_FILE`), each with its own database,
   object store, state folder and worktree, two devices with generated tokens,
   and the data described under [Seeded data](#seeded-data).
2. `local_server.py` serves them as production does (one uvicorn process, one
   event loop, `create_server_app`), wrapped in a thin ASGI layer that measures
   event-loop lag and answers `GET /__loadtest/stats`. Access logging is off;
   the server's log (one JSON line per request, as in production) goes to a
   file in the work folder, not to the terminal.
3. The load runs: `--warmup` seconds that are not measured, then `--duration`
   seconds that are.
4. The server is stopped and the work folder is deleted, also after Ctrl-C or
   an error.

All temporary data lives in one fresh folder under `--work-dir` (default: the
system temporary directory); only that folder is deleted, never `--work-dir`
itself. The tool refuses to start when less than 2 GiB are free there, or when
the run would leave less than 1 GiB. A seeded Library takes about 10 MiB.
Each uploaded blob is deleted from the generated object store as soon as the
server has stored it (its row stays, nothing reads it again), so a default
20-Library run holds about 300 MiB at most however long it runs.
`--keep-uploads` keeps them all: at 20 Libraries that is 80 MiB more every
15 s. `--keep-server-log FILE` copies the server's log out before the folder
is deleted.

Seeding takes a few seconds per Library (several Libraries are seeded in
parallel); the server then needs a few seconds to start, because it prepares
every Library at startup.

## Remote mode

Measures a server that is already running. The tokens file has one line per
Library with one or more whitespace-separated device tokens of that Library;
the first two are the two always-open clients (one token is used for both if
there is only one), further tokens are used for the long polls. `#` starts a
comment.

```sh
python tools/loadtest/loadtest.py --base-url http://127.0.0.1:8080 --tokens tokens.txt
python tools/loadtest/loadtest.py --base-url http://127.0.0.1:8080 --tokens tokens.txt --libraries 5
```

Before the load starts every token is checked once; a token the server
refuses stops the run, because repeated failed authentications lock the
caller's address out.

Remote mode changes the Libraries it is given: every upload stays in the
Library's object store, and every build request queues a real build, which is
cancelled a few seconds later. Use Libraries made for testing.

There is no monitor inside a remote server. The worst event-loop stall is
therefore **estimated**: a probe requests `GET /v1/health` every 100 ms on its
own connection, and the tool reports its maximum and 99th percentile latency
minus its median. That route runs in a worker thread, so the figure also
contains the time spent waiting for a free thread; read it as an upper bound.
`--health-probe` adds the same probe to a local run, to compare the estimate
with the measured value.

## Workload

Per Library; every number is an option, and an interval of 0 turns that part
off. Real clients are slower than the defaults (a real poll interval is 20 s);
the defaults compress time so that a short run means something. Start times
are spread at random (`--seed` makes them repeatable).

| Part | Requests | Defaults |
| --- | --- | --- |
| Polls | each of the 2 clients: `GET /v2/sync/state` | `--poll-interval 2` |
| Listings | each client, one request per interval, in turn: `GET /v2/library`, `GET /v1/render-state?target=all`, `GET /v1/library/projects`, `POST /v2/sync/plan` | `--listing-interval 5` |
| Long polls | `GET /v1/agent-notes/replies?wait=W&after=cursor`, re-issued as soon as it returns | `--long-polls 5`, `--long-poll-wait 10` |
| Uploads | one client: a new random blob to `POST /v1/objects` with `X-Content-SHA256`, streamed in chunks | `--upload-interval 15`, `--upload-bytes 4M` (at most 100M), `--upload-chunk-bytes 64K`, `--upload-rate-bytes-per-second 0` (not throttled) |
| Builds | one client: `POST /v1/builds` with a fresh `request_id`, `GET /v2/builds/{id}?wait_ms=W`, `POST /v1/builds/{id}/cancel` | `--build-interval 20`, `--build-wait-ms 5000`, `--build-target auto` |
| Canary | `GET /v1/device-sequence` on a connection of its own | `--canary-interval 0.25` |

Details:

* `--conditional`: once a poll answered with an `ETag`, the client sends
  `If-None-Match`; the `statuses` column then shows 200 and 304 separately.
  Off by default, as today's clients do not send it.
* The Sync plan is the one of a client that is up to date: each client asks
  for a first plan before the warm-up, and afterwards sends the objects it
  knows, its sequence and its Library revision.
* The long polls start with `after=0` and continue with the cursor the server
  returns, so a Library that already has replies does not answer in a loop.
* Build targets: with `auto` each build targets another document of the
  Library that is built from sources (read once from `GET /v2/library`), or
  `all` if there is none. Without a build worker the job stays `queued`, so
  the wait lasts `wait_ms`. A 429 or 409 to `POST /v1/builds` (a cap on queued
  builds) is counted and the client carries on. Builds still queued when the
  run ends are cancelled.
* A request that takes longer than `--request-timeout` (60 s) is counted as
  `timeout`. A slow answer delays that client's next request; it does not make
  the client send a burst afterwards. So when the server cannot keep up, the
  request counts drop: compare them with what the intervals should produce.

## Output

The configuration is printed first, then one table per run:

```
route                              count  errors     p50 ms     p95 ms     max ms  statuses
GET /v2/sync/state                    30       0       32.6      136.0      144.0  200x30
...
GET /v2/builds/{id}?wait_ms            1       0     (wait)     (wait)     (wait)  200x1
GET /v1/agent-notes/replies?wait      15       0     (wait)     (wait)     (wait)  200x15
GET /v1/device-sequence (canary)     120       0        6.0       16.0       28.9  200x120
```

* `errors` counts answers other than 2xx and 304; `statuses` lists every
  status seen. Besides HTTP codes it can contain `timeout`, the name of a
  transport error (for example `ReadError`), and `unfinished` for a request
  that was still open 30 s after the run ended (its time so far is in the
  percentiles).
* A request belongs to the measured window when it started in it. Requests
  under way at the end are awaited, so slow answers are measured in full.
* Long polls and build waits last as long as the client asked, so their rows
  have no latency. Instead the **overshoot** is reported: duration minus the
  requested wait, for those that ended inside the window after waiting the
  full time. One that is still open more than a second past its wait when the
  run ends is counted with the time it has overshot so far.
* **worst canary**: the slowest canary request, when it was sent (seconds
  after the window started), for which Library, and how many uploads, build
  waits and requests in all the load generator had in flight when it was sent
  and when it was answered.
* **worst event-loop stall / p99 loop lag** (local mode): from the monitor
  inside the server, a task that sleeps 10 ms in a loop and records how late
  it woke up. The three worst stalls of at least 50 ms are listed with the
  routes answered during the stall and those in flight right after it.
* **server RSS**, maximum thread count, open file descriptors and the maximum
  number of requests in flight (local mode). RSS is the resident size at the
  start and at the end of the window; on macOS it can shrink under memory
  pressure.
* **server CPU** (local mode): the CPU time the server process used in the
  window, as a share of one core, split into user and system time. When the
  process is saturated the latencies are mostly time spent queueing, and the
  request counts fall below what the intervals ask for. A high system share
  means the threads are handing the interpreter lock to each other rather than
  working (see the note on sqlite below).
* **load generator**: its own event-loop lag, to check that it was not the
  bottleneck, and the machine's load average. In local mode the server and the
  load generator share the machine: run on a quiet one.
* **requests/s total**: all requests of the table divided by the measured
  seconds.

`--json-out FILE` writes the same numbers, plus the 99th percentiles and means,
the seed summary and the error lines of the server's log, as JSON.

## Seeded data

Per Library, in local mode (`--seed-publications 120`, `--seed-builds 400`,
`--seed-operations 3000`, `--seed-sources 200`, `--seed-projects 8`,
`--seed-project-pdfs 3`):

* an operation log: revisions of LaTeX source files and comments on
  documents, with the source files and comment entities they lead to;
* publications: three fifths are documents built from sources, each with a
  PDF, a source package and a reverse lookup object that the Sync plan
  resolves; the rest are imported PDFs. All PDFs are small valid PDFs of a few
  kilobytes;
* past builds (succeeded, failed, cancelled), and a current successful build
  for most documents, so that most read as up to date and a few as changed;
* Projects: each imports some of the PDFs and connects one source folder with
  its documents.

Rows are inserted directly with the server's SQLAlchemy models and their bytes
stored in the object store. Projects are made through the Library's own HTTP
API, served in process, because the server derives their files, document
identities and readings itself.

With this data every route of the workload answers 200. The limits of the
synthetic Libraries:

* The database is sqlite, one file per Library; production uses PostgreSQL.
  Absolute times are not production's, and lock behaviour differs. In
  particular Python's sqlite3 module releases the interpreter lock for every
  row it fetches. Once many threads are runnable, each row then costs a
  hand-over of the lock, which shows as system CPU. A saturated local run
  therefore degrades more steeply than the same load on PostgreSQL would,
  and with the default (time-compressed) intervals 20 seeded Libraries can
  be more than one process serves. To compare versions below saturation, use
  real intervals, for example
  `--poll-interval 20 --listing-interval 30 --canary-interval 1`.
* Only comments are seeded as review data: no annotations, review cards,
  proposals or HTML editions. Listings of a real Library carry more per
  document.
* No worker runs, so nothing is built and no PDF changes during a run. The
  Library revision still changes whenever a build is queued or cancelled, as
  it does in production.
* A build that the load test queues and cancels leaves its document in the
  "latest build failed" state for the rest of the run.

## Files

* `loadtest.py`: command line, local-mode orchestration, the report.
* `workload.py`: the simulated clients and what they record.
* `seed.py`: the generated Libraries.
* `local_server.py`: the server subprocess with the event-loop monitor.

## Idle cost of a Library

`idle_cost.py` starts, for an empty Library on sqlite, each of a Library's four
worker processes and the API with 1 and with N Libraries, lets them idle and
prints their resident memory, threads and the database connections their pools
keep open (plus the LISTEN connection each API Library and build worker holds
on PostgreSQL), and the same for a build worker with
`THOUGHTS_WORKERS_ON_DEMAND=true`.

```sh
PYTHONPATH=src python tools/loadtest/idle_cost.py --libraries 5 --seconds 6
```
