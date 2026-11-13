#!/usr/bin/env python3
"""Generate decisions.json: shared fixtures for protocol/agent-proposals.md section 10.

Expectations are written by hand here (string building and hashing only); no
implementation is imported. Regenerate with:

    python3 protocol/fixtures/agent-proposals/gen_decisions.py

Schema (schema_version 1):
  scenarios[]: server state machine, one canonical store per scenario.
    id, rules (spec sections), note, strict (proposal_decisions_strict),
    initial {path: text}         canonical sources before step 0; every valid wrapper in them is "pending"
    steps[]:
      {"do": "upload", "device", "base": "current" | "initial" | <step index>,
       "sources": {path: text | null (delete)}, "claims": [claim],
       "expect": {"outcome": "applied" | "merged" | "conflict",
                  "claims": {decision_id: "matched" | "matched_late" | "settled" | "unmatched"},
                  "proposals": {id: {"status", "looks_like"?, "deleted"?}}}}
        One verified source transaction (a publication when it touches several paths).
        "base" names the canonical revision the device last synced: the text before step 0,
        after step N, or the current text (no merge).
      {"do": "resolve", "action": "accept" | "disregard", "ids": [...], "expect": {...}}
        A server-applied resolution (phone, or desktop without a local source), section 10.4.1.
      {"do": "dismiss", "id", "base_revision": "current" | "stale", "expect": {"outcome": "applied" | "conflict", "proposals"}}
        Section 10.6. "stale" means the client's base revision is older than the row's.
    "proposals" lists only the IDs a step checks.
  attach[]: client rule of section 10.4.2 (which stored claims go with an upload).
    id, claims [claim], upload {path: text}, expect {"sent": [decision_id], "dropped": [decision_id]}
  restore[]: Restore Proposal edit of section 10.6.
    id, path, source (current text T, or null when the file is missing), snapshot, line (removal.source_line),
    expect {"output": text} | {"already_present": true} | {"refused": code}
  Claims: decision_id, proposal_id, action, wrapper_sha256, source_file, decided_at, origin.
  wrapper_sha256 is the SHA-256 of the UTF-8 wrapper text, as in section 10.3.
"""
import hashlib
import json
from pathlib import Path

C = "2026-10-10T12:00:00.000Z"
U = "0b6c5b7e-1d2a-4c3b-9f8e-112233445566"
D = "2026-10-11T08:59:12.345Z"
TEX = "docs-x/tex/source/a.tex"
TEX2 = "docs-x/tex/source/b.tex"
LINK = "https://claude.ai/code/session_01ExampleSessionAbcdEfgh"
MD = "docs-x/notes/a.md"


def tex(pid, expl, orig, repl, created=C, conv=U, url=""):
    meta = ""
    if created:
        meta = ("[created=" + created + (",conversation_id=" + conv if conv else "")
                + (",conversation_url=" + url if url else "") + "]")
    return "\\agentProposedEdit" + meta + "{" + pid + "}{" + expl + "}{" + orig + "}{" + repl + "}"


def readable(value):
    if value == "":
        return ""
    return "".join("| " + line.replace("&", "&amp;").replace("-->", "--&gt;") + "\n" for line in value.split("\n"))


def v2(pid, expl, orig, after, created=C, conv=U, url=""):
    return ("<!-- thoughts:agent-proposal:v2:start\n" + f"id: {pid}\n" + f"created_at: {created}\n"
            + f"conversation_id: {conv}\n" + (f"conversation_url: {url}\n" if url else "") + "explanation:\n" + readable(expl)
            + "original_text:\n" + readable(orig) + "-->\n" + after
            + ("" if after == "" or after.endswith("\n") else "\n") + f"<!-- thoughts:agent-proposal:v2:end {pid} -->\n")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim(n, pid, action, wrapper, path=TEX, origin="desktop"):
    return {"decision_id": f"00000000-0000-4000-8000-{n:012d}", "proposal_id": pid, "action": action,
            "wrapper_sha256": sha(wrapper), "source_file": path, "decided_at": D, "origin": origin}


def did(n):
    return f"00000000-0000-4000-8000-{n:012d}"


def st(status, **extra):
    value = {"status": status, "deleted": status not in {"pending", "removed_outside"}}
    value.update(extra)
    return value


P1 = tex("agent-1", "Clarify.\n", "Old line.\n", "New line.\n")
P2 = tex("agent-2", "Shorten.\n", "Long words here.\n", "Short.\n")
BASE = "Intro.\n" + P1 + "Outro.\n"
ACCEPTED = "Intro.\nNew line.\nOutro.\n"
REJECTED = "Intro.\nOld line.\nOutro.\n"
EDITED = "Intro.\nSomething else.\nOutro.\n"
TWO = "Intro.\n" + P1 + "Middle.\n" + P2 + "Outro.\n"
TWO_ACCEPTED = "Intro.\nNew line.\nMiddle.\nShort.\nOutro.\n"

scenarios = []


def scenario(sid, initial, steps, rules, note, strict=True):
    scenarios.append({"id": sid, "rules": rules, "note": note, "strict": strict, "initial": initial, "steps": steps})


def upload(sources, claims=(), expect=None, device="desktop-a", base="current"):
    return {"do": "upload", "device": device, "base": base, "sources": sources, "claims": list(claims), "expect": expect or {}}


scenario("desktop-accept-with-claim", {TEX: BASE},
         [upload({TEX: ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "applied", "claims": {did(1): "matched"}, "proposals": {"agent-1": st("accepted")}})],
         ["10.4.2", "10.4.3"], "A local Accept uploaded with its claim retires the proposal as accepted.")

scenario("desktop-reject-with-claim", {TEX: BASE},
         [upload({TEX: REJECTED}, [claim(1, "agent-1", "reject", P1)],
                 {"outcome": "applied", "claims": {did(1): "matched"}, "proposals": {"agent-1": st("rejected")}})],
         ["10.4.3"], "")

scenario("hand-edit-exact-accept-strict", {TEX: BASE},
         [upload({TEX: ACCEPTED}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("removed_outside", looks_like="accept")}})],
         ["10.2", "10.3"], "Exact accept text without a claim is not a decision in strict mode.")

scenario("hand-edit-exact-accept-compatible", {TEX: BASE},
         [upload({TEX: ACCEPTED}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("accepted")}})],
         ["10.7.1", "10.9"], "Compatible mode (rollout step 1) keeps today's exact-text classification.", strict=False)

scenario("hand-edit-other-text-compatible", {TEX: BASE},
         [upload({TEX: EDITED}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("removed_outside", looks_like="edited")}})],
         ["10.2", "10.9"], "What used to be deleted_from_source is removed_outside even in compatible mode.", strict=False)

scenario("claim-with-later-edits-in-same-upload", {TEX: BASE},
         [upload({TEX: "Intro.\nNew line, then edited.\nOutro.\n"}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "applied", "claims": {did(1): "matched"}, "proposals": {"agent-1": st("accepted")}})],
         ["10.4.3"], "The decided text is not compared with section 7: the user edited after accepting, before Sync.")

scenario("claim-but-wrapper-still-present", {TEX: BASE},
         [upload({TEX: BASE + "More.\n"}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "applied", "claims": {did(1): "unmatched"}, "proposals": {"agent-1": st("pending")}})],
         ["10.4.3"], "A client should have dropped this claim (10.4.2); the server ignores it.")

scenario("late-claim-after-hand-removal-elsewhere", {TEX: BASE},
         [upload({TEX: ACCEPTED}, [], {"outcome": "applied",
                                        "proposals": {"agent-1": st("removed_outside", looks_like="accept")}},
                 device="mac-b"),
          upload({TEX: ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "merged", "claims": {did(1): "matched_late"}, "proposals": {"agent-1": st("accepted")}},
                 device="desktop-a", base="initial")],
         ["10.4.3", "10.8"], "Another device removed the wrapper first; the offline desktop's decision on the same version settles it.")

scenario("late-claim-for-other-version", {TEX: BASE},
         [upload({TEX: BASE.replace("New line.", "Newer line.")}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("pending")}}, device="mac-b"),
          upload({TEX: EDITED}, [], {"outcome": "applied",
                                      "proposals": {"agent-1": st("removed_outside", looks_like="edited")}}, device="mac-b"),
          upload({TEX: ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "conflict", "claims": {},
                  "proposals": {"agent-1": st("removed_outside")}}, device="desktop-a", base="initial"),
          upload({TEX: ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "applied", "claims": {did(1): "unmatched"},
                  "proposals": {"agent-1": st("removed_outside")}}, device="desktop-a", base="current")],
         ["10.4.3", "10.8"], "desktop-a accepted the first version offline while mac-b revised and then hand-removed it. "
                             "The upload conflicts (nothing applied, claims kept); the conflict resolution carries the claim, "
                             "which names a version that is not the one that vanished, so it does not settle the row.")

scenario("phone-resolution-then-desktop-claim", {TEX: BASE},
         [{"do": "resolve", "action": "accept", "ids": ["agent-1"], "expect": {"proposals": {"agent-1": st("accepted")}}},
          upload({TEX: ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "merged", "claims": {did(1): "settled"}, "proposals": {"agent-1": st("accepted")}},
                 base="initial")],
         ["10.4.1", "10.4.3", "10.8"], "Both devices accepted; the second claim is settled.")

scenario("agent-withdrawal", {TEX: BASE},
         [upload({TEX: REJECTED}, [claim(1, "agent-1", "withdraw", P1, origin="mcp")],
                 {"outcome": "applied", "claims": {did(1): "matched"}, "proposals": {"agent-1": st("withdrawn")}})],
         ["10.1", "10.4.2"], "remove_proposal restores the original text and records a withdrawal, not a rejection.")

scenario("partial-review-last-step", {TEX: BASE},
         [upload({TEX: "Intro.\nNew\nOutro.\n"}, [claim(1, "agent-1", "partial", P1, origin="phone_editor")],
                 {"outcome": "applied", "claims": {did(1): "matched"}, "proposals": {"agent-1": st("decided_in_parts")}})],
         ["10.4.2", "10.5"], "Only the phone source editor produces partial claims (VS Code decides whole proposals only).")

scenario("batch-hand-accept", {TEX: TWO},
         [upload({TEX: TWO_ACCEPTED}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("removed_outside", looks_like="accept"),
                                                      "agent-2": st("removed_outside", looks_like="accept")}})],
         ["10.3"], "Each vanished wrapper gets its own row; looks_like also recognises a whole-file batch decision.")

scenario("one-claimed-one-hand-removed", {TEX: TWO},
         [upload({TEX: TWO_ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "applied", "claims": {did(1): "matched"},
                  "proposals": {"agent-1": st("accepted"), "agent-2": st("removed_outside", looks_like="accept")}})],
         ["10.4.3"], "")

scenario("restore-makes-pending", {TEX: BASE},
         [upload({TEX: EDITED}, [], {"outcome": "applied", "proposals": {"agent-1": st("removed_outside")}}),
          upload({TEX: BASE}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("pending")}})],
         ["10.2", "10.6"], "Any upload that brings the wrapper back makes it pending; Restore sends no proposal operation.")

scenario("dismiss", {TEX: BASE},
         [upload({TEX: EDITED}, [], {"outcome": "applied", "proposals": {"agent-1": st("removed_outside")}}),
          {"do": "dismiss", "id": "agent-1", "base_revision": "current",
           "expect": {"outcome": "applied", "proposals": {"agent-1": st("dismissed")}}},
          upload({TEX: BASE}, [], {"outcome": "applied", "proposals": {"agent-1": st("pending")}})],
         ["10.6"], "A dismissed proposal that reappears is pending again.")

scenario("dismiss-after-restore-conflicts", {TEX: BASE},
         [upload({TEX: EDITED}, [], {"outcome": "applied", "proposals": {"agent-1": st("removed_outside")}}),
          upload({TEX: BASE}, [], {"outcome": "applied", "proposals": {"agent-1": st("pending")}}, device="phone"),
          {"do": "dismiss", "id": "agent-1", "base_revision": "stale",
           "expect": {"outcome": "conflict", "proposals": {"agent-1": st("pending")}}}],
         ["10.6"], "Dismiss queued offline on one device after another device restored the proposal.")

scenario("dismiss-pending-refused", {TEX: BASE},
         [{"do": "dismiss", "id": "agent-1", "base_revision": "current",
           "expect": {"outcome": "conflict", "proposals": {"agent-1": st("pending")}}}],
         ["10.6"], "Only removed_outside rows can be dismissed.")

scenario("broken-wrapper-is-not-removal", {TEX: BASE},
         [upload({TEX: BASE.replace("{New line.\n}", "{New line.\n")}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("pending")}})],
         ["10.2"], "An unclosed wrapper is an issue (section 6) that still carries the ID; it has not vanished.")

scenario("commented-wrapper-is-not-removal", {TEX: BASE},
         [upload({TEX: "Intro.\n% " + P1 + "\nOutro.\n"}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("pending")}})],
         ["10.2"], "The ID text is still in the tree; today's conservative rule keeps the row unchanged.")

scenario("atomic-move", {TEX: BASE, TEX2: "Other.\n"},
         [upload({TEX: ACCEPTED.replace("New line.\n", ""), TEX2: "Other.\n" + P1}, [],
                 {"outcome": "applied", "proposals": {"agent-1": st("pending")}})],
         ["10.2"], "A wrapper moved to another file in one publication is the same pending proposal.")

scenario("source-file-deleted", {TEX: BASE, TEX2: "\\documentclass{article}\n\\begin{document}\n\\input{a}\n\\end{document}\n"},
         [upload({TEX: None}, [], {"outcome": "applied",
                                   "proposals": {"agent-1": st("removed_outside", looks_like="file_deleted")}})],
         ["10.2", "10.3"], "Deleting an included file (not the document's entry source) is an ordinary disappearance.")

scenario("claim-survives-rename", {TEX: BASE},
         [upload({TEX: None, TEX2: ACCEPTED}, [claim(1, "agent-1", "accept", P1)],
                 {"outcome": "applied", "claims": {did(1): "matched"}, "proposals": {"agent-1": st("accepted")}})],
         ["10.4.2"], "Accept, then rename the file before Sync: the publication-level claim still matches.")

attach = [
    {"id": "attach-decided", "claims": [claim(1, "agent-1", "accept", P1)], "upload": {TEX: ACCEPTED},
     "expect": {"sent": [did(1)], "dropped": []}},
    {"id": "attach-undone-locally", "claims": [claim(1, "agent-1", "accept", P1)], "upload": {TEX: BASE},
     "expect": {"sent": [], "dropped": [did(1)]},
     "note": "Undo brought the wrapper back before Sync; the claim is dropped, not sent."},
    {"id": "attach-other-path-not-uploaded", "claims": [claim(1, "agent-1", "accept", P1)], "upload": {TEX2: "Other.\n"},
     "expect": {"sent": [], "dropped": []},
     "note": "Claims wait for an upload that includes their source (or its deletion/rename)."},
    {"id": "attach-broken-after-decision", "claims": [claim(1, "agent-2", "accept", P2)],
     "upload": {TEX: "Intro.\n" + P1.replace("{New line.\n}", "{New line.\n") + "Short.\nOutro.\n"},
     "expect": {"sent": [did(1)], "dropped": []},
     "note": "Only the claimed wrapper's presence matters; an unrelated broken wrapper does not hold a claim back."},
]

# ------------------------------------------------------------------ restore
restore = []


def snap(pid, expl, before, after, path=TEX, created=C, conv=U, url=""):
    value = {"proposal_id": pid, "source_file": path, "comment": expl, "before": before, "after": after}
    if created:
        value["created_at"] = created
    if conv:
        value["conversation_id"] = conv
    if url:
        value["conversation_url"] = url
    return value


S1 = snap("agent-1", "Clarify.\n", "Old line.\n", "New line.\n")


def r(rid, source, snapshot, line, expect, path=TEX, note=""):
    restore.append({"id": rid, "path": path, "source": source, "snapshot": snapshot, "line": line, "expect": expect,
                    **({"note": note} if note else {})})


r("restore-accepted-text", ACCEPTED, S1, 2, {"output": BASE})
r("restore-rejected-text", REJECTED, S1, 2, {"output": BASE})
r("restore-text-changed", EDITED, S1, 2, {"refused": "text_changed"})
r("restore-nearest-candidate", "New line.\nA\nB\nC\nD\nNew line.\n", S1, 5,
  {"output": "New line.\nA\nB\nC\nD\n" + P1}, note="Candidates on lines 1 and 6; line 6 is nearer to L=5.")
r("restore-tie-ambiguous", "New line.\nA\nNew line.\n", S1, 2, {"refused": "ambiguous"})
r("restore-mixed-tie-ambiguous", "Old line.\nA\nNew line.\n", S1, 2, {"refused": "ambiguous"},
  note="Accepted and original text are equally near: Thoughts cannot tell which one the proposal covered.")
r("restore-not-whole-line", "Intro.\nSee New line.\nOutro.\n", S1, 2, {"refused": "text_changed"},
  note="A candidate must start and end at line boundaries.")
r("restore-already-present", BASE, S1, 2, {"already_present": True})
r("restore-id-in-comment", "Intro.\n% agent-1 was here\nNew line.\nOutro.\n", S1, 3, {"refused": "id_in_source"})
r("restore-source-missing", None, S1, 2, {"refused": "source_missing"})
r("restore-overlaps-wrapper", "Intro.\n" + tex("agent-9", "Other.\n", "Old.\n", "Changed.\nNew line.\n") + "\nOutro.\n", S1, 3,
  {"refused": "text_changed"}, note="A whole line inside another proposal is not a candidate.")
r("restore-legacy-without-metadata", ACCEPTED, snap("agent-1", "Clarify.\n", "Old line.\n", "New line.\n", created="", conv=""), 2,
  {"refused": "not_restorable"},
  note="LaTeX metadata is mandatory (section 4.3): a snapshot without it would write an invalid_metadata wrapper.")
r("restore-created-only", ACCEPTED, snap("agent-1", "Clarify.\n", "Old line.\n", "New line.\n", conv=""), 2,
  {"refused": "not_restorable"}, note="Both created and conversation_id are required.")
r("restore-tex-explanation-not-reescaped", ACCEPTED, snap("agent-1", "50\\% done.\n", "Old line.\n", "New line.\n"), 2,
  {"output": "Intro.\n" + tex("agent-1", "50\\% done.\n", "Old line.\n", "New line.\n") + "Outro.\n"},
  note="The LaTeX explanation is stored as written (section 3.4) and written back unchanged.")
r("restore-insertion-accepted", "Intro.\nAdded.\nOutro.\n", snap("agent-1", "Add.\n", "", "Added.\n"), 2,
  {"output": "Intro.\n" + tex("agent-1", "Add.\n", "", "Added.\n") + "Outro.\n"})
r("restore-insertion-rejected", "Intro.\nOutro.\n", snap("agent-1", "Add.\n", "", "Added.\n"), 2,
  {"output": "Intro.\n" + tex("agent-1", "Add.\n", "", "Added.\n") + "Outro.\n"},
  note="The empty original's only candidate is the start of line L.")
r("restore-deletion-prefers-text", "Intro.\nGone.\nOutro.\n", snap("agent-1", "Drop.\n", "Gone.\n", ""), 2,
  {"output": "Intro.\n" + tex("agent-1", "Drop.\n", "Gone.\n", "") + "Outro.\n"},
  note="A non-empty candidate wins over an empty text's candidate.")
MDS = snap("agent-1", "Clarify.", "Old line.\n", "New line.\n", path=MD)
r("restore-markdown", "Intro.\nNew line.\nOutro.\n", MDS, 2,
  {"output": "Intro.\n" + v2("agent-1", "Clarify.", "Old line.\n", "New line.\n") + "Outro.\n"}, path=MD)
r("restore-markdown-escapes", "Intro.\nOld --> &amp;\nOutro.\n",
  snap("agent-1", "a --> b & c", "Old --> &amp;\n", "New.\n", path=MD), 2,
  {"output": "Intro.\n" + v2("agent-1", "a --> b & c", "Old --> &amp;\n", "New.\n") + "Outro.\n"}, path=MD)
r("restore-conversation-url", ACCEPTED, snap("agent-1", "Clarify.\n", "Old line.\n", "New line.\n", url=LINK), 2,
  {"output": "Intro.\n" + tex("agent-1", "Clarify.\n", "Old line.\n", "New line.\n", url=LINK) + "Outro.\n"},
  note="The stored conversation link is written back (section 3.6).")
r("restore-markdown-conversation-url", "Intro.\nNew line.\nOutro.\n",
  snap("agent-1", "Clarify.", "Old line.\n", "New line.\n", path=MD, url=LINK), 2,
  {"output": "Intro.\n" + v2("agent-1", "Clarify.", "Old line.\n", "New line.\n", url=LINK) + "Outro.\n"}, path=MD)

# Sanity: the restored outputs re-create the wrappers byte for byte where the source was written by the desktop writer.
assert restore[0]["expect"]["output"] == BASE

out = Path(__file__).resolve().parent / "decisions.json"
out.write_text(json.dumps({"schema_version": 1, "spec": "protocol/agent-proposals.md#10",
                           "scenarios": scenarios, "attach": attach, "restore": restore},
                          indent=1, ensure_ascii=False) + "\n")
print(f"{len(scenarios)} scenarios, {len(attach)} attach cases, {len(restore)} restore cases -> {out.name}")
