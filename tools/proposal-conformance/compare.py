#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Compare runner outputs (results/*.json) with protocol/fixtures/agent-proposals/cases.json.

Runner output: {impl: {"capabilities": {...}, "cases": {case_id: {"parse": {"proposals": [...],
"issues": [...]}, "decisions": [...], "classify": [...]}}, "writers": {writer_id: text}}}

Two verdicts per case:
  behavioral  the valid proposals (fields and spans), whether any issue is reported, decision
              outputs, and which decisions are refused all match.
  strict      additionally every issue matches exactly (code, span, id) and refusals carry the
              expected code. Implementations without stable codes cannot pass strict issue cases.
Writes results/summary.json and results/summary.md.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent.parent / "protocol" / "fixtures" / "agent-proposals" / "cases.json"
corpus = json.load(open(FIXTURES))
results = {}
for path in sorted((HERE / "results").glob("*.json")):
    if path.name != "summary.json":
        results.update(json.load(open(path)))
ORDER = ["reference", "server-core", "server-publisher", "server-history", "desktop", "android-js", "vscode"]
IMPLS = [i for i in ORDER if i in results]


def compare_proposals(caps, case, expected, actual):
    problems = []
    if [p["id"] for p in expected] != [p.get("id") for p in actual]:
        return [f"proposals {[p.get('id') for p in actual]} != expected {[p['id'] for p in expected]}"]
    for e, a in zip(expected, actual):
        for field in caps["fields"]:
            if field == "original" and case["format"] == "markdown" and caps.get("markdown_original") is False:
                continue
            want, got = e.get(field), a.get(field)  # conversation_url: absent when not written
            if field == "explanation" and caps.get("explanation_trimmed"):
                want, got = want.strip(), (got or "").strip()
            if want != got:
                problems.append(f"{e['id']}.{field} {got!r} != {want!r}")
        if caps["spans"] and a.get("span") != e["span"]:
            problems.append(f"{e['id']}.span {a.get('span')} != {e['span']}")
    return problems


def check(impl, case):
    caps = results[impl]["capabilities"]
    actual = results[impl]["cases"].get(case["id"])
    if actual is None:
        return ["no result"], ["no result"]
    exp = case["expect"]
    behavioral = compare_proposals(caps, case, exp["proposals"], actual["parse"]["proposals"])
    strict = []
    if caps["issues"]:
        got_issues = actual["parse"]["issues"]
        if bool(exp["issues"]) != bool(got_issues):
            behavioral.append(f"issues {[i.get('code') or i.get('message') for i in got_issues]} but expected "
                              f"{[i['code'] for i in exp['issues']]}")
        else:
            simplified = [{k: i[k] for k in ("code", "span", "id") if k in i} for i in got_issues]
            if simplified != exp["issues"]:
                strict.append(f"issues {simplified or [i.get('message') for i in got_issues]} != {exp['issues']}")
    if caps["decisions"]:
        for d, a in zip(case["decisions"], actual["decisions"]):
            label = f"{d['action']}{d['ids']}"
            if "output" in d["expect"]:
                if "output" not in a:
                    behavioral.append(f"{label}: refused ({a.get('message')})")
                elif a["output"] != d["expect"]["output"]:
                    behavioral.append(f"{label}: output {a['output']!r} != {d['expect']['output']!r}")
            elif "output" in a:
                behavioral.append(f"{label}: succeeded (expected refusal {d['expect']['refused']})")
            elif a.get("refused") != d["expect"]["refused"]:
                strict.append(f"{label}: refusal code {a.get('refused')} != {d['expect']['refused']}")
    for d, c in zip(case["decisions"], actual.get("classify", [])):
        if "output" in d["expect"]:
            want = {"accept": {"accept", "accept_pdf"}, "reject": {"disregard"}}[d["action"]]
            if c not in want:
                behavioral.append(f"classify {d['action']}{d['ids']} -> {c}")
    return behavioral, behavioral + strict


summary = {"fixtures": str(FIXTURES), "implementations": IMPLS, "cases": {}, "totals": {}}
totals = {impl: {"behavioral": 0, "strict": 0, "total": 0, "by_status": {}} for impl in IMPLS}
for case in corpus["cases"]:
    row = {}
    for impl in IMPLS:
        behavioral, strict = check(impl, case)
        row[impl] = {"behavioral": not behavioral, "strict": not strict, "problems": strict}
        t = totals[impl]
        t["behavioral"] += not behavioral
        t["strict"] += not strict
        t["total"] += 1
        s = t["by_status"].setdefault(case["status"], [0, 0, 0])
        s[0] += not behavioral
        s[1] += not strict
        s[2] += 1
    summary["cases"][case["id"]] = {"status": case["status"], "results": row}
summary["totals"] = totals

writer_rows = {}
for impl in IMPLS:
    produced = results[impl].get("writers")
    if produced is None:
        continue
    for w in corpus["writers"]:
        if w["id"] in produced:
            writer_rows.setdefault(w["id"], {})[impl] = produced[w["id"]] == w["expect"]
summary["writers"] = writer_rows
(HERE / "results" / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False) + "\n")


def cell(r):
    return "pass" if r["strict"] else ("behav" if r["behavioral"] else "FAIL")


lines = ["# Agent-proposal conformance results", "",
         f"Fixtures: `{FIXTURES.relative_to(HERE.parent.parent) if FIXTURES.is_relative_to(HERE.parent.parent) else FIXTURES}` "
         f"({len(corpus['cases'])} cases, {len(corpus['writers'])} writer cases).",
         "`pass` = strict (issues and refusal codes exact), `behav` = behavior right but no stable codes/spans, `FAIL` = wrong behavior.",
         "", "## Totals", "", "| implementation | strict pass | behavioral pass | total |", "|---|---|---|---|"]
for impl in IMPLS:
    t = totals[impl]
    lines.append(f"| {impl} | {t['strict']} | {t['behavioral']} | {t['total']} |")
lines += ["", "| status | " + " | ".join(IMPLS) + " |", "|---|" + "---|" * len(IMPLS)]
for status in ("normative", "legacy"):
    lines.append(f"| {status} (behavioral) | " + " | ".join(
        "{}/{}".format(totals[i]["by_status"].get(status, [0, 0, 0])[0], totals[i]["by_status"].get(status, [0, 0, 0])[2]) for i in IMPLS) + " |")
if writer_rows:
    lines += ["", "## Writers", ""]
    for wid, row in writer_rows.items():
        lines.append(f"- {wid}: " + ", ".join(f"{impl} {'pass' if ok else 'FAIL'}" for impl, ok in row.items()))
    untested = [w["id"] for w in corpus["writers"] if w["id"] not in writer_rows]
    if untested:
        lines.append(f"- not exercised by any runner: {', '.join(untested)}")
lines += ["", "## Per case", "", "| case | status | " + " | ".join(IMPLS) + " |", "|---|---|" + "---|" * len(IMPLS)]
for cid, info in summary["cases"].items():
    lines.append(f"| {cid} | {info['status']} | " + " | ".join(cell(info["results"][i]) for i in IMPLS) + " |")
lines += ["", "## Failure details", ""]
for cid, info in summary["cases"].items():
    for impl in IMPLS:
        for problem in info["results"][impl]["problems"]:
            lines.append(f"- `{cid}` [{impl}] {problem}")
(HERE / "results" / "summary.md").write_text("\n".join(lines) + "\n")
print("\n".join(lines[: lines.index("## Per case")]))
