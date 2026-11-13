# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""build-status: which PDF of a document fails, why, and which pending proposal most likely caused it.

Before this, an agent asked GET /v1/render-status, then GET /v1/builds/<id>, then
GET /v1/objects/<log> and searched the log, then read the sources for the pending
proposals near the error (memories thoughts-build-failure-diagnosis and
thoughts-unbalanced-brace-repair). One command now answers:

* the document's PDF and its render status (synced, building, attention, ...);
* the failure records of its newest build and the error lines of the build log, with
  the source and authored line the server located (``Source <path>:<line> failed: ...``);
* the links the build left inactive (``unresolved_link`` warnings);
* the pending proposals of the failing sources, ranked: one that holds the error line,
  one whose text holds what the error names (an undefined command, a missing file), one
  that breaks a rule of guards.py (a proposal inside \\hypersetup or \\twocolumn[...], a
  ../ figure path, emoji or < in Markdown math), one newer than the last good PDF; each
  with the fix (revise_proposal, or remove_proposal and propose the whole command).

It only reads.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from . import documents, guards
from .api import ApiError, ServerApi, TransportError
from .authoring import AuthoringRefused, brace_depth
from .proposal_syntax import parse, source_format

BUILD_STATUS = "build-status"
FIELDS = frozenset({"command", "document"})
MAX_LOG_LINES = 40
SOURCE_FAILED = re.compile(r"Source (?P<path>\S+?)(?::(?P<line>\d+))? failed(?: during LaTeX compilation)?:?\s*"
                           r"(?P<message>.*?)(?:;\s*latexmk exit \d+)?\s*$")
LOCATED = re.compile(r"(?P<path>(?:docs-[A-Za-z0-9_.-]+|\.thoughts/projects/[0-9a-f]{64}/(?:default|documents))"
                     r"/[^\s:;]+\.(?:tex|md|markdown)):(?P<line>\d+)")
UNCAPTURED = re.compile(r"uncaptured host file\s+(?P<file>[^\s;,]+)")
UNDEFINED = re.compile(r"Undefined control sequence\s*(?P<command>\\[A-Za-z@]+)?")
NOT_FOUND = re.compile(r"File [`'\"]?(?P<file>[^`'\"\s]+)[`'\"]? not found")
LOG_ERROR = re.compile(r"^(?:!|l\.\d+|MJPDF_FAILURE\t|Runaway argument|.*(?:LaTeX|Package \S+) Error:|"
                       r".*\.(?:tex|md):\d+: )")
CODE = re.compile(r"^(?:renderer exited with -?\d+:\s*)?([a-z0-9_]+):")


def validate(request: Any) -> str:
    if not isinstance(request, dict) or request.get("command") != BUILD_STATUS:
        raise AuthoringRefused("invalid_request", "The request must be a build-status JSON object.")
    for key in request:
        if key not in FIELDS:
            raise AuthoringRefused("invalid_request", f"Unexpected request field: {key}")
    document = request.get("document")
    if not isinstance(document, str) or not document.strip() or len(document) > 1024:
        raise AuthoringRefused("invalid_request", "document must be a document id, its name, its PDF filename or "
                                                  "its source path.")
    return document.strip()


def find_document(catalogue: Any, wanted: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """The (Project, document) that wanted names: its id, PDF filename, source path, or exact (then folded) name."""
    entries = [(project, document) for project in (catalogue or {}).get("projects") or [] if isinstance(project, dict)
               for document in documents.documents(project)]
    pdf = wanted if wanted.lower().endswith(".pdf") else wanted + ".pdf"
    for test in (lambda d: d.get("document_id") == wanted, lambda d: d.get("filename") == pdf,
                 lambda d: d.get("source_file") == wanted, lambda d: d.get("name") == wanted,
                 lambda d: str(d.get("name", "")).casefold() == wanted.casefold()):
        matches = [(project, document) for project, document in entries if test(document)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            listed = ", ".join(f"{d.get('name')!r} in {p.get('name')!r} ({d.get('document_id')})" for p, d in matches)
            raise AuthoringRefused("ambiguous_document", f"Several documents match {wanted!r}: {listed}; give its id.")
    raise AuthoringRefused("unknown_document", f"No Library document has the id, PDF filename, source path or name "
                                               f"{wanted!r}.")


def _code(message: str, fallback: str | None = None) -> str | None:
    match = CODE.match(message.strip())
    return match.group(1) if match else fallback


def errors_of(messages: list[str]) -> list[dict[str, Any]]:
    """The located errors in failure messages and log lines: {source, line, code, message}."""
    found, seen = [], set()
    for raw in messages:
        message = " ".join(raw.replace("\t", " ").split())
        entry: dict[str, Any] = {"message": message}
        code = _code(message.removeprefix("MJPDF_FAILURE "))
        if code:
            entry["code"] = code
        if (match := SOURCE_FAILED.search(message)) is not None:
            entry.update(source=match.group("path"), detail=match.group("message"))
            if match.group("line"):
                entry["line"] = int(match.group("line"))
        elif (match := LOCATED.search(message)) is not None:
            entry.update(source=match.group("path"), line=int(match.group("line")))
        if (match := UNCAPTURED.search(message)) is not None:
            entry["file"] = match.group("file")
        key = (entry.get("source"), entry.get("line"), entry.get("detail") or message[-200:])
        if key not in seen:
            seen.add(key)
            found.append(entry)
    return found


def tokens_of(errors: list[dict[str, Any]]) -> list[str]:
    """What the errors name that a proposal's text could hold: undefined commands and file names."""
    tokens = []
    for error in errors:
        text = error.get("message", "")
        tokens += [match.group("command") for match in UNDEFINED.finditer(text) if match.group("command")]
        tokens += [match.group("file").rpartition("/")[2] for match in NOT_FOUND.finditer(text)]
        if error.get("file"):
            tokens.append(error["file"].rpartition("/")[2])
    return list(dict.fromkeys(token for token in tokens if token))


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _line(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def rank_proposals(path: str, text: str, errors: list[dict[str, Any]], tokens: list[str],
                   last_good: datetime | None) -> list[dict[str, Any]]:
    """The pending proposals of one source, each with a score, the reasons and the fix."""
    parsed = parse(path, text)
    lines = [error["line"] for error in errors if error.get("source") == path and error.get("line")]
    codes = {error.get("code") for error in errors}
    crashed = any(re.search(r"exit(?:ed)?(?: with)? -11|signal 11|Segmentation", error.get("message", ""))
                  for error in errors)
    unreadable = any("not well-formed" in error.get("message", "") for error in errors)
    frames = [frame for frame in guards.argument_frames(text) if frame.forbidden] if path.endswith(".tex") else []
    masks = guards._masks(text) if path.endswith(".tex") else []
    ranked = []
    for proposal in parsed.proposals:
        first, last = _line(text, proposal.start), _line(text, max(proposal.start, proposal.end - 1))
        score, reasons, fixes = 0, [], []
        held = [line for line in lines if first <= line <= last]
        if held:
            score += 100
            reasons.append(f"the error is at line {held[0]}, inside this proposal (lines {first}-{last})")
        named = [token for token in tokens if token in proposal.replacement]
        if named:
            score += 60
            reasons.append(f"its proposed text holds {', '.join(named)}, which the error names")
        inside = [frame for frame in frames if frame.contains(proposal.start, len(text))]
        if inside:
            outer = min(inside, key=lambda frame: frame.command_start)
            close = outer.close if outer.close is not None else len(text) - 1
            score += 80
            what = guards._shown(outer, text)
            reasons.append(f"it sits inside {what}, where the proposal marker's own [created=...] breaks the build")
            fixes.append(f"remove_proposal {proposal.id}, then propose the whole command: start_line "
                         f"{_line(text, outer.command_start)}, end_line {_line(text, close) + 1}")
        try:
            _rewritten, warnings = guards.rewrite_include_paths(path, proposal.replacement)
            escaping = None
        except AuthoringRefused as refusal:
            warnings, escaping = [], refusal.message
        uncaptured = "undeclared_build_input" in codes or any("uncaptured" in e.get("message", "") for e in errors)
        if warnings:
            score += 80 if uncaptured else 30
            reasons.append("it includes a figure by a ../ path, which the build refuses (undeclared_build_input)")
            fixes.append(f"revise_proposal {proposal.id} with the figure paths written as in the folder: "
                         + "; ".join(f"{w['from']} -> {w['to']}" for w in warnings))
        elif escaping:
            score += 80 if uncaptured else 30
            reasons.append(escaping.removesuffix(" Nothing was written."))
        if path.endswith((".md", ".markdown")):
            for issue in guards.markdown_issues(proposal.replacement, proposal.original):
                strong = (issue["code"] == "unsupported_character" and crashed) or (
                    issue["code"] == "markdown_math_lt" and unreadable)
                score += 80 if strong else 30
                reasons.append(issue["message"])
                fixes.append(f"revise_proposal {proposal.id}: " + (
                    f"write {issue['suggestion']}" if issue.get("suggestion") else "remove the emoji"))
        if path.endswith(".tex") and "hyperref" in proposal.replacement and any(
                low < proposal.start and proposal.end <= high for low, high, _card in masks):
            score += 10
            reasons.append("it puts \\hyperref inside a review card's answer (the card becomes unavailable; the PDF "
                           "still builds)")
            fixes.append(f"revise_proposal {proposal.id} with \\hyperlink{{label}}{{text}} and a \\hypertarget")
        depth = brace_depth(proposal.replacement)
        if depth.final or depth.lowest:
            score += 50
            reasons.append("its proposed text does not balance its braces")
        created = _timestamp(proposal.created_at)
        if created is not None and last_good is not None and created > last_good:
            score += 20
            reasons.append("it is newer than the last PDF that built")
        if not held and lines and not reasons and first <= min(lines):
            score += 5
            reasons.append(f"it is the nearest proposal before the error line {min(lines)}")
        entry = {"proposal_id": proposal.id, "source": path, "start_line": first, "end_line": last,
                 "created_at": proposal.created_at, "conversation_id": proposal.conversation_id, "score": score,
                 "reasons": reasons}
        if fixes:
            entry["fix"] = "; ".join(fixes)
        ranked.append(entry)
    return ranked


def build_status(api: ServerApi, request: Any) -> dict[str, Any]:
    wanted = validate(request)
    project, document = find_document(api.library_projects(), wanted)
    filename = document.get("filename")
    if not isinstance(filename, str):
        raise AuthoringRefused("unknown_document", f"{wanted!r} has no PDF.")
    render = api.render_status(filename)
    build = render.get("build") if isinstance(render.get("build"), dict) else {}
    outcome = build.get("result") if isinstance(build.get("result"), dict) else {}
    failures = [failure for failure in outcome.get("failed_documents") or []
                if isinstance(failure, dict) and failure.get("pdf_filename") == filename]
    link_warnings = [warning.get("message") for warning in outcome.get("warnings") or []
                     if isinstance(warning, dict) and warning.get("pdf_filename") == filename]
    publication = render.get("publication") if isinstance(render.get("publication"), dict) else {}
    result: dict[str, Any] = {
        "schema_version": 1, "accepted": True, "terminal": True, "state": "build_status",
        "document": document.get("name"), "document_id": document.get("document_id"), "pdf_filename": filename,
        "project": project.get("name"), "project_id": project.get("id"), "source": document.get("source_file"),
        "status": render.get("status"), "reason": render.get("reason", ""),
        "build": {key: build.get(key) for key in ("id", "status", "target") if build.get(key) is not None} or None,
        "last_good_pdf": ({"published_at": publication.get("published_at"), "build_id": publication.get("build_id")}
                          if publication else None),
        "link_warnings": link_warnings,
    }
    failing = bool(failures) or render.get("status") in {"attention", "error"}
    if not failing:
        result["errors"], result["likely_causes"] = [], []
        result["message"] = (f"{filename} is {render.get('status')}: {render.get('reason', '')}".strip()
                             + (f" {len(link_warnings)} link(s) stay inactive; see link_warnings." if link_warnings
                                else ""))
        return result
    messages = [str(failure.get("message") or failure.get("error") or "") for failure in failures]
    if not messages and build.get("error"):
        messages.append(str(build["error"]))
    log_lines: list[str] = []
    if isinstance(outcome.get("log_sha256"), str):
        try:
            log = api.download(outcome["log_sha256"]).decode("utf-8", "replace")
        except (ApiError, TransportError):
            log = ""
        for line in log.splitlines():
            if LOG_ERROR.match(line) and (not line.startswith("MJPDF_FAILURE\t") or filename in line
                                          or line.startswith("MJPDF_FAILURE\tnative-render")):
                log_lines.append(line.rstrip())
    result["log_lines"] = log_lines[-MAX_LOG_LINES:]
    errors = errors_of(messages + log_lines)
    result["errors"] = errors
    tokens = tokens_of(errors)
    # The sources to look at: where the errors are, the failure's source, the document's own.
    paths = [error["source"] for error in errors if error.get("source")]
    paths += [failure.get("source") for failure in failures]
    paths.append(document.get("source_file"))
    last_good = _timestamp(publication.get("published_at"))
    causes, error_texts = [], []
    for path in dict.fromkeys(path for path in paths if isinstance(path, str) and source_format(path)):
        snapshot = api.source(path)
        if snapshot is None or snapshot.get("deleted"):
            continue
        text = api.download(snapshot["object_sha256"]).decode("utf-8", "replace")
        causes += rank_proposals(path, text, errors, tokens, last_good)
        source_lines = text.split("\n")
        for error in errors:
            if error.get("source") == path and error.get("line") and 0 < error["line"] <= len(source_lines):
                error_texts.append({"source": path, "line": error["line"], "text": source_lines[error["line"] - 1]})
        if path.endswith((".md", ".markdown")) and any(
                "not well-formed" in error.get("message", "") or "-11" in error.get("message", "") for error in errors):
            result.setdefault("markdown_issues", []).extend(
                {**issue, "source": path} for issue in guards.markdown_issues(text))
    result["error_lines"] = error_texts
    result["pending_proposals"] = len(causes)
    result["likely_causes"] = sorted((cause for cause in causes if cause["score"] > 0),
                                     key=lambda cause: (-cause["score"], cause["source"], cause["start_line"]))
    if build.get("id"):
        try:
            diff = api.build_source_diff(build["id"])
            changed = [{"path": file.get("path"), "change": file.get("change")}
                       for entry in diff.get("documents") or [] if entry.get("target") == filename
                       for file in entry.get("files") or [] if file.get("change") != "unchanged"]
            result["changed_since_last_good"] = changed
        except (ApiError, TransportError, AttributeError):
            pass
    top = result["likely_causes"][0] if result["likely_causes"] else None
    first = errors[0] if errors else {"message": render.get("reason", "")}
    where = f" at {first['source']}:{first['line']}" if first.get("source") and first.get("line") else ""
    summary = f"{filename} does not build{where}: {first.get('detail') or first.get('message')}"
    if top is not None:
        summary += (f" Most likely cause: pending proposal {top['proposal_id']} (lines {top['start_line']}-"
                    f"{top['end_line']} of {top['source']}): {'; '.join(top['reasons'])}.")
        if top.get("fix"):
            summary += f" Fix: {top['fix']}."
    elif error_texts:
        summary += " No pending proposal is involved; the error is in the note's own text (error_lines)."
    result["message"] = summary
    return result
