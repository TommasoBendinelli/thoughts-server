# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Cache deterministic PDF transformations after their current targets are validated."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from pathlib import Path

from .component_cache import digest


def location(pdf: Path, stage: str, slot: str, arguments, implementation: Path):
    configured = os.environ.get('THOUGHTS_LATEX_CACHE_ROOT')
    if not configured:
        return None
    key = hashlib.sha256(json.dumps([digest(pdf), arguments, digest(implementation), digest(Path(__file__))], sort_keys=True).encode()).hexdigest()
    name = hashlib.sha256(slot.encode()).hexdigest()
    return Path(configured) / stage / (name + '.zip'), key


def restore(location, pdf: Path) -> bool:
    if location is None:
        return False
    path, key = location
    try:
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read('metadata.json'))
            if metadata['key'] != key:
                return False
            raw = archive.read('document.pdf')
            if hashlib.sha256(raw).hexdigest() != metadata['sha256']:
                return False
        with tempfile.NamedTemporaryFile(dir=pdf.parent, delete=False) as temporary:
            temp = Path(temporary.name)
            temporary.write(raw)
        try:
            temp.chmod(pdf.stat().st_mode)
            os.replace(temp, pdf)
        finally:
            temp.unlink(missing_ok=True)
        return True
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
        return False


def save(location, pdf: Path):
    if location is None:
        return
    path, key = location
    temp = None
    try:
        raw = pdf.read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temp = Path(temporary.name)
        with zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('document.pdf', raw)
            archive.writestr('metadata.json', json.dumps({'key': key, 'sha256': hashlib.sha256(raw).hexdigest()}))
        os.replace(temp, path)
    except OSError:
        pass
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
