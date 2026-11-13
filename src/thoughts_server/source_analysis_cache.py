# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reuse canonical per-component SyncTeX analysis, independent of aggregate page offsets."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from .component_cache import digest
from .renderer_tools.synctex_canonical import canonical_payload


def location(docs: Path, synctex: Path, inputs: list, queries: list, worker: Path, implementation: Path):
    configured = os.environ.get('THOUGHTS_LATEX_CACHE_ROOT')
    if not configured:
        return None
    # Requests include source hashes and every queried line range, including proposal roles.
    request = json.dumps([inputs, queries], sort_keys=True).replace(str(docs) + '/', '@/')
    key = hashlib.sha256(canonical_payload(synctex.read_bytes(), docs))
    key.update(request.encode())
    for path in (worker, implementation, Path(__file__)):
        key.update(digest(path).encode())
    slot = hashlib.sha256(json.dumps([synctex.name, [value['source_file'] for value in inputs]], sort_keys=True).encode()).hexdigest()
    return Path(configured) / 'source-analysis' / (slot + '.zip'), key.hexdigest()


def restore(location):
    if location is None:
        return None
    path, key = location
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read('metadata.json'))
            if metadata['key'] != key:
                return None
            lookup, boxes_raw = archive.read('lookup.zip'), archive.read('boxes.json')
            if hashlib.sha256(lookup + boxes_raw).hexdigest() != metadata['sha256']:
                return None
            boxes = {(identity, role): {int(page): [tuple(box) for box in values] for page, values in pages.items()}
                     for identity, role, pages in json.loads(boxes_raw)}
            return lookup, boxes
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return None


def save(location, result):
    if location is None:
        return
    path, key = location
    lookup, boxes = result
    boxes_raw = json.dumps([[identity, role, pages] for (identity, role), pages in boxes.items()], sort_keys=True).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        temp = Path(temporary.name)
    try:
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('lookup.zip', lookup, compress_type=zipfile.ZIP_STORED)
            archive.writestr('boxes.json', boxes_raw)
            archive.writestr('metadata.json', json.dumps({'key': key, 'sha256': hashlib.sha256(lookup + boxes_raw).hexdigest()}))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
