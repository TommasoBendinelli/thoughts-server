#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Drive the thoughts-server Python implementations on the agent-proposal fixtures.

Usage: PYTHONPATH=<server worktree>/src <server venv python> python_server.py cases.json out.json

Implementations:
  server-core       proposal_syntax.parse / decide / classify_proposal_transition (path-based
                    dispatch; used by the resolution worker, source-transition classification,
                    native_renderer and review_metadata; markdown_proposals for Markdown).
  server-publisher  renderer_tools.publish_phone_source_bundle.parse_agent_proposal_source
                    (produces the phone manifest). Parse only; byte offsets are converted to code
                    points. The manifest's comment is the trimmed explanation and Markdown
                    proposals carry no original text, by schema.
  server-history    proposal_history.snapshots (open-proposal state). Lists proposals only: no
                    spans and no issues by design.
Output format: see compare.py.
"""
import json
import sys

from thoughts_server import proposal_syntax as ps
from thoughts_server.renderer_tools import publish_phone_source_bundle as pub
from thoughts_server.proposal_history import snapshots

FIELDS = ["id", "created_at", "conversation_id", "conversation_url", "explanation", "original", "replacement"]


def _url(value):
    return {"conversation_url": value} if value else {}


def _issue(code, start, end, ident):
    return {"code": code, "span": {"start": start, "end": end}, **({"id": ident} if ident is not None else {})}


def core_parse(path, source):
    parsed = ps.parse(path, source)
    proposals = [{"id": p.id, "created_at": p.created_at, "conversation_id": p.conversation_id,
                  **_url(p.conversation_url), "explanation": p.explanation, "original": p.original, "replacement": p.replacement,
                  "span": {"start": p.start, "end": p.end}} for p in parsed.proposals]
    return {"proposals": proposals, "issues": [_issue(i.code, i.start, i.end, i.id) for i in parsed.issues]}


def publisher_parse(path, source):
    raw = source.encode("utf-8")

    def cp(byte_offset):
        return len(raw[:byte_offset].decode("utf-8"))

    items, issues = pub.parse_agent_proposal_source(path, raw)
    out = []
    for item in items:
        def text(part):
            return raw[part["byte_start"]:part["byte_end"]].decode("utf-8")
        entry = {"id": item["id"], "created_at": item.get("created_at", ""),
                 "conversation_id": item.get("conversation_id", ""), **_url(item.get("conversation_url")),
                 "explanation": item.get("comment", ""),
                 "replacement": text(item["after"]), "span": {"start": cp(item["macro_start"]), "end": cp(item["macro_end"])}}
        if item.get("source_format") != "markdown":
            entry["original"] = text(item["before"])
        out.append(entry)
    return {"proposals": out,
            "issues": [_issue(i["code"], cp(i["byte_start"]), cp(i["byte_end"]), i.get("id")) for i in issues]}


def history_parse(path, source):
    return {"proposals": [{"id": v["proposal_id"], "created_at": v.get("created_at", ""),
                           "conversation_id": v.get("conversation_id", ""), **_url(v.get("conversation_url")),
                           "explanation": v["comment"],
                           "original": v["before"], "replacement": v["after"]}
                          for v in snapshots(path, source).values()], "issues": []}


def parse_result(fn):
    try:
        return fn()
    except Exception as error:  # noqa: BLE001 - every failure is a whole-file issue
        return {"proposals": [], "issues": [{"message": f"{type(error).__name__}: {error}"}]}


def main():
    corpus = json.load(open(sys.argv[1]))
    core, publisher, history = {}, {}, {}
    for case in corpus["cases"]:
        src, path = case["source"], case["path"]
        entry = {"parse": parse_result(lambda: core_parse(path, src)), "decisions": [], "classify": []}
        for d in case["decisions"]:
            try:
                entry["decisions"].append({"output": ps.decide(path, src, d["ids"], d["action"])})
            except ps.ProposalRefused as error:
                entry["decisions"].append({"refused": error.code, "message": str(error)})
            expected = d["expect"].get("output")
            entry["classify"].append(ps.classify_proposal_transition(path, src, expected) if expected is not None else None)
        core[case["id"]] = entry
        publisher[case["id"]] = {"parse": parse_result(lambda: publisher_parse(path, src))}
        history[case["id"]] = {"parse": parse_result(lambda: history_parse(path, src))}
    caps = {"spans": True, "issues": True, "issue_codes": True, "decisions": True, "refusal_codes": True, "fields": FIELDS}
    json.dump({
        "server-core": {"capabilities": caps, "cases": core},
        "server-publisher": {"capabilities": {**caps, "decisions": False, "explanation_trimmed": True,
                                              "markdown_original": False}, "cases": publisher},
        "server-history": {"capabilities": {**caps, "spans": False, "issues": False, "decisions": False}, "cases": history},
    }, open(sys.argv[2], "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
