# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Reuse component-local review geometry before aggregate page offsets are applied."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def location(source_id: str, inputs: dict):
    root = os.environ.get("THOUGHTS_LATEX_CACHE_ROOT")
    if not root:
        return None
    slot = hashlib.sha256(source_id.encode()).hexdigest()
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return Path(root) / "source-projections" / f"{slot}.json", key


def restore(location):
    if location is None:
        return None
    path, key = location
    try:
        stored = json.loads(path.read_bytes())
        payload = stored["payload"]
        if stored["key"] != key or hashlib.sha256(payload.encode()).hexdigest() != stored["sha256"]:
            return None
        result = json.loads(payload)
        if set(result) != {"proposals", "points", "cards", "comments"}:
            return None
        if not all(isinstance(result[name], dict) for name in ("proposals", "points", "comments")):
            return None
        result["cards"] = {(card, segment): fragments for card, segment, fragments in result["cards"]}
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save(location, result):
    if location is None:
        return
    path, key = location
    temp = None
    try:
        payload = json.dumps({**result, "cards": [[*key, value] for key, value in result["cards"].items()]})
        stored = {"key": key, "sha256": hashlib.sha256(payload.encode()).hexdigest(), "payload": payload}
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(json.dumps(stored).encode())
        os.replace(temp, path)
    except OSError:
        pass
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
