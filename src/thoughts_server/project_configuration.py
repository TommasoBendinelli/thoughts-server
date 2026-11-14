# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""One authoritative manifest in each Project's default source folder.

Source references use a stable folder UUID and a folder-relative path. Historical
renderers consume a v4 projection; it is never a second configuration authority.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import posixpath
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from . import default_folder_segment as segments
from .uniform_manifest import BUILD_FIELDS, FIELDS

VERSION = 5
KIND = "thoughts-project"
FILENAME = "thoughts-project.json"


def default_folder(project_id: str, segment: str | None = None) -> str:
    """The default folder this server creates: the Library's current segment.

    An existing Project's folder is the one its manifest declares
    (configured_default_folder); before the default-folder migration both are
    ``documents``, afterwards both are ``default``.
    """
    return ".thoughts/projects/" + hashlib.sha256(project_id.encode()).hexdigest() + "/" + (segment or segments.current())


def default_folders(project_id: str) -> tuple[str, str]:
    """Both accepted spellings of a Project's default folder, canonical first."""
    return tuple(default_folder(project_id, segment) for segment in segments.SEGMENTS)


def manifest_path(project_id: str, segment: str | None = None) -> str:
    return default_folder(project_id, segment) + "/" + FILENAME


def manifest_paths(project_id: str) -> tuple[str, str]:
    """Both accepted manifest locations of a Project, canonical first."""
    return tuple(folder + "/" + FILENAME for folder in default_folders(project_id))


_MANIFEST_PATH = re.compile(r"\.thoughts/projects/[a-f0-9]{64}/(?:default|documents)/thoughts-project\.json")
MANIFEST_LIKE_PATTERNS = tuple(".thoughts/projects/%/" + segment + "/" + FILENAME for segment in segments.SEGMENTS)


def is_manifest_path(path: str) -> bool:
    return isinstance(path, str) and _MANIFEST_PATH.fullmatch(path) is not None


def manifest_path_filter(column):
    """SQL condition: column names a Project manifest in either default-folder spelling."""
    from sqlalchemy import or_
    return or_(*(column.like(pattern) for pattern in MANIFEST_LIKE_PATTERNS))


def configured_default_folder(value: dict) -> str:
    """The default folder a (validated) Project manifest declares."""
    return next(folder["path"] for folder in value["source_folders"] if folder["folder_id"] == value["default_folder_id"])


def configured_manifest_path(value: dict) -> str:
    """Where a Project manifest lives: inside the default folder it declares."""
    return configured_default_folder(value) + "/" + FILENAME


def manifest_files(root: Path) -> list[Path]:
    """Every Project manifest on disk, one per Project: ``default/`` wins over ``documents/``."""
    base = root / ".thoughts/projects"
    result = []
    try:
        candidates = sorted(base.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    for directory in candidates:
        if not re.fullmatch(r"[a-f0-9]{64}", directory.name):
            continue
        for segment in segments.SEGMENTS:
            path = directory / segment / FILENAME
            if path.is_file() or path.is_symlink():
                result.append(path)
                break
    return result


def _parsed(path: str, content: bytes) -> dict | None:
    """The validated manifest stored at path, or None."""
    if not is_manifest_path(path):
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate Project field")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique_object)
        validate(value)
        if configured_manifest_path(value) != path:
            return None
        return value
    except (ValueError, TypeError, UnicodeError):
        return None


def semantic_identity(path: str, content: bytes) -> str | None:
    """Compare valid authority without changing the exact bytes used for hashes."""
    value = _parsed(path, content)
    if value is None:
        return None
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, UnicodeError):
        return None


class _Clash(Exception):
    pass


_MISSING = object()
# Lists of objects merged entry by entry; any other list must not change on both sides.
_KEYED_LISTS = {"documents": "document_id", "source_folders": "folder_id"}


def _merge_value(base, current, incoming, key=None):
    if current == incoming:
        return current
    if base == current:
        return incoming
    if base == incoming:
        return current
    if all(isinstance(value, dict) for value in (base, current, incoming)):
        result = {}
        for name in list(current) + [name for name in incoming if name not in current]:
            merged = _merge_value(base.get(name, _MISSING), current.get(name, _MISSING), incoming.get(name, _MISSING), name)
            if merged is not _MISSING:
                result[name] = merged
        return result
    if key in _KEYED_LISTS and all(isinstance(value, list) for value in (base, current, incoming)):
        return _merge_keyed_list(base, current, incoming, _KEYED_LISTS[key])
    raise _Clash


def _merge_keyed_list(base, current, incoming, identity):
    entries = [{item[identity]: item for item in side} for side in (base, current, incoming)]
    orders = [[item[identity] for item in side] for side in (base, current, incoming)]
    merged = {}
    for name in orders[1] + orders[2] + orders[0]:
        if name not in merged:
            value = _merge_value(*(side.get(name, _MISSING) for side in entries))
            merged[name] = value
    merged = {name: value for name, value in merged.items() if value is not _MISSING}

    def reordered(order):
        common = [name for name in order if name in entries[0]]
        return common != [name for name in orders[0] if name in entries[0] and name in set(order)]

    current_moved, incoming_moved = reordered(orders[1]), reordered(orders[2])
    if current_moved and incoming_moved:
        shared = set(orders[1]) & set(orders[2])
        if [name for name in orders[1] if name in shared] != [name for name in orders[2] if name in shared]:
            raise _Clash
    # The incoming order wins unless only the current side reordered.
    primary, secondary = (orders[1], orders[2]) if current_moved and not incoming_moved else (orders[2], orders[1])
    result = [name for name in primary if name in merged]
    # An entry only the other side has goes before the entry that follows it there, else last.
    for index in range(len(secondary) - 1, -1, -1):
        name = secondary[index]
        if name in merged and name not in result:
            following = next((later for later in secondary[index + 1:] if later in result), None)
            result.insert(len(result) if following is None else result.index(following), name)
    return [merged[name] for name in result]


def merge(path: str, base: bytes, current: bytes, incoming: bytes) -> bytes | None:
    """Three-way merge of a Project manifest by field and by document, or None on a real clash.

    Both sides adding the same document, or one adding while the other reorders, are
    not conflicts. The result keeps current's bytes when it is equivalent to them.
    """
    values = [_parsed(path, content) for content in (base, current, incoming)]
    if any(value is None for value in values):
        return None
    try:
        merged = _merge_value(*values)
        validate(merged)
    except (_Clash, ValueError, TypeError, KeyError):
        return None
    if merged == values[1]:
        return current
    return (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode()


def _uuid(value, *, document=False):
    if not isinstance(value, str):
        raise ValueError("A permanent UUID is required")
    parsed = uuid.UUID(value)
    if str(parsed) != value or (document and parsed.version != 4):
        raise ValueError("Invalid permanent UUID")
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or value == "."
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
            or "\\" in value or posixpath.normpath(value) != value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Expected a safe folder-relative path")
    return value


def validate(value: dict) -> dict:
    if (not isinstance(value, dict) or value.get("schema_version") != VERSION or value.get("kind") != KIND
            or set(value) - {"schema_version", "kind", "id", "name", "default_folder_id", "source_folders",
                             "documents", "rendering", "document_names"}
            or not isinstance(value.get("id"), str) or not value["id"]
            or not isinstance(value.get("name"), str)
            or not isinstance(value.get("source_folders"), list)
            or not isinstance(value.get("documents"), list)):
        raise ValueError("Invalid Project configuration")
    folders, locations, aggregate_ids = {}, set(), set()
    default_id = _uuid(value.get("default_folder_id"))
    for folder in value["source_folders"]:
        if not isinstance(folder, dict) or set(folder) != {"folder_id", "path", "document_id"}:
            raise ValueError("Invalid source-folder binding")
        folder_id = _uuid(folder["folder_id"])
        aggregate = _uuid(folder["document_id"], document=folder_id != default_id)
        location = _relative(folder["path"])
        if folder_id in folders or location.casefold() in locations or aggregate in aggregate_ids:
            raise ValueError("Duplicate source-folder identity or location")
        if folder_id == default_id:
            if location not in default_folders(value["id"]):
                raise ValueError("Default source folder belongs to another Project")
        elif not re.fullmatch(r"docs-[A-Za-z0-9_.-]+", location):
            raise ValueError("Invalid connected source-folder location")
        folders[folder_id] = folder
        locations.add(location.casefold())
        aggregate_ids.add(aggregate)
    if default_id not in folders:
        raise ValueError("Missing default source folder")
    identifiers, sources = set(aggregate_ids), set()
    by_id = {}
    for document in value["documents"]:
        if not isinstance(document, dict) or set(document) != FIELDS:
            raise ValueError("Every document requires exactly document_id, source, format, and included")
        doc_id = _uuid(document["document_id"], document=True)
        if doc_id in identifiers:
            raise ValueError("Duplicate permanent document identity")
        identifiers.add(doc_id)
        source = document["source"]
        if not isinstance(source, dict) or set(source) != {"folder_id", "path"} or source["folder_id"] not in folders:
            raise ValueError("Document references an unknown source folder")
        path = _relative(source["path"])
        kind = document["format"]
        if kind not in {"latex", "markdown", "pdf"} or type(document["included"]) is not bool:
            raise ValueError("Invalid document format or inclusion")
        if kind == "pdf":
            if source["folder_id"] != default_id or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.pdf", path):
                raise ValueError("Imported PDF must belong to the Project's default source folder")
        elif (PurePosixPath(path).suffix.lower() != ".tex" if kind == "latex"
              else PurePosixPath(path).suffix.lower() not in {".md", ".markdown"}):
            raise ValueError("Invalid editable document source")
        key = (source["folder_id"], path.casefold())
        if key in sources:
            raise ValueError("Duplicate document source")
        sources.add(key)
        by_id[doc_id] = document
    rendering = value.get("rendering", {})
    if not isinstance(rendering, dict):
        raise ValueError("Invalid rendering metadata")
    for key, options in rendering.items():
        if (key not in by_id or not isinstance(options, dict) or set(options) - BUILD_FIELDS
                or any(not isinstance(v, str) or not v for v in options.values())):
            raise ValueError("Invalid rendering metadata")
        kind = by_id[key]["format"]
        if kind == "pdf" and set(options) != {"sha256"}:
            raise ValueError("PDF rendering metadata must contain its fingerprint")
        if "sha256" in options and (kind != "pdf" or not re.fullmatch(r"[a-f0-9]{64}", options["sha256"])):
            raise ValueError("Invalid PDF fingerprint")
        if "working_directory" in options:
            if kind != "latex":
                raise ValueError("Invalid compiler working directory")
            if options["working_directory"] != ".":
                _relative(options["working_directory"])
    for key, document in by_id.items():
        if document["format"] == "pdf" and "sha256" not in rendering.get(key, {}):
            raise ValueError("Missing PDF fingerprint")
    names = value.get("document_names", {})
    if not isinstance(names, dict) or any(k not in by_id or not isinstance(v, str) for k, v in names.items()):
        raise ValueError("Invalid document names")
    return value


def resolve(value: dict, document: dict) -> str:
    folder = next(f for f in value["source_folders"] if f["folder_id"] == document["source"]["folder_id"])
    return folder["path"] + "/" + document["source"]["path"]


def projection(value: dict, owner: str) -> dict:
    """Return an ephemeral v4 view for a connected folder's compiler."""
    validate(value)
    folder = next((f for f in value["source_folders"] if f["path"] == owner), None)
    if folder is None:
        raise ValueError("Source folder does not belong to this Project")
    documents, rendering = [], {}
    for original in value["documents"]:
        source = original["source"]
        if source["folder_id"] != folder["folder_id"] and original["format"] != "pdf":
            continue
        entry = copy.deepcopy(original)
        entry["source"] = (posixpath.relpath(resolve(value, original), owner)
                           if original["format"] == "pdf" else source["path"])
        documents.append(entry)
        if entry["document_id"] in value.get("rendering", {}):
            rendering[entry["document_id"]] = copy.deepcopy(value["rendering"][entry["document_id"]])
    return {"schema_version": 4, "document_id": folder["document_id"], "documents": documents,
            **({"rendering": rendering} if rendering else {})}


def replace_projection(value: dict, owner: str, incoming: dict) -> dict:
    """Apply a legacy client mutation without disturbing other folders or global order."""
    from .uniform_manifest import storage, validate as validate_v4
    incoming = storage(incoming)
    validate_v4(incoming, owner)
    result = copy.deepcopy(validate(value))
    folder = next((f for f in result["source_folders"] if f["path"] == owner), None)
    if folder is None or incoming["document_id"] != folder["document_id"]:
        raise ValueError("Source-folder identity changed")
    replacements = []
    for raw in incoming["documents"]:
        document = copy.deepcopy(raw)
        if document["format"] == "pdf":
            # A client that has not migrated yet may still spell the folder ``documents``.
            name = PurePosixPath(raw["source"]).name
            if posixpath.normpath(owner + "/" + raw["source"]) not in {
                    folder + "/" + name for folder in default_folders(result["id"])}:
                raise ValueError("PDF belongs to another Project")
            document["source"] = {"folder_id": result["default_folder_id"], "path": PurePosixPath(raw["source"]).name}
        else:
            document["source"] = {"folder_id": folder["folder_id"], "path": raw["source"]}
        replacements.append(document)
    # Replace only the affected slots, retaining interleaving with other folders.
    replacement_iter = iter(replacements)
    result["documents"] = []
    removed_ids = set()
    for old in value["documents"]:
        if old["source"]["folder_id"] == folder["folder_id"] or old["format"] == "pdf":
            removed_ids.add(old["document_id"])
            replacement = next(replacement_iter, None)
            if replacement is not None:
                result["documents"].append(replacement)
        else:
            result["documents"].append(copy.deepcopy(old))
    result["documents"].extend(replacement_iter)
    metadata = result.setdefault("rendering", {})
    for key in removed_ids:
        metadata.pop(key, None)
    metadata.update(copy.deepcopy(incoming.get("rendering", {})))
    valid = {d["document_id"] for d in result["documents"]}
    result["document_names"] = {k: v for k, v in result.get("document_names", {}).items() if k in valid}
    return validate(result)


_READ_CACHE: dict[str, tuple[tuple, dict]] = {}


def _file_identity(path: Path) -> tuple:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def read(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError("Project configuration must not be a symlink")
    # The catalogue resolves each folder's Project by scanning every
    # configuration, so unchanged files are parsed and validated once.
    key = path.as_posix()
    identity = _file_identity(path)
    cached = _READ_CACHE.get(key)
    if cached is not None and cached[0] == identity:
        return copy.deepcopy(cached[1])
    value = validate(json.loads(path.read_bytes()))
    if path.as_posix().endswith("/" + configured_manifest_path(value)) is False:
        raise ValueError("Project configuration is stored outside its default folder")
    if _file_identity(path) == identity:
        _READ_CACHE[key] = (identity, copy.deepcopy(value))
    return value


def selected_source_folders(value: dict) -> set[str]:
    """Folders with selected source documents, including documents without PDFs."""
    selected = {document["source"]["folder_id"] for document in value["documents"]
                if document["included"] and document["format"] in {"latex", "markdown"}}
    return {folder["path"] for folder in value["source_folders"] if folder["folder_id"] in selected}


_OWNERS_CACHE: dict[str, tuple[tuple, dict[str, list[Path]]]] = {}


def find(root: Path, owner: str) -> tuple[Path, dict] | None:
    # Every source folder of the catalogue asks this; index all configurations
    # once per unchanged set of files instead of parsing them all each time.
    paths = manifest_files(root)
    identity = tuple((path.as_posix(), *_link_identity(path)) for path in paths)
    cached = _OWNERS_CACHE.get(root.as_posix())
    if cached is None or cached[0] != identity:
        owners: dict[str, list[Path]] = {}
        for path in paths:
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError("Project configuration escapes the workspace")
            for folder in read(path)["source_folders"]:
                owners.setdefault(folder["path"], []).append(path)
        cached = (identity, owners)
        _OWNERS_CACHE[root.as_posix()] = cached
    matches = cached[1].get(owner, [])
    if len(matches) > 1:
        raise ValueError("Source folder belongs to multiple Projects")
    return (matches[0], read(matches[0])) if matches else None


def _link_identity(path: Path) -> tuple:
    stat = path.lstat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def save(root: Path, value: dict, *, expected_sha256: str | None = None) -> Path:
    validate(value)
    path = root / configured_manifest_path(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Project configuration escapes the workspace")
    if expected_sha256 is not None:
        actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else hashlib.sha256(b"").hexdigest()
        if actual != expected_sha256:
            raise ValueError("Project configuration changed; reload before saving")
    content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    if path.exists() and path.read_bytes() == content:
        return path
    fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def from_legacy(project: dict, manifests: dict[str, dict]) -> dict:
    """Plan a migration from an explicit live Project; never discover new Projects."""
    from .uniform_manifest import storage, validate as validate_v4
    key = project["id"]
    default_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "thoughts:default-folder:" + key))
    result = {"schema_version": VERSION, "kind": KIND, "id": key, "name": project["name"],
              "default_folder_id": default_id, "source_folders": [
                  {"folder_id": default_id, "path": default_folder(key), "document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "thoughts:default-document:" + key))}],
              "documents": [], "rendering": {}, "document_names": {}}
    owners = list(dict.fromkeys(filter(None, [project.get("source_project"),
                                             *(f["project"] for f in project.get("source_folders", []))])))
    by_id = {}
    aliases = {}
    for owner in owners:
        if owner not in manifests:
            raise ValueError("Missing source-folder manifest: " + owner)
        manifest = storage(manifests[owner])
        validate_v4(manifest, owner)
        folder_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "thoughts:source-folder:" + manifest["document_id"]))
        result["source_folders"].append({"folder_id": folder_id, "path": owner, "document_id": manifest["document_id"]})
        for raw in manifest["documents"]:
            document = copy.deepcopy(raw)
            if raw["format"] == "pdf":
                name = PurePosixPath(raw["source"]).name
                if posixpath.normpath(owner + "/" + raw["source"]) not in {
                        folder + "/" + name for folder in default_folders(key)}:
                    raise ValueError("PDF belongs to another Project")
                document["source"] = {"folder_id": default_id, "path": PurePosixPath(raw["source"]).name}
                alias = "third_party/" + document["source"]["path"]
            else:
                document["source"] = {"folder_id": folder_id, "path": raw["source"]}
                alias = owner + "/" + raw["source"]
            doc_id = document["document_id"]
            if doc_id in by_id and by_id[doc_id] != document:
                raise ValueError("Conflicting copies of a document must be reconciled")
            by_id[doc_id] = document
            aliases[alias] = doc_id
            options = manifest.get("rendering", {}).get(doc_id)
            if options:
                previous = result["rendering"].get(doc_id)
                if previous is not None and previous != options:
                    raise ValueError("Conflicting document rendering metadata")
                result["rendering"][doc_id] = copy.deepcopy(options)
    ordered = []
    for entry in project.get("documents", []):
        doc_id = entry.get("document_id") or aliases.get(entry.get("id"))
        if not doc_id:
            raise ValueError("Project document has no permanent identity")
        if doc_id not in by_id:
            if entry.get("source_file") and not entry["source_file"].startswith("third_party/"):
                raise ValueError("Project document is missing from its source manifest")
            filename = entry.get("attachment_filename") or entry.get("filename")
            by_id[doc_id] = {"document_id": doc_id, "source": {"folder_id": default_id, "path": filename},
                             "format": "pdf", "included": entry.get("included", True)}
            result["rendering"][doc_id] = {"sha256": entry["sha256"]}
        if doc_id not in ordered:
            ordered.append(doc_id)
        if entry.get("name"):
            result["document_names"][doc_id] = entry["name"]
    ordered.extend(key for key in by_id if key not in ordered)
    result["documents"] = [by_id[key] for key in ordered]
    return validate(result)


def find_canonical(session, store, owner: str) -> tuple[str, dict] | None:
    """Resolve from versioned objects rather than potentially stale worktree bytes."""
    from sqlalchemy import select
    from .models import SourceFile
    matches = []
    records = session.scalars(select(SourceFile).where(
        SourceFile.deleted.is_(False), manifest_path_filter(SourceFile.path)))
    for record in records:
        raw = store.path(record.object_sha256).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record.sha256:
            raise ValueError("Project configuration failed verification")
        value = validate(json.loads(raw))
        if record.path != configured_manifest_path(value):
            raise ValueError("Project configuration belongs to another Project")
        if any(f["path"] == owner for f in value["source_folders"]):
            matches.append((record.path, value))
    if len(matches) > 1:
        raise ValueError("Source folder belongs to multiple Projects")
    return matches[0] if matches else None


def register(session, value: dict) -> None:
    from .document_identity import register_source_manifest, register_document
    validate(value)
    editable = {document["source"]["folder_id"] for document in value["documents"]
                if document["format"] in {"latex", "markdown"}}
    for folder in value["source_folders"]:
        # The default folder registers only when it declares its own editable documents.
        if folder["folder_id"] != value["default_folder_id"] or folder["folder_id"] in editable:
            register_source_manifest(session, folder["path"], projection(value, folder["path"]))
    for document in value["documents"]:
        if document["format"] == "pdf":
            register_document(session, document["document_id"], document["source"]["path"])
    # Empty/PDF-only Projects have no compiler aggregate to register.


def canonical_manifest_record(session, project_id: str):
    """The live manifest SourceFile of a Project, in whichever default-folder spelling it has."""
    from .models import SourceFile
    for path in manifest_paths(project_id):
        record = session.get(SourceFile, path)
        if record is not None and not record.deleted:
            return record
    return None


def current_manifest_path(session, project_id: str) -> str:
    """Where a Project's manifest is saved: its live location, else where this server creates one."""
    record = canonical_manifest_record(session, project_id)
    return record.path if record is not None else manifest_path(project_id)


def canonical_for_project(session, store, project_id: str) -> dict | None:
    record = canonical_manifest_record(session, project_id)
    if record is None:
        return None
    raw = store.path(record.object_sha256).read_bytes()
    if hashlib.sha256(raw).hexdigest() != record.sha256:
        raise ValueError("Project configuration failed verification")
    value = validate(json.loads(raw))
    if value["id"] != project_id or record.path != configured_manifest_path(value):
        raise ValueError("Project configuration belongs to another Project")
    return value


_VERIFIED_OBJECTS: dict[tuple[str, str], dict] = {}


def canonical_projects(session, store) -> list[dict]:
    """Load and validate the complete current Project authority without writes."""
    from sqlalchemy import select
    from .models import SourceFile
    result, ids, owners = [], set(), set()
    records = session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False),
        manifest_path_filter(SourceFile.path)))
    for record in records:
        # Objects are immutable: verify and validate each configuration once.
        verified = _VERIFIED_OBJECTS.get((record.object_sha256, record.sha256))
        if verified is None:
            raw = store.path(record.object_sha256).read_bytes()
            if hashlib.sha256(raw).hexdigest() != record.sha256:
                raise ValueError("Project configuration failed verification: " + record.path)
            verified = validate(json.loads(raw))
            if len(_VERIFIED_OBJECTS) > 1024:
                _VERIFIED_OBJECTS.clear()
            _VERIFIED_OBJECTS[(record.object_sha256, record.sha256)] = verified
        value = copy.deepcopy(verified)
        if record.path != configured_manifest_path(value) or value["id"] in ids:
            raise ValueError("Project configuration has a conflicting identity: " + record.path)
        ids.add(value["id"])
        for folder in value["source_folders"]:
            if folder["path"] in owners:
                raise ValueError("Source folder belongs to multiple Projects: " + folder["path"])
            owners.add(folder["path"])
        result.append(value)
    return result


def declared_filename(value: dict, declaration: dict) -> str:
    """The PDF filename a declared document has in the catalogue before anything of it is published."""
    if declaration["format"] == "pdf":
        return declaration["source"]["path"]
    from .project_documents import document_output
    from .source_owner import owner_of
    location = resolve(value, declaration)
    owner = owner_of(location)
    options = value.get("rendering", {}).get(declaration["document_id"], {})
    return document_output(owner, {"source": location.removeprefix(owner + "/"), "format": declaration["format"],
                                   **({"output": options["output"]} if "output" in options else {})})


def catalogue_entry(entry: dict, value: dict) -> None:
    """Overlay membership and global order without changing the authority on reads."""
    validate(value)
    current = {d.get("document_id"): d for d in entry["documents"] if d.get("document_id")}
    paths = {d.get("source_file") or d.get("id"): d for d in entry["documents"]}
    documents, unpublished = [], []
    for declaration in value["documents"]:
        published = declaration["included"]
        location = resolve(value, declaration)
        kind = declaration["format"]
        alias = "third_party/" + declaration["source"]["path"] if kind == "pdf" else location
        doc_id = declaration["document_id"]
        document = copy.deepcopy(current.get(doc_id) or paths.get(alias) or {})
        document.update(document_id=doc_id, id=document.get("id", alias), included=published, published=published)
        document["name"] = value.get("document_names", {}).get(doc_id, document.get("name") or PurePosixPath(location).stem)
        if kind == "pdf":
            document.update(imported=True, attachment_filename=declaration["source"]["path"], managed_path=location)
            document.setdefault("filename", declaration["source"]["path"])
            document.setdefault("sha256", value["rendering"][doc_id]["sha256"])
        else:
            from .source_owner import owner_of
            owner = owner_of(location)
            document.update(source_file=location, source_project=owner)
            document.setdefault("filename", declared_filename(value, declaration))
            document.setdefault("sha256", "")
        document.setdefault("start_page", 0)
        document.setdefault("size_bytes", -1)
        if document["start_page"] == 0 and published:
            document["pending_build"] = True
        # Older clients read `documents` as the Project's members, so an
        # unpublished document is only listed apart
        # (protocol/project-document-publication.md §3).
        (documents if published else unpublished).append(document)
    entry["name"] = value["name"]
    entry["documents"] = documents
    entry["unpublished_documents"] = unpublished
    entry["project_configuration"] = copy.deepcopy(value)
    directory = configured_default_folder(value)
    files = [{"id": configured_manifest_path(value), "name": FILENAME, "relative_path": FILENAME, "size_bytes": -1}]
    files.extend({"id": resolve(value, d), "name": d["source"]["path"], "relative_path": d["source"]["path"], "size_bytes": -1}
                 for d in value["documents"] if d["format"] == "pdf")
    entry["sources"] = [s for s in entry.get("sources", []) if s.get("id") != directory]
    entry["sources"].insert(0, {"id": directory, "name": "Default source folder", "kind": "local",
                                "folder_id": value["default_folder_id"], "files": files})


def project_documents(entry: dict) -> list[dict]:
    """A catalogue entry's documents, published ones first, then unpublished ones."""
    return list(entry.get("documents", [])) + list(entry.get("unpublished_documents", []))


def apply_library_change(value: dict, action: str, request: dict, current: dict, data: dict) -> dict:
    result = copy.deepcopy(validate(value))
    if action == "rename":
        result["name"] = data["name"]
    elif action == "rename_document":
        document = next(d for d in project_documents(current) if request["document_id"] in {d.get("document_id"), d["id"]})
        result.setdefault("document_names", {})[document["document_id"]] = request["name"]
    elif action == "order":
        aliases = {alias: d["document_id"] for d in current["documents"] for alias in (d["id"], d["document_id"])}
        order = [aliases[key] for key in request["documents"]]
        selected = {d["document_id"]: d for d in result["documents"]}
        ordered = iter(selected[key] for key in order)
        # Keep unchecked documents in their existing positions.
        result["documents"] = [next(ordered) if d["document_id"] in order else d for d in result["documents"]]
    elif action == "remove_document":
        document = next(d for d in project_documents(current) if request["document_id"] in {d.get("document_id"), d["id"]})
        doc_id = document["document_id"]
        result["documents"] = [d for d in result["documents"] if d["document_id"] != doc_id]
    elif action == "set_document_published":
        document = next(d for d in project_documents(current) if request["document_id"] in {d.get("document_id"), d["id"]})
        for declaration in result["documents"]:
            if declaration["document_id"] == document["document_id"]:
                declaration["included"] = request["published"]
    elif action == "remove_source":
        folder_ids = {f["folder_id"] for f in result["source_folders"] if f["path"] == request["source_project"]}
        result["source_folders"] = [f for f in result["source_folders"] if f["folder_id"] not in folder_ids]
        result["documents"] = [d for d in result["documents"] if d["source"]["folder_id"] not in folder_ids]
    elif action == "import":
        incoming = next(d for d in data["imports"] if d["filename"] == request["filename"])
        if incoming["document_id"] not in {d["document_id"] for d in result["documents"]}:
            result["documents"].append({"document_id": incoming["document_id"],
                "source": {"folder_id": result["default_folder_id"], "path": incoming["filename"]}, "format": "pdf", "included": True})
        result.setdefault("rendering", {})[incoming["document_id"]] = {"sha256": incoming["sha256"]}
        result.setdefault("document_names", {})[incoming["document_id"]] = incoming["name"]
    retained = {d["document_id"] for d in result["documents"]}
    for field in ("rendering", "document_names"):
        result[field] = {k: v for k, v in result.get(field, {}).items() if k in retained}
    return validate(result)


def transfer_folder(source: dict, destination: dict, owner: str) -> tuple[dict, dict]:
    """Transfer ownership, including unchecked documents, without rewriting identities."""
    source, destination = copy.deepcopy(validate(source)), copy.deepcopy(validate(destination))
    folder = next((f for f in source["source_folders"] if f["path"] == owner), None)
    if folder is None or folder["folder_id"] == source["default_folder_id"]:
        raise ValueError("Choose an additional connected source folder")
    if any(f["folder_id"] == folder["folder_id"] or f["path"] == owner for f in destination["source_folders"]):
        raise ValueError("Destination already owns this source folder")
    moved = [d for d in source["documents"] if d["source"]["folder_id"] == folder["folder_id"]]
    moved_ids = {d["document_id"] for d in moved}
    if moved_ids.intersection(d["document_id"] for d in destination["documents"]):
        raise ValueError("Destination already owns one of these documents")
    source["source_folders"].remove(folder)
    destination["source_folders"].append(folder)
    source["documents"] = [d for d in source["documents"] if d["document_id"] not in moved_ids]
    destination["documents"].extend(moved)
    for field in ("rendering", "document_names"):
        for doc_id in moved_ids:
            if doc_id in source.get(field, {}):
                destination.setdefault(field, {})[doc_id] = source[field].pop(doc_id)
    return validate(source), validate(destination)


def add_folder(value: dict, owner: str, manifest: dict) -> dict:
    result = copy.deepcopy(validate(value))
    addition = from_legacy({"id": value["id"], "name": value["name"], "source_project": owner, "documents": []}, {owner: manifest})
    if any(f["path"] == owner for f in result["source_folders"]):
        raise ValueError("Project already owns this source folder")
    result["source_folders"].extend(f for f in addition["source_folders"] if f["folder_id"] != addition["default_folder_id"])
    known = {d["document_id"] for d in result["documents"]}
    if known.intersection(d["document_id"] for d in addition["documents"]):
        raise ValueError("Imported folder contains an existing document identity")
    result["documents"].extend(addition["documents"])
    result.setdefault("rendering", {}).update(addition.get("rendering", {}))
    return validate(result)


def retire_source(session, store, root: Path, operation_id: str, path: str, *, materialize=True):
    from .library_projects import SYSTEM_DEVICE
    from .models import SourceFile, utc_now
    from .operations import latest_device_sequence
    from .schemas import SourcePublicationMutation
    from .sources import _apply_source_delete, _materialize_canonical_path
    source = session.get(SourceFile, path)
    if source is None or source.deleted:
        return
    result = _apply_source_delete(session, store, root, SYSTEM_DEVICE, SourcePublicationMutation(
        action="delete", operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id + ":retire:" + path)),
        device_sequence=latest_device_sequence(session, SYSTEM_DEVICE) + 1, path=path,
        base_revision=source.revision, base_sha256=source.sha256, base_object_sha256=source.object_sha256,
        client_timestamp=utc_now().isoformat()))
    if result.outcome not in {"applied", "duplicate"}:
        raise ValueError("Configuration changed while retiring its old location")
    if materialize:
        _materialize_canonical_path(session, store, root, path, already_locked=True)
