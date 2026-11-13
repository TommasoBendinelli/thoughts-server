# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Canonicalize SyncTeX artifacts for stable content identities."""

from __future__ import annotations

import gzip
import hashlib
import io
import re
from pathlib import Path

SYNCTEX_INPUT = re.compile(r"^(Input:\d+:)(.*)$")


def _canonical_input_path(value: str, docs_root: Path) -> str:
    root = docs_root.expanduser().resolve().as_posix().rstrip("/")
    normalized = value.replace("\\", "/")
    if normalized == root:
        return "."
    prefix = f"{root}/"
    if normalized.startswith(prefix):
        return normalized.removeprefix(prefix)
    return normalized


_RECENT: dict[tuple[bytes, str], bytes] = {}


def canonical_payload(raw: bytes, docs_root: Path) -> bytes:
    """Return decompressed SyncTeX with workspace-local input paths made relative.

    A publication canonicalizes the same SyncTeX for several identities, so
    the last few results are kept by content.
    """

    key = (hashlib.sha256(raw).digest(), str(docs_root))
    cached = _RECENT.get(key)
    if cached is None:
        cached = _canonical_payload(raw, docs_root)
        _RECENT[key] = cached
        while len(_RECENT) > 4:
            del _RECENT[next(iter(_RECENT))]
    return cached


def _canonical_payload(raw: bytes, docs_root: Path) -> bytes:
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        payload = compressed.read()
    lines = payload.decode("utf-8", errors="replace").splitlines(keepends=True)
    canonical = bytearray()
    previous_anchor = 0
    for line in lines:
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        match = SYNCTEX_INPUT.fullmatch(body)
        if match is not None:
            body = match.group(1) + _canonical_input_path(match.group(2), docs_root)
        elif body.startswith("!") and body[1:].isdigit():
            current_anchor = len(canonical)
            body = f"!{current_anchor - previous_anchor}"
            previous_anchor = current_anchor
        canonical.extend((body + ending).encode("utf-8"))
    return bytes(canonical)


def canonical_gzip(raw: bytes, docs_root: Path) -> bytes:
    """Return a deterministic gzip encoding of canonical SyncTeX content."""

    return gzip.compress(canonical_payload(raw, docs_root), compresslevel=9, mtime=0)


def fingerprint(path: Path, docs_root: Path) -> str:
    """Hash canonical decompressed SyncTeX, falling back to the original bytes."""

    raw = path.read_bytes()
    try:
        payload = canonical_payload(raw, docs_root)
    except (OSError, EOFError):
        payload = raw
    return hashlib.sha256(payload).hexdigest()
