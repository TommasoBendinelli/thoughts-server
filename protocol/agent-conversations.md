<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# Agent conversations

A Claude Code or Codex conversation linked to a Library Project. Entity type `agent_conversation`, collection `agent-conversations`, one record per (Project, conversation), last writer wins. Servers advertise `agent_conversations_v1`; clients hold these operations until the capability is listed, because an older server rejects the whole batch. Section numbers follow the feature spec (sections 2 and 3).

## 2. Record format (normative, shared by all writers)

Operation envelope (`POST /v1/operations`, the same as every other entity):

```
entity_type   = "agent_conversation"
collection    = "agent-conversations"
entity_id     = payload.link_id
base_revision = payload.revision - 1
client_timestamp = payload.updated_at
```

Payload, `schema_version` 1:

| Field | Type / format | Rule |
|---|---|---|
| `schema_version` | int | `1` |
| `link_id` | 64 lowercase hex | `sha256_hex(utf8(project_id + "\n" + agent + ":" + conversation_id))`. One record per (project, conversation), so two devices can never create duplicates. |
| `project_id` | string, 1–512, no control chars (U+0000–U+001F, U+007F) | The Project `id` exactly as in the Library catalogue, e.g. `new:8b964c95-8bc0-4045-a38e-015d2172d7ca` or `source:docs-concepts`. Rename-safe. |
| `agent` | `"claude"` \| `"codex"` | |
| `conversation_id` | `^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$` | Claude: the local `sessionId` (the same value proposals carry as `conversation_id`). Codex: the thread UUID. |
| `conversation_url` | string | `""` when unknown. Otherwise it must pass the proposal rule (agent-proposals spec §3.6): **claude** ⇒ `https://claude.ai/code/session_<1-64 [A-Za-z0-9]>`; **codex** ⇒ exactly `codex://threads/` + `conversation_id`. |
| `title` | string, 1–200 UTF-16 units after trim | Single line: no `\n`, `\r` or control chars. Writers replace newlines and tabs with a space and collapse whitespace runs. |
| `title_source` | `"agent"` \| `"app"` \| `"prompt"` \| `"fallback"` | Rank agent 3 > app 2 > prompt 1 > fallback 0 (§3.2). |
| `session_name` | string, 0–100 | Claude's local session name (e.g. `alex-94`; the user refers to sessions by it). `""` for Codex. |
| `linked_by` | `"agent"` \| `"tool"` \| `"backfill"` | How the record was **first** created. Copied unchanged by every later write. |
| `last_tool` | `^[a-z_]{0,64}$` | The MCP tool of the latest automatic touch (`create_proposal`, …). `"link_conversation"` for explicit links. `""` for backfill. |
| `device_name` | string, 0–128 | Host name of the computer that last touched it (`QSysInfo::machineHostName()`). |
| `created_at` | RFC 3339 UTC, ms, `Z` | First link. Never changed afterwards. |
| `last_active_at` | same | Latest touch. Monotonic: writers use `max(stored, now)`. |
| `updated_at` | same | Time of this write, strictly greater than the stored `updated_at` (rule of `PdfLinkOverrides::timestamp(now, notBefore)`). |
| `revision` | int ≥ 1 | stored revision + 1; 1 on create. |
| `origin_id` | string, 1–128 | Writer's origin: desktop `Thoughts::Platform::desktopOriginId()`; Android uses the origin its link overrides and feedback use. |
| `removed` | bool (strict) | The user removed it from the Project. Hidden everywhere. The record is kept, not deleted (§3.3). |
| `removed_at` | RFC 3339 or `null` | Non-null if and only if `removed` is true. |
| `deleted` | bool (strict), default `false` | Reserved for a future purge. Clients treat `deleted: true` as absent. v1 clients never write it. |

- Unknown fields must be preserved by every writer: copy the stored payload, then change the known fields.
- Timestamp format written by all clients: `yyyy-MM-ddTHH:mm:ss.SSSZ` (UTC).

### Test vectors (`link_id`)

```
project_id "source:docs-concepts", agent "claude", conversation_id "5ef4cd0f-6a8a-5b7a-ac7b-1e9f51cd3139"
  → ef8e44d466d11197fa75b69c419f74b574068655d7b0dd3ffeced7c341db3f4d
project_id "new:8b964c95-8bc0-4045-a38e-015d2172d7ca", agent "codex", conversation_id "01900000-0000-7000-8000-000000000002"
  → e05cfb424a6849e70f4fc8ad7eb8ba99a5c6c25da759ccf97fae0a9aadca6679
```

### Example payload

```json
{
  "schema_version": 1,
  "link_id": "ef8e44d466d11197fa75b69c419f74b574068655d7b0dd3ffeced7c341db3f4d",
  "project_id": "source:docs-concepts",
  "agent": "claude",
  "conversation_id": "5ef4cd0f-6a8a-5b7a-ac7b-1e9f51cd3139",
  "conversation_url": "https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv",
  "title": "Thoughts search on mobile",
  "title_source": "app",
  "session_name": "alex-94",
  "linked_by": "tool",
  "last_tool": "create_proposal",
  "device_name": "Alexs-MacBook-Pro",
  "created_at": "2026-10-26T11:02:13.511Z",
  "last_active_at": "2026-10-26T11:40:02.004Z",
  "updated_at": "2026-10-26T11:40:02.004Z",
  "revision": 3,
  "origin_id": "desktop-…",
  "removed": false,
  "removed_at": null,
  "deleted": false
}
```

## 3. Write rules

### 3.1 Conflict resolution
- The server uses the generic path in `operations.py`: priority `(revision, updated_at, origin_id, operation_id)`. Higher wins, and a lower one is `superseded`. That never returns 409 and never blocks Sync.
- Clients do not merge. After Sync they show what the server holds.

### 3.2 Touch (desktop only; it is the only writer that links)

Input: target Project, `agent`, `conversation_id`, `conversation_url`, `title`, `title_source`, `session_name`, `linked_by` ∈ {agent, tool, backfill}, `tool`.

**No stored record** → create it:
- `revision` 1; `created_at` = `last_active_at` = `updated_at` = now.
- `removed` false, `removed_at` null.
- `last_tool` = tool (`"link_conversation"` when `linked_by` is agent; `""` for backfill).

**Stored record with `removed: true`:**
- If `linked_by` is tool or backfill, write nothing. Return `written: false, removed: true`. Removal is sticky against automatic matching.
- If `linked_by` is agent (an explicit `link_conversation`), revive it: `removed` false, `removed_at` null, and apply the field updates below.

**Stored record, not removed** → build the candidate. All other fields are copied, including unknown ones.
- `title`/`title_source` are replaced if `rank(new) > rank(stored)`, or if `rank(new) == rank(stored)`, `new.title_source ∈ {agent, app}` and the text differs.
- `conversation_url`: take a non-empty incoming value. Never replace a non-empty value with `""`.
- `session_name`: take a non-empty incoming value.
- `last_active_at` = max(stored, now); `last_tool`, `device_name` = incoming.

**Throttle:** write only if title, `conversation_url`, `session_name` or `removed` changed, or if stored `last_active_at` is ≥ 600 s old. Otherwise return `written: false`.

### 3.3 Remove (desktop and phone)
- Copy the stored record, then set `removed` true, `removed_at` = now, `revision` + 1, and `updated_at` and `origin_id` as usual. Leave `last_active_at` unchanged.
- There is no Restore UI in v1. An explicit `link_conversation` from the agent revives the record.
- **Known race (accepted):** the phone removes a row while the Mac, not yet having pulled that removal, auto-touches the same record at the same revision. Then the later `updated_at` wins and the row may reappear. The 600 s throttle keeps this rare, and removing it again settles it.

