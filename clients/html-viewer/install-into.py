#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Vendor the shared HTML viewer into an app.

    clients/html-viewer/install-into.py <app-asset-dir>            copy + write html-viewer.json
    clients/html-viewer/install-into.py <app-asset-dir> --check    verify the vendored copy (CI)
    clients/html-viewer/install-into.py <app-asset-dir> --force    allow replacing a newer copy

Copies the viewer files (inject order: geometry.js, text.js, viewer.js; viewer.css is
passed to load()) and VERSION into <app-asset-dir>, and writes html-viewer.json with the
version, protocol, inject order and the SHA-256 of every file. Refuses to downgrade.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
INJECT = ["geometry.js", "text.js", "viewer.js"]
FILES = INJECT + ["viewer.css", "VERSION"]
MANIFEST = "html-viewer.json"


def version_tuple(text: str) -> tuple[int, ...]:
    if not re.fullmatch(r"\d+\.\d+\.\d+", text):
        raise SystemExit(f"bad version {text!r}")
    return tuple(int(part) for part in text.split("."))


def source_version() -> tuple[str, int]:
    version = (HERE / "VERSION").read_text().strip()
    version_tuple(version)
    viewer = (HERE / "viewer.js").read_text()
    js_version = re.search(r'const VERSION = "([^"]+)";', viewer)
    js_protocol = re.search(r"const PROTOCOL = (\d+);", viewer)
    if not js_version or js_version.group(1) != version:
        raise SystemExit(f"viewer.js VERSION {js_version and js_version.group(1)!r} != VERSION file {version!r}")
    if not js_protocol:
        raise SystemExit("viewer.js has no PROTOCOL")
    return version, int(js_protocol.group(1))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest() -> dict:
    version, protocol = source_version()
    data = {"name": "thoughts-html-viewer", "version": version, "protocol": protocol, "inject": INJECT,
            "css": "viewer.css", "files": {name: sha256(HERE / name) for name in FILES}}
    try:
        commit = subprocess.run(["git", "-C", str(HERE), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "-C", str(HERE), "status", "--porcelain", "--", "."], capture_output=True, text=True, check=True).stdout.strip()
        data["source_commit"] = commit + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        pass
    return data


def check(target: Path, wanted: dict) -> list[str]:
    problems = []
    try:
        vendored = json.loads((target / MANIFEST).read_text())
    except (OSError, ValueError):
        return [f"{target / MANIFEST} missing or unreadable"]
    if vendored.get("version") != wanted["version"]:
        problems.append(f"vendored version {vendored.get('version')} != {wanted['version']}")
    for name, digest in wanted["files"].items():
        path = target / name
        if not path.exists():
            problems.append(f"{name} missing")
        elif sha256(path) != digest:
            problems.append(f"{name} differs from version {wanted['version']}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path, help="the app's asset directory for the viewer")
    parser.add_argument("--check", action="store_true", help="only verify the vendored copy")
    parser.add_argument("--force", action="store_true", help="replace a newer vendored version")
    args = parser.parse_args()
    wanted = manifest()
    target = args.target
    if args.check:
        problems = check(target, wanted)
        for problem in problems:
            print(f"html-viewer: {problem}", file=sys.stderr)
        if not problems:
            print(f"html-viewer {wanted['version']} up to date in {target}")
        return 1 if problems else 0
    old = None
    if (target / MANIFEST).exists():
        try:
            old = json.loads((target / MANIFEST).read_text())
        except ValueError:
            old = None
    if old and not args.force and version_tuple(str(old.get("version", "0.0.0"))) > version_tuple(wanted["version"]):
        raise SystemExit(f"{target} has html-viewer {old['version']}, newer than {wanted['version']} (use --force)")
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        shutil.copyfile(HERE / name, target / name)
    if old:
        for name in set(old.get("files", {})) - set(FILES):
            stale = target / name
            if stale.is_file() and stale.parent == target:
                stale.unlink()
    (target / MANIFEST).write_text(json.dumps(wanted, indent=2, sort_keys=True) + "\n")
    print(f"html-viewer {wanted['version']} (protocol {wanted['protocol']}) installed into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
