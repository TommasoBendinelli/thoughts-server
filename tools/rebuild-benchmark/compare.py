# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Compare two bench.py trees: published PDFs byte for byte, artifacts by resolved content.

Usage: python compare.py BASE_WORK NEW_WORK cold nochange edit1 ...

Run both checkouts with the same WORK path (move the first tree aside), so
pdfTeX's trailer /ID, which hashes the output path, matches. Object references
are compared by content; keys that name code versions are ignored.
"""
import io, json, re, sys, zipfile, hashlib
from pathlib import Path

HEX = re.compile(r"[0-9a-f]{64}")
IGNORED = {"component_cache_key", "document_input_sha256"}

def resolve(root, value, seen):
    if isinstance(value, dict):
        return {k: resolve(root, v, seen) for k, v in value.items() if k not in IGNORED}
    if isinstance(value, list):
        return [resolve(root, v, seen) for v in value]
    if isinstance(value, str) and HEX.fullmatch(value):
        path = root / "objects" / value
        if path.is_file() and value not in seen:
            raw = path.read_bytes()
            try:
                return {"object": resolve(root, json.loads(raw), seen | {value})}
            except ValueError:
                pass
            try:
                with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                    return {"zip": {n: hashlib.sha256(archive.read(n)).hexdigest() for n in archive.namelist()}}
            except zipfile.BadZipFile:
                return {"bytes": hashlib.sha256(raw).hexdigest()}
        return "<object>" if path.is_file() else value
    return value

def load(tree, scenario):
    artifacts = Path(tree, scenario, "artifacts")
    pointer = json.loads(next((artifacts / "documents").glob("*.json")).read_text())
    return resolve(artifacts, {k: v for k, v in pointer.items()}, set())

ok = True
for scenario in sys.argv[3:]:
    a, b = load(sys.argv[1], scenario), load(sys.argv[2], scenario)
    pdfs = [sorted(Path(t, scenario, "docs/current_pdfs").glob("*.pdf")) for t in sys.argv[1:3]]
    same_pdf = [p.read_bytes() for p in pdfs[0]] == [p.read_bytes() for p in pdfs[1]]
    same = a == b
    ok &= same and same_pdf
    print(scenario, "artifacts identical" if same else "ARTIFACTS DIFFER", "| PDFs identical" if same_pdf else "| PDFS DIFFER")
    if not same:
        def diff(x, y, path=""):
            if type(x) != type(y): print(" ", path, "type"); return
            if isinstance(x, dict):
                for k in sorted(set(x) | set(y)):
                    if k not in x or k not in y: print(" ", path + "/" + k, "missing"); continue
                    diff(x[k], y[k], path + "/" + k)
            elif isinstance(x, list):
                if len(x) != len(y): print(" ", path, "len", len(x), len(y)); return
                for i, (p, q) in enumerate(zip(x, y)): diff(p, q, f"{path}[{i}]")
            elif x != y: print(" ", path, repr(x)[:80], repr(y)[:80])
        diff(a, b)
sys.exit(0 if ok else 1)
