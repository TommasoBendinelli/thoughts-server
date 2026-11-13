<!-- SPDX-License-Identifier: GPL-2.0-or-later -->
<!-- SPDX-FileCopyrightText: 2026 Tommaso Bendinelli -->

# thoughts-cli

A headless, terminal-only Thoughts client that creates and revises agent proposals
(protocol/agent-proposals.md section 8) as an ordinary sync client, the way the Mac does with
`thoughtsctl`. The server stays a plain sync server: the client reads the source through the
public endpoints, writes the `\agentProposedEdit` wrappers (Markdown: `thoughts:agent-proposal:v2`
blocks) itself, byte for byte as the desktop does, and publishes the new source like any device.
It can also add a new note to a Library Project (`create-document`, below); it never writes over
an existing source. Like the Mac, it publishes the new PDF's build inputs, without which Fedora
cannot build it (`publish-build-inputs`, below). It also makes review cards from a source
selection and moves a card on an imported PDF into a note (`create-review-card`,
`move-review-card`, below), marking the passage in the source as the desktop does, and has the
server merge review cards into one (`merge-review-cards`, below). A move onto a pending proposal's
proposed text waits for the proposal: the server makes it when the user accepts it
(`pending-card-moves`, below).

Pure standard library, Python 3.11 or later; runs with the system `python3`, no install needed.

| File | Role |
|---|---|
| `thoughts-cli` | entry script (`./thoughts-cli COMMAND`); `pip install .` also installs a `thoughts-cli` command |
| `thoughts_cli/authoring.py` | the desktop's request checks, refusal codes and writers (`shell/proposalauthoring.cpp`), moved here from the server's former `proposal_authoring.py` |
| `thoughts_cli/proposal_syntax.py`, `markdown_proposals.py` | the server's proposal parsers, copied unchanged (a test keeps them identical to `src/thoughts_server/`) |
| `thoughts_cli/client.py` | read, check, author, publish; the request records; create-document |
| `thoughts_cli/documents.py` | create-document's request checks and its Project lookups in the catalogue |
| `thoughts_cli/build_inputs.py` | a note's PDF build inputs as the desktop declares them (publish-build-inputs) |
| `thoughts_cli/review_cards.py` | create-review-card and move-review-card: the desktop's request checks and the card records |
| `thoughts_cli/review_card_selection.py` | the selection rules, the card markers and a target on a pending proposal's text, copied unchanged from the server (a test keeps them identical) |
| `thoughts_cli/card_merges.py` | merge-review-cards: the desktop's request checks; the server merges (`POST /v1/review-card-merges`) |
| `thoughts_cli/card_pending_moves.py` | a move onto a pending proposal (`POST /v1/review-card-pending-moves`), pending-card-moves, cancel-pending-card-move |
| `thoughts_cli/api.py` | the public endpoints (`urllib`) |
| `thoughts_cli/state.py` | `requests.json`, the request records |
| `thoughts_cli/cli.py` | the command line |

## Commands

Requests and results are `thoughtsctl`'s: one JSON object on stdin **without** a `command`
field, one JSON object on stdout. The request fields may instead be given as options
(`--source`, `--expected-source-sha256`, `--start-line`, `--end-line`, `--replacement` or
`--replacement-file`, `--explanation` or `--explanation-file`, `--proposal-id`,
`--proposals-file`, `--conversation-id`, `--conversation-url`, `--request-id`; for create-document
`--project`, `--name`, `--filename`, `--content` or `--content-file`; for publish-build-inputs
`--document-id`, or `--project` and `--filename`; for the review cards `--question`, `--due-at`
and `--card-id`; for merge-review-cards `--card-id` once per card and `--keep-card-id`); then
stdin is not read.

```sh
export THOUGHTS_SERVER_URL=http://127.0.0.1:8080
export THOUGHTS_TOKEN_FILE=~/connectors/thoughts-token

thoughts-cli read-source --source docs-x/tex/source/intro.tex           # text, source_sha256, source_revision
thoughts-cli create-proposal < request.json                            # one addition, replacement or deletion
thoughts-cli create-proposals < batch.json                             # several, against one source version
thoughts-cli revise-proposal < revise.json                             # new text and explanation of a pending one
thoughts-cli create-document < document.json                           # a new note in a Library Project
thoughts-cli publish-build-inputs --document-id ID --publish --request-id UUID  # inputs of a note made earlier
thoughts-cli create-review-card < card.json                            # a card whose answer is a source passage
thoughts-cli move-review-card < move.json                              # a card on an imported PDF into a note
thoughts-cli merge-review-cards < merge.json                           # several cards of one note into one card
thoughts-cli pending-card-moves --card-id UUID                         # card moves waiting for a proposal
thoughts-cli cancel-pending-card-move --pending-move-id UUID           # the card stays where it is
thoughts-cli move-pdf --pdf KEY --project NAME --request-id UUID       # an imported PDF to another Project
thoughts-cli build-status --document NAME                              # why a PDF fails, which proposal to blame
```

- `create-proposal`: `source`, `expected_source_sha256`, `start_line`, `end_line` (one-based,
  end exclusive; equal boundaries insert), `replacement`, `explanation`, `conversation_id`,
  `request_id`, optional `conversation_url`.
- `create-proposals`: the same without the four edit fields, plus `proposals`: 1 to 500 objects
  of exactly `start_line`, `end_line`, `replacement`, `explanation`, all in lines of the version read.
- `revise-proposal`: `source`, `expected_source_sha256`, `proposal_id`, `replacement`,
  `explanation`, `conversation_id`, `request_id`.
- `read-source`: `source`, optional `from_line`, `max_lines`.
- Figures (protocol/agent-proposals.md section 11): `figures`, a list of `{name, svg}`, on each
  entry of `create-proposals` and on `create-proposal` and `revise-proposal`. Each becomes
  `<folder of the note>/agent-figures/<name>.svg` in the same publication; the replacement must
  reference it as `agent-figures/<name>` (LaTeX `\includegraphics`) or `agent-figures/<name>.svg`
  (Markdown). The result lists them under `figures`; bad figures are refused with `invalid_figure`.

Results carry the desktop's fields (`state` `proposal_created` / `proposals_created` /
`proposal_revised`, `source`, `source_sha256`, `previous_source_sha256`, `proposal_id`,
`created_at`, `original_text`, `replacement`, `explanation`, `line`, `conversation_id`,
`agent_conversation_id`, `request_id`, `replayed`, `warnings`) plus the publication:
`source_revision`, `source_frontier`, `publication_id`, `publication_outcome` (`applied` or
`merged`), `device_id` and `render_check`. Refusals are the desktop's error object
(`accepted: false`, `state: failed`, `error_code`, `message`; `current_source_sha256` for
`source_conflict`; `reason` for `invalid_proposal`; `proposal_id` for issue codes):
`invalid_request`, `invalid_range`, `invalid_proposal`, `invalid_source`, `generated_source`,
`no_change`, `proposal_overlap`, `duplicate_id`, `source_conflict`, `request_conflict`,
`busy`, `read_failed`, `write_failed`, and the issue code of a named proposal
(`unknown_id`, `nested_proposal`, ...). `transport_error` (and `write_failed` for a server
error) carry `retryable: true`. Exit status as `thoughtsctl`: 0 success, 2 invalid request,
3 not configured or no answer, 4 refused, 6 busy.

## Review cards

`create-review-card` and `move-review-card` take the desktop's requests (`thoughtsctl`,
`shell/reviewstore.cpp`) and answer with its results:

- `create-review-card`: `source`, `expected_source_sha256`, `start_line`, `end_line` (one-based,
  end exclusive, whole lines), `question` (1 to 500 characters), `due_at` (ISO 8601 with a
  timezone, now or later), `request_id`. The request ID is the new card's ID, so an uncertain
  retry never makes a second card. Result: `state: card_created`, `card_id`, `document` (the
  note's PDF filename), `document_id`, `source`, `source_sha256` (the marked source), `due_at`,
  `source_link_state: linked`, `pdf_placement_state: pending`, `card_revision`, `render_check`.
- `move-review-card`: `card_id`, `source`, `expected_source_sha256`, `start_line`, `end_line`,
  `request_id`. Only a card without source markers moves this way (a card on an imported PDF);
  it keeps its ID, question, schedule and review history. Result: `state: card_moved` and
  `from_document`. A card with markers is refused with `linked_card`: it moves by cutting and
  pasting its marked passage, as on the desktop. When the lines are proposed text of a pending
  agent proposal, the move waits for it (below): result `state: waiting_for_proposal`.

The source must be the main source of exactly one Library document (`GET /v1/library/projects`,
its `source_file`); otherwise `document_unavailable` or `ambiguous_document`. The selection rules
are the desktop's (`sourceSelectionError`): complete UTF-8 lines with text, outside pending agent
proposals and other cards' passages; in Markdown, complete top-level blocks, never inside HTML,
front matter or a block quote, and a list only whole. LaTeX selections must also lie in the
document body, contain text that is not a comment, close every group and environment they open,
and lie only inside text environments (lists, quotations, centering); figures, tables, math and
other environments are taken whole or not at all. A refused selection answers `invalid_selection`.

The client writes the card's marker pair around the selection (`\ReviewCardMaskStart{card}{0}%` /
`\ReviewCardMaskEnd{card}{0}%`, Markdown `<!-- thoughts:v1:start review:card:answer:0 -->` /
`end`), uploads the source before and after, and sends the card as one `review_card` operation
(`POST /v1/operations`) whose `source_change` names both versions, exactly as the desktop's Sync
does (`client_review_sources.py`; capabilities `client_review_sources_v1`, and for a move
`client_review_source_replacement_v1` and `client_review_card_move_v1`, read from
`GET /v1/health`). The server applies the marked source and the card in one transaction and checks
that removing the markers restores the source exactly; a refusal writes neither. The card's PDF
areas are placed by the next build, which the client asks for like after a proposal
(`render_check`). A lost answer is resent with the same operation ID (the server answers
`duplicate`), a taken device sequence is renumbered, and a source change since the read refuses
with `source_conflict`.

### Merging cards

`merge-review-cards` takes the desktop's request (`thoughtsctl merge-review-cards`): `card_ids`
(2 to 20 different cards of one document whose passages are marked in `source`), `source`,
`expected_source_sha256`, `request_id`, and optionally `keep_card_id` (default: the card with the
most reviews) and `question` (1 to 500 characters; default: the cards' questions joined in reading
order). The kept card keeps its ID, schedule and review history and marks every card's passage as
its own segment, in file order; text between the passages stays unmarked; the other cards are
deleted with their markers.

The desktop merges on the Mac and sends the records through its outbox; this client has none, so
the server merges (`POST /v1/review-card-merges`, capability `review_card_merge_v1` read from
`GET /v1/health`, otherwise `server_unsupported` and nothing is sent; protocol/review-card-merge.md).
It makes the desktop's records and applies them in one transaction on the canonical source. Result:
`state: merged`, `kept_card_id`, `deleted_card_ids`, `delete_event_ids`, `card_order` (reading
order), `question`, `source`, `source_sha256` (the merged source), `passages`, `card_revision`,
`document`, `render_check` (the rebuild that places the kept card's areas). Refusals keep the
server's code: `merge_refused` (with the reason: cards of several documents or files, a PDF-only
card, cards on different PDF versions, ...), `card_unavailable`, `invalid_source`,
`source_conflict` (with `current_source_sha256`), `request_conflict`. The server answers a retry of
the same `request_id` from its receipt (`replayed: true`), so a lost answer (`transport_error`,
`retryable: true`) is retried with the identical request and never merges twice.

### Moving a card onto a pending proposal

A card cannot be marked on proposed text: until the user accepts the proposal it is only an offer.
When the selected lines of `move-review-card` lie inside one pending proposal's proposed text (they
may also hold the wrapper's own `}{` before it and its closing `}` or end comment after it, never its
explanation or original text) and the server has `review_card_pending_moves_v1`, the client asks
the server to wait for the proposal (`POST /v1/review-card-pending-moves`,
protocol/review-card-pending-moves.md) and answers `state: waiting_for_proposal`, `terminal: false`,
`pending_move_id` (the request ID), `proposal_id`, `document`, `from_document` and `target_text` (the
lines as they read once accepted). Nothing changes yet. The selection is checked on the source with
that proposal accepted, with the rules above.

When the user accepts the proposal, the server moves the card exactly as `move-review-card` would
move it onto the accepted text; when the proposal is rejected or withdrawn, or revised so that the
target's text is gone, or when the card is deleted, moved or linked another way first, the move is
dropped and the card stays where it was. Without the capability the selection is refused as before
(`invalid_selection`). A retry with the same `request_id` is answered from the server's record
(`replayed: true`, `pending_move` with its current state), also after the proposal was decided.
Other refusals keep the server's code: `pending_move_exists` or `target_taken` (with
`pending_move_id`), `document_unavailable`, `same_document`, `card_unavailable`, `request_conflict`.

- `pending-card-moves`: optional `card_id`, `proposal_id`, `state` (`waiting_for_proposal`, the
  default, `moved`, `dropped`, `cancelled` or `all`). Result: `pending_moves`, each with its
  `state`, `reason` and `message` once settled (for example `proposal_rejected`, `card_deleted`) and,
  once moved, `result` (the moved card's `source_sha256` and `card_revision`).
- `cancel-pending-card-move`: `pending_move_id`. Result: the move, `state: cancelled`
  (`already_settled: true` for one already dropped or cancelled); a move already made is
  `already_moved`.

## Creating a document

`create-document` adds a new Markdown or standalone LaTeX note to a Library Project, as "New
document" does on the desktop and Android (docs/default-folder-documents.md, "Library"). It only
creates: a path that exists is refused, and existing notes change only through proposals.

Request (stdin may hold up to 24 MiB here, so the content's JSON escapes fit):

- `project`: the Project's id (`new:2830…`, `source:docs-ideas`) or its exact name; an unknown
  or ambiguous one is `unknown_project`.
- `name`: the title the Library shows (1 to 200 characters, no line breaks).
- `filename`: the path inside the Project's default folder, ending in `.md`, `.markdown` or
  `.tex`; subfolders are allowed; not absolute, no empty, `.` or `..` segment, no segment starting
  with `.` or with a space at either end, no `build/` or `pdf/` folder (the folder's build outputs),
  no control characters or `< > : " | ? * \`.
- `content`: UTF-8 text without NUL, at most 4 MiB (the server's source limit), not blank; a
  `.tex` must be standalone (`\documentclass` and `\begin{document}` outside comments, the
  server's check); no agent proposals. A final newline is added when it is missing.
- `conversation_id`, `request_id`: lowercase UUIDs, as for proposals.

Steps (each saved in the request record before it is sent):

1. Validate the request (nothing is read before).
2. `GET /v1/library/projects`: find the Project and its default folder (the manifest's
   `source_folders[default_folder_id].path`, else the catalogue's "Default source folder", else
   `.thoughts/projects/<sha256(project id)>/default` as the server creates it). The source is
   `<default folder>/<filename>`. A document of the Project with that source (published or not)
   is `source_exists`.
3. `GET /v1/sources/<path>`: an existing source is `source_exists`. A deleted one is created again
   on its revision with the empty base; otherwise the base is revision 0 and the empty object.
4. `POST /v1/objects` with the content and with `b""`, `GET /v1/device-sequence`, then
   `POST /v1/source-publications` with one `upsert` (the proposal publication's payload, retries
   and sequence renumbering). A file another device created at the same moment is
   `source_exists`; nothing is published.
5. Compose the new PDF's build inputs (below; its PDF is the name the server gives a new
   default-folder document) and keep them in the record, so they follow the declaration at once.
6. `POST /v1/library/projects`
   `{operation_id, project_id, base_revision, action: "include_source", source_file, new_document_id}`
   with a client-made `new_document_id` (uuid4), as Android enqueues it. The server answers with
   the catalogue; the document is taken from it by its source.
7. At once `POST /v1/build-input-snapshots` (composed again if the declared PDF differs from the
   plan). A published note whose PDF has no snapshot fails every build of the Library ("affected
   target has no exact build input", production builds 89b3d9cf and 98fd3477), so when the inputs
   cannot be composed or Fedora refuses them, the note is unpublished:
   `{action: "set_document_published", document_id, published: false}`; the result says so
   (`published: false`, warning `document_unpublished`). The snapshot cannot be registered before
   include_source (the server needs the declaration), so between the include_source answer and the
   snapshot (one request) a build planned by another device would still fail; closing that needs
   the server (an include_source that declares the note unpublished, or builds that skip a PDF
   without inputs instead of failing).
8. `POST /v1/library/projects`
   `{operation_id, project_id, base_revision, action: "rename_document", document_id, name}`, so
   the Library shows `name` (the server would otherwise show the file stem or the built PDF's title).
9. `POST /v1/render-checks` (`target: all`), as after proposals, for a published note;
   `--no-render-check` skips it.

A Library-project action refused because another device changed the Project (409 "changed on
another device") is built again on a fresh catalogue with a new operation ID, three times at most;
then the answer is `project_conflict` with `retryable: true`. Any other refusal of step 5 or 6 is
`write_failed` saying that the note was saved at its path but not added (or not titled).

Result: `accepted: true`, `state: "document_created"`, `document_id`, `project_id`, `project`
(its name), `project_revision`, `name`, `source` (the path), `filename`, `format` (`markdown` or
`latex`), `source_sha256`, `source_revision`, `source_frontier`, `publication_id`,
`publication_outcome`, `device_id`, `conversation_id`, `request_id`, `replayed`, `published`,
`build_inputs` (below; `status` `skipped` or `refused` with `error_code` and `message` when the
note has none and was unpublished), `render_check`, and `warnings` (`several_sentences_on_line`, as
for proposals, `merged_with_concurrent_change`, those of the build inputs, and
`document_unpublished`).
Refusals are the error object above with `invalid_request`, `unknown_project`, `source_exists`
(with `source`), `project_conflict`, `request_conflict`, `busy`, `write_failed`, `read_failed`, or
`transport_error`; exit status as above.

Retries: the record (keyed by `request_id`, like a proposal's) holds the publication, the document
ID and each Library action with its operation ID. An identical retry of a finished request returns
its result with `replayed: true`; one of an unfinished request continues where it stopped, sending
a recorded publication or action unchanged (the server answers what it already applied from its
publication record or Library receipt), so a document is never created twice. The same
`request_id` with other arguments is `request_conflict`.

## Build inputs

Fedora builds a PDF only from a build-input snapshot (`POST /v1/build-input-snapshots`, schema 3;
`src/thoughts_server/build_inputs.py`): a render check plans each affected PDF with its latest
snapshot, and one without fails the whole build ("project_layout_invalid: affected target has no
exact build input"). The Mac's source publisher publishes them at each Sync, after its sources
(thoughts-desktop `tools/publisher/buildinputs.cpp`, `publishcommand.cpp`), so before this a
note created by an agent or the phone built only after the Mac's next Sync.

The desktop's snapshot belongs to the folder: it scans the folder on disk and publishes the same
entries for each of its PDFs, every file that is not a canonical source (Markdown images,
stylesheet dependencies, `tex/assets`, `tex/generated`, LaTeX folder assets, as `asset` or
`generated`) and the Project's selected imported PDFs (`external_pdf`), minus `.ThoughtIgnore`,
build directories and sensitive files, with `discovery_policy` `source-closure-v2`. Without the
folder on disk the client composes the same snapshot from what Fedora holds:

- the Project's selected imported PDFs of the folder from its manifest (path
  `<folder>/<source>`, SHA-256 from `rendering`, size from `HEAD /v1/objects/<sha256>`,
  `application/pdf`), exactly as the desktop;
- every other input as the Mac last published it: the union of the latest snapshots of the
  folder's other selected PDFs (the server declares the same union for a moved document);
- never a canonical source (`GET /v1/sources`), nor what the folder's `.ThoughtIgnore` (or its
  default rules), build directories or sensitive names exclude.

Left out with a warning: a path whose bytes differ between those snapshots
(`build_input_ambiguous`; Fedora gives every PDF the newest version of a path, which a client
cannot tell) and an object Fedora no longer holds (`build_input_unavailable`). Not covered: a
third-party PDF (`third_party/<key>.pdf`) that only the new note links (its link stays inactive
until the Mac publishes it).

`publish-build-inputs` does this for a note that exists, published or not: `document_id`, or
`project` and `filename` (as for create-document), `request_id`, and optionally `publish: true`
(`--publish`) to publish an unpublished note once its inputs are registered (never before). Only a
Markdown or standalone LaTeX note of a Project's default folder is supported; anything else is
`unsupported_document` (a connected `docs-*` folder, an imported PDF, a deleted note, a
non-standalone `.tex`, a Project without a manifest), an unknown one `unknown_document` or
`unknown_project`.

1. Validate the request; `GET /v1/library/projects`: the note, its folder and its PDF (the
   manifest's `rendering[document_id].output`, else the catalogue's `filename`).
2. `GET /v1/sources` (and the note, for a `.tex`; the folder's `.ThoughtIgnore`).
3. `GET /v1/build-input-snapshots/latest?target=` for each other selected PDF of the folder;
   `HEAD /v1/objects/<sha256>` for each object.
4. `GET /v1/build-input-snapshots/latest?target=<the PDF>`: the same snapshot is `current`; a
   different one (the Mac's or an earlier request's) is `kept`, never replaced, so no PDF's inputs
   are ever changed.
5. Otherwise `POST /v1/build-input-snapshots` (`published`); a refusal (409/422) is
   `build_inputs_refused`: nothing is registered and an unpublished note stays unpublished.
6. With `publish` and an unpublished note: `{action: "set_document_published", published: true}`.
7. `POST /v1/render-checks` (`target: all`) for a published note, unless `--no-render-check`.

Result: `state: "build_inputs_published"`, `document_id`, `project_id`, `project`, `source`,
`format`, `target`, `build_inputs` (`status`, `target`, `project_folder`, `discovery_policy`,
`snapshot_sha256`, `entries`, `roles`; `message` when `kept`), `published`, `warnings`,
`render_check`, `request_id`, `replayed`. The record keeps the composed snapshot, so a retry with the same
`request_id` sends the same one; an identical snapshot is answered as registered.

## HTML pages

`add-html-page` `{folder, name, html, replace?, request_id}` adds one self-contained page to a docs
folder as `<folder>/tex/support/html/<name>.html` (`protocol/html-pages.md`): it uploads the bytes and
changes the folder's page list with `POST /v1/html-pages`, naming the page it replaces, so a page
another device changed meanwhile is refused (`html_page_changed`) instead of overwritten. An existing
page needs `replace: true` (`html_page_exists`). The answer has `path`, `link` and the LaTeX and
Markdown forms of the link, and warns when the page loads resources from the network, which the
viewer blocks.

Proposal commands refuse (`invalid_link`, `links[]`) a `thoughts-html:` link the replacement adds to a
page the note's own folder does not have (`html_page_not_found`, `html_page_other_folder` with a
`suggestion` when its folder has a page of that name). Links already in the source are not checked;
with a server without `html_pages_v1` the check is skipped.

## Checks before saving

Proposal commands (and `create-document`, on the whole note) refuse, before anything is written,
the mistakes that stopped the user's PDF build until someone found them in a build log
(`thoughts_cli/guards.py`, `thoughts_cli/pdf_links.py`):

| Refusal (`error_code`) | What | Fix in the answer |
|---|---|---|
| `proposal_in_argument` | lines inside a key=value argument (`\hypersetup{...}`, `\geometry{...}`, package or TikZ options) or a bracketed optional argument (`\twocolumn[...]`, `\begin{figure}[...]`): the wrapper's own `[created=...,conversation_id=...]` breaks them | `command`, `suggested_start_line`, `suggested_end_line`: the whole command |
| `invalid_include_path` | `\includegraphics` of a path that leaves the note's folder | use the folder path, e.g. `tex/assets/<file>` |
| `review_card_latex_unsupported` | `\hyperref[...]{...}` inside a review card's masked answer | `suggestion`: `\hyperlink{label}{text}` and a `\hypertarget{label}{}` |
| `unsupported_character` | Markdown: emoji (the renderer crashed on its colour emoji font) | `issues[]` with the characters |
| `markdown_math_lt` | Markdown: a literal `<` in math ("not well-formed (invalid token)") | `issues[].suggestion`: the math with `\lt` / `\le` |
| `invalid_link` | a `\PdfLink`, `\CrossPdfLink` or Markdown PDF link the build would leave inactive: a PDF no Project selects (`pdf_not_in_library`, with the closest Library filename), a key with `.pdf` or a folder (`invalid_pdf_key`), an invalid anchor | `links[].suggestion` |

Repaired instead, with a warning:

- `include_path_rewritten`: `\includegraphics{../assets/x}` is written as the folder path
  `tex/assets/x` (resolved from the note's directory), which the build reads.
- `link_resolves_by_name`: a `\CrossPdfLink{docs-<folder>/key}` whose folder no longer holds the PDF
  (it moved) resolves by the PDF's name; the warning names the shorter `\PdfLink{key}{anchor}{text}`.
  `\PdfLink` finds a Library PDF by its filename in any Project, and every build defines it, also
  for notes in a Project's default folder (server: `doc_links.library_pdfs`).
- `original_braces_balanced`: a proposal may repair the file's braces. When the file does not
  balance and the file with the replacements does, an unbalanced original is accepted; it is kept
  in the wrapper with the fewest braces added (a wrapper's original must balance, spec 8.1), and
  the explanation says so. Accept gives the replacement, Reject that original; both balance the
  file. A selection that cuts a group of a balanced file is refused with the smallest balanced
  range (`suggested_start_line`, `suggested_end_line`).

Named anchors (`doc-anchor:`) are not checked; a missing one leaves only that link inactive and
shows in `build-status`. Lines and links the proposal keeps from the original are not checked.

## Build status and moving PDFs

`build-status` `{document}` (a document id, name, PDF filename or source path; reads only) answers
which PDF fails and why: the render status, the failure records and error lines of the newest build
(`errors[]` with the source and authored line the server located, `log_lines[]`, `error_lines[]`
with the text there), the links the build left inactive (`link_warnings`), the files changed since
the last good PDF, and `likely_causes[]`: the pending proposals of the failing sources ranked by
whether they hold the error line, hold what the error names (an undefined command, a missing file),
break a rule above, or are newer than the last good PDF, each with `reasons` and a `fix`.

`move-pdf` `{pdf, project, request_id}` moves an imported PDF (its document id, Library filename or
name) to another Project with its review cards (the Library-project action `move_document`, sent
again with fresh revisions when a Project changed meanwhile; a retry with the same request_id is
answered from its record). Links need no edit; the answer lists the sources that name the PDF
(`linking_sources`).

## Configuration

The server: `--server` or `THOUGHTS_SERVER_URL` (no default, so a test can never reach
production by accident); the device token from `--token-file`, `THOUGHTS_TOKEN_FILE` or
`THOUGHTS_TOKEN` (the token itself is never a command-line argument, so it stays out of process listings). Request records in
`--state-dir` / `THOUGHTS_CLI_STATE` (default `~/.local/state/thoughts-cli`), the source cache
for the uniqueness check in `--cache-dir` / `THOUGHTS_CLI_CACHE` (default `~/.cache/thoughts-cli`).

Library use (the claude.ai connector):

```python
from thoughts_cli import ThoughtsClient
client = ThoughtsClient(url, token, state_dir=..., cache_dir=...)
client.read_source(path)                       # dict as read-source prints it
client.run({"command": "create-proposals", ...})   # dict as create-proposals prints it; never raises for a refusal
client.run({"command": "create-document", ...})    # dict as create-document prints it
```

## How a request is written

1. Validate the request (the desktop's checks, before anything is read).
2. If the request ID has a record: a different request is `request_conflict`; an answered one
   returns its result with `replayed: true`; a prepared one sends its publication again (below).
3. `GET /v1/sources/<path>` and `GET /v1/objects/<object_sha256>` (verified). Generated sources
   are refused; a SHA-256 other than `expected_source_sha256` is `source_conflict` with
   `current_source_sha256`.
4. Write the proposals (`authoring.author`): `agent-<uuid4>` IDs, the UTC creation time, the
   overlap and nesting checks against every pending or broken proposal, the round-trip check.
   Uniqueness: the IDs must not occur in any other canonical source (`GET /v1/sources`; objects
   are cached by SHA-256, so only changed sources are downloaded again).
5. `POST /v1/objects` with the new bytes.
6. `GET /v1/sources/<path>` again: any change since step 3 is `source_conflict` (nothing written).
7. `GET /v1/device-sequence`, record the request as `prepared` (with its publication and bytes),
   then `POST /v1/source-publications` with the Mac source publisher's payload:

   ```json
   {"publication_id": "<uuid4>",
    "mutations": [{"action": "upsert", "operation_id": "<uuid4>", "device_sequence": <next>,
                   "path": "<source>", "base_revision": <revision read>, "base_sha256": "<sha read>",
                   "base_object_sha256": "<object read>", "content_sha256": "<new bytes>",
                   "client_timestamp": "2026-10-27T18:00:00.123456Z"}]}
   ```

8. Record the answer as `applied` and return it; then `POST /v1/render-checks` (`target: all`) so
   the server rebuilds the PDFs the new source made stale, as the Mac does after each Sync (the
   server's own authoring used a debounced proposal render instead). `--no-render-check` skips it.

## Concurrent changes

The client's own check (steps 3 and 6) refuses any change it can see. A change that another
device publishes in the milliseconds between step 6 and the server applying the publication is
handled by the server exactly as for every device's publication (`thoughts_server.sources`
`publish_sources`):

- The mutation's base is no longer current, so the server merges base, current and the new
  source three ways (`git merge-file`).
- Overlapping or adjacent changes (the other device edited lines the proposal wraps) are a
  conflict: the whole publication is refused with 409 and nothing is published or recorded. The
  client answers `source_conflict` with the current SHA-256 and forgets the request, as for a
  stale `expected_source_sha256`.
- Changes elsewhere merge cleanly (`outcome: merged`). The client downloads the merged source,
  checks that every proposal it wrote is present with the same original text, replacement and
  timestamp and no new issue, and answers with the merged `source_sha256`, recomputed `line`s and
  the warning `merged_with_concurrent_change` (or `merged_proposal_unverified` if the check
  failed, which a clean merge of disjoint lines should never produce).

The server's former endpoint refused instead of merging in that window
(`require_current_base`); a device publication cannot ask for that, and the client's re-read in
step 6 makes the window a few milliseconds.

## Retries and request IDs

`requests.json` maps `<server>#<token fingerprint>#<request_id>` to its record, so test and
production, or two devices, never share records. One process at a time holds `requests.lock`.

- Identical retry of an answered request: the stored result, `replayed: true`, nothing sent.
- Same request ID with different arguments: `request_conflict`.
- A request whose publication got no answer (timeout, closed connection, server error) stays
  `prepared`; the result is `transport_error` / `write_failed` with `retryable: true`. Retrying
  with the identical request sends the same publication (same publication, operation IDs and
  timestamp; the bytes are uploaded again). The server answers a publication it already applied
  from its record, so nothing is written twice; otherwise it is applied now (merged like any
  device's late publication if the source changed in between).
- A device sequence taken by another operation of the same device (`device_sequence_assigned`,
  e.g. the connector's diary entry at the same moment) is renumbered, keeping the IDs, as the
  Mac's publisher does (at most three times).
- A refused publication (conflict) removes the record: retrying the request authors it again
  from the current source.

Answered records are kept for 180 days.

## Device and token

The client publishes with the device token it is given; every proposal it writes is an
operation of that device with that device's own operation sequence (`GET /v1/device-sequence`
before each publication, renumbered on collision). It keeps no local sequence counter, so it can
share a device with another writer that also asks the server for its next sequence. The
claude.ai connector uses its own device `connector` (`~/connectors/thoughts-token`). A dedicated
device per agent host (`thoughts-admin create-device <name>`) keeps the writers apart if wanted.
Never use the Mac's or the phone's token: they keep local sequence counters.

## Not ported

- The desktop's introduced-link check (`invalid_link`) for `\SourceLink` and `\CrossSourceLink`;
  `thoughts-html:` links (`html_pages.py`) and Library PDF links (`pdf_links.py`) are checked.
- `remove-proposal` (a withdrawal; the desktop records its claim) is refused.
- The server port also refused IDs of proposals it had seen in the past (retired ones); the client
  checks the current sources, as the desktop checks its workspace. New IDs are random UUIDs.
- The desktop's Sync before and after writing: the client reads from and publishes to the server
  directly, so there is nothing to sync.

## Tests

```sh
python -m pytest clients/thoughts-cli/tests              # pure authoring + client (create-document, build inputs) against an in-memory server
python -m pytest tests/test_thoughts_cli.py              # client against this server's API, in-process
tests/integration/thoughts_cli_test_env.py               # the isolated Fedora test environment (see its docstring)
```
