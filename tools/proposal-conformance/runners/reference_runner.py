#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Run the conformance reference (../reference.py) on the fixtures; it must pass every case.

Usage: python3 reference_runner.py cases.json out.json
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reference as ref  # noqa: E402

FIELDS = ["id", "created_at", "conversation_id", "conversation_url", "explanation", "original", "replacement"]


def main():
    corpus = json.load(open(sys.argv[1]))
    out = {}
    for case in corpus["cases"]:
        parsed = ref.parse(case["path"], case["source"])
        proposals = [{**{k: p[k] for k in FIELDS if k in p}, "span": {"start": p["start"], "end": p["end"]}} for p in parsed["proposals"]]
        issues = [{"code": i["code"], "span": {"start": i["start"], "end": i["end"]}, **({"id": i["id"]} if "id" in i else {})}
                  for i in parsed["issues"]]
        decisions = []
        for d in case["decisions"]:
            result = ref.decide(case["path"], case["source"], d["ids"], d["action"])
            decisions.append({"output": result["output"]} if result["ok"] else {"refused": result["code"]})
        out[case["id"]] = {"parse": {"proposals": proposals, "issues": issues}, "decisions": decisions}
    caps = {"spans": True, "issues": True, "issue_codes": True, "decisions": True, "refusal_codes": True, "fields": FIELDS}
    json.dump({"reference": {"capabilities": caps, "cases": out}}, open(sys.argv[2], "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
