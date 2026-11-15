<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Push to the phones (`push_fcm_v1`)

Status: version 1 (2026-11-14). Capability `push_fcm_v1`.

The server sends the phone a content-free Firebase Cloud Messaging (FCM) push whenever a new agent note
becomes available, so the phone shows notes at once without keeping the inbox long poll ("study mode",
protocol/agent-notes.md "Phone") running. The push only says "there is something new"; the phone then asks
the server's inbox for it, exactly as the long poll and the periodic job do.

## Where each piece lives

| Piece | Repository / path |
| --- | --- |
| Endpoints, OAuth, sender | thoughts-server `src/thoughts_server/push.py`, hooked into `AgentNoteService.create_note` (`agent_notes.py`) and `create_app` (`api.py`) |
| Storage | model `PushRegistration` in `models.py`, migration `alembic/versions/0033_push_registrations.py` |
| Settings | `fcm_credentials_file`, `fcm_endpoint` in `config.py` |
| Tests | `tests/test_push.py` (a throwaway RSA key, a fake OAuth + FCM server) |
| Phone | thoughts-android: FCM token registration, the ping handler that asks the inbox |

## Privacy rule

A ping carries no content: no note title, message, id, document, card, device or sender, only the kind of
ping and the time it was made. Google sees that the phone got a ping, never what it is about. The phone
fetches the note from the Thoughts server itself, with its own device token, over the usual endpoints. The
server never logs a registration token (it logs the first 8 hex digits of the token's SHA-256) and never
returns one.

## Capability and configuration

Health (`GET /v1/health`, `GET /v1/ready`) lists `push_fcm_v1` only when the FCM credentials loaded when the
API started. Without it the endpoints below still exist: registrations are stored, the status says
`"push": "off"`, and nothing is sent.

The credentials are a Google service-account JSON key of the Firebase project (Firebase console, Project
settings, Service accounts, Generate new private key). The server reads `project_id`, `client_email`,
`private_key` (an unencrypted PEM RSA key), `private_key_id` (the JWT's `kid`, when present) and
`token_uri` (default `https://oauth2.googleapis.com/token`); `type`, when present, must be
`service_account`.

* `THOUGHTS_FCM_CREDENTIALS_FILE`: the path of that file. When unset, `/etc/thoughts/fcm-service-account.json`
  is used if it exists. The file is read once at startup.
* `THOUGHTS_FCM_ENDPOINT`: the FCM origin, default `https://fcm.googleapis.com` (tests point it, and the
  credentials' `token_uri`, at a fake server).

A file that is missing, unreadable or invalid turns push off; the API starts normally and logs one line at
startup, e.g. `push_fcm_v1 off (library default): FCM credentials /etc/thoughts/fcm-service-account.json:
private_key is missing` (the reason never quotes a value of the file). With valid credentials the line is
`push_fcm_v1 on (library default): FCM project <project_id>, credentials <path>`. Without a file:
`push_fcm_v1 off (library default): no FCM credentials file (...)`. One server serving several Libraries
uses the same credentials for each and logs one line per Library.

On a server installed with `deploy/fedora` (the API runs as user `thoughts` with `ProtectSystem=strict`, so
`/etc` is readable):

```
install -o root -g thoughts -m 0640 <downloaded key>.json /etc/thoughts/fcm-service-account.json
systemctl restart thoughts-api
journalctl -u thoughts-api | grep push_fcm_v1     # "push_fcm_v1 on ..."
```

No variable is needed for the default path. The key is a secret like `server.env`: never commit it or copy
it into a Library.

## Endpoints (`Authorization: Bearer <device token>`, JSON)

Every endpoint uses the device token like every other `/v1` call; a missing or wrong token is the usual
`401`. Errors are `{"detail": "...", "error_code": "..."}`: `invalid_push_registration` (`422`: not JSON, a
wrong provider, a missing or too long field), `push_request_too_large` (`413`: a body above 16 KiB),
`push_off` (`409`).

1. `PUT /v1/push/registration` — `{"provider": "fcm", "token": "<FCM registration token>", "app":
   "org.tbend.thoughts"}`. `200` `{"registered": true}`, or `{"registered": true, "push": "off"}` while push
   is off (the registration is stored all the same). One registration per device, provider and app: a new
   token of the same app replaces the old one (its last result is cleared), another app of the same device
   is a second registration. A token registered by another device (or for another app) moves to this
   device and app: no two registrations hold the same token. `provider` must be `"fcm"`; `token` 1 to 4096
   and `app` 1 to 200 printable ASCII characters without spaces; other fields are ignored. The phone calls
   it whenever it gets a token (start, `onNewToken`), so a token FCM dropped comes back.
2. `DELETE /v1/push/registration` — optional body `{"provider": "fcm", "token": "..."}`: removes the
   caller's registration with that token (another device's token is not removed), or with only `provider`
   every one of that provider; without a body every registration of the caller. `200` `{"removed": n}`.
3. `GET /v1/push/status` — `{"push": "on" | "off", "project_id": "<Firebase project>" | null,
   "registrations": [{"device_name", "app", "registered_at", "last_sent_at", "last_result",
   "this_device"}]}`: every registration of the Library's enabled devices, oldest first. `last_result` is
   `"ok"`, `"error: <short>"` (e.g. `"error: 403 SENDER_ID_MISMATCH"`, `"error: unreachable"`, `"error:
   oauth 400 invalid_grant"`) or null before the first send; `last_sent_at` is when that result came. A
   registration FCM no longer knows is deleted (see below), so `"unregistered"` is never listed. Tokens are
   never returned.
4. `POST /v1/push/test` — sends one ping with `kind` `"test"` to each of the caller's own registrations now,
   one try each, and answers when FCM has: `200` `{"sent": n, "results": [{"app", "registered_at",
   "result": "ok" | "unregistered" | "error: <short>", "http_status": 200 | null}]}`, where `sent` is the
   number FCM accepted. A caller without registrations gets `{"sent": 0, "results": []}`. `409`
   `{"push": "off", "detail", "error_code": "push_off"}` while push is off. This is how a real phone is
   checked end to end.

## When a ping is sent

Whenever a new agent note becomes available to the phones: the moment `POST /v1/agent-notes` commits a new
note (status `201`), which is the moment it appears in the inbox of every other device. Every note goes
through that request: notes from `send_note`, answers to replies, the phone's pages (`origin.kind`
`phone_page`), anything else. An identical retry (`200`) sends nothing.

Each such note pings every registration of every enabled device except the device that created the note
(the inbox shows a device every note but its own). Notes within 2 seconds of the first one are coalesced:
one ping per registration at the end of that window, sent to each registration whose device created at
least one of those notes not itself. A note later than that starts a new window. When the API stops, a
window still open is sent at once.

Sending never blocks or fails the request that created the note: the request only records that a note
arrived, and a background thread of the API (one per Library) sends. The API is one process
(`thoughts-server`), and notes are created only by its requests, so the thread sees every note.

## FCM message

`POST {THOUGHTS_FCM_ENDPOINT}/v1/projects/{project_id}/messages:send`, one per registration:

```json
{"message": {"token": "<registration token>",
             "data": {"kind": "agent_notes", "at": "2026-11-14T18:00:00Z"},
             "android": {"priority": "HIGH", "ttl": "3600s", "collapse_key": "agent_notes"}}}
```

Data only, no `notification` block. `at` is when the ping was made (UTC, seconds). A test ping has `kind`
and `collapse_key` `"test"`. The phone treats any ping as "ask the inbox now"; it needs nothing from the
ping itself.

## OAuth

The server signs a JWT with the service account's key (RS256; header `{"alg": "RS256", "typ": "JWT",
"kid": <private_key_id>}`; claims `iss` = `client_email`, `scope` =
`https://www.googleapis.com/auth/firebase.messaging`, `aud` = `token_uri`, `iat`, `exp` = `iat` + 1 h) and
exchanges it at `token_uri` (`grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer`, `assertion=<JWT>`)
for an access token, which it uses until 60 s before it expires. A `401` from FCM drops the access token and
tries once more with a new one.

## FCM's answers

* `200`: `last_result` `ok`.
* `404` with FCM's error answer, or error code `UNREGISTERED`, or `INVALID_ARGUMENT` about the token (a
  `message.token` field violation, or a message that names the token): the registration is deleted (the
  test answers `"unregistered"`). The phone registers again with its next token. A bare `404` without FCM's
  error body (a wrong endpoint, a proxy) is `error: 404` and deletes nothing.
* `429` and `5xx`, and no answer at all (network): tried again after 10 s and 40 s, or after FCM's
  `Retry-After` (at most 60 s), three tries in all; then `error: <status> <code>`. A newer ping to the same
  registration replaces a retry still waiting. The test does not retry.
* Anything else: `error: <status> <code>`, e.g. `error: 403 SENDER_ID_MISMATCH` (a token of another Firebase
  project) or `error: 400 INVALID_ARGUMENT` (not about the token); the registration is kept.

Each send is logged as one line `{"event": "push_fcm_send_v1", "library", "kind", "token": "<8 hex>",
"attempt", "result", "http_status", "retrying"}`, and each registration as `{"event":
"push_registration_v1", "library", "device_id", "app", "token": "<8 hex>", "moved"}`.

## Storage (migration 0033_push_registrations)

`push_registrations`, in each Library's database (the API's `ExecStartPre` migrates every Library):

| column | type | notes |
| --- | --- | --- |
| id | integer PK | |
| device_id | FK devices.id, ON DELETE CASCADE | the registering device; its registrations go with it |
| provider | string(16) | `fcm` |
| app | string(200) | the app id the phone sent |
| token | text | the registration token |
| token_sha256 | string(64) unique | the token is unique by its hash (a 4096-character token is too long for a PostgreSQL index entry) |
| registered_at, updated_at | timestamptz | first registration of this device and app; last `PUT` |
| last_sent_at, last_result | timestamptz null, string(64) null | the last send's outcome |

Unique (`device_id`, `provider`, `app`). The table is new and read by nothing older, so the migration may run
while the previous version still serves; a server without the branch ignores it.
