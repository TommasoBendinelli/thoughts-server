# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Project containers, including empty projects, independent of PDF publications.

Project edits are explicit, idempotent Library operations. Current manifests
define containers and membership independently of PDF publications.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from .models import Device, Entity, Publication, SourceFile, utc_now

TYPE = "library_project"
RECEIPT = "library_project_receipt"
SYSTEM_DEVICE = "thoughts-library-projects"
_SOURCE_ARTIFACT_FACTS: dict[str, tuple] = {}


def retired_filenames(session):
    result = set()
    for record in session.scalars(select(Entity).where(Entity.entity_type == TYPE)):
        result.update(record.payload.get("retired_filenames", []))
        if record.deleted and "retired_filenames" not in record.payload:
            result.update(d["filename"] for d in record.payload.get("imports", []))
    from .models import DocumentIdentity
    identities = {identity.document_id: identity for identity in session.scalars(select(DocumentIdentity))}
    for publication in session.scalars(select(Publication)):
        identity = identities.get(publication.document_id)
        if publication.project and (identity is None or not identity.source_path):
            result.add(publication.filename)
    for record in session.scalars(select(Entity).where(Entity.entity_type == TYPE)):
        composition = record.payload.get("composition", {})
        filename = composition.get("filename", "")
        if re.fullmatch(r"project_[0-9a-f]{24}\.pdf", filename):
            result.add(filename)
    return result


def unselected_document_filenames(session, store) -> set[str]:
    """PDFs of documents a Project declares but does not publish.

    Unpublishing a document (or unchecking it in Manage documents) keeps its
    publication, its comments and its review cards, but the document is
    neither a build target nor part of the Library a client lists: render-state, the Library listing
    and the source-layout index all leave it out, so they agree. Checking it
    again lists the kept publication again.

    Match the publication itself (its permanent identity) and the configured
    output name: a registered identity's filename can differ from the PDF name
    a document renders to (rendering.output).
    """
    if store is None:
        return set()
    from . import project_configuration as pc
    from .models import DocumentIdentity
    try:
        configurations = pc.canonical_projects(session, store)
    except (ValueError, OSError) as error:
        logging.getLogger(__name__).warning("Cannot read Project configurations for unchecked documents: %s", error)
        return set()
    selected, outputs = set(), {}
    for configuration in configurations:
        rendering = configuration.get("rendering", {})
        for document in configuration["documents"]:
            # An imported PDF published by another Project stays listed.
            if document["included"]:
                selected.add(document["document_id"])
            else:
                outputs[document["document_id"]] = (rendering.get(document["document_id"]) or {}).get("output")
    unselected = sorted(set(outputs) - selected)
    if not unselected:
        return set()
    filenames = {output for document_id, output in outputs.items() if document_id in unselected and output}
    filenames |= set(session.scalars(select(Publication.filename).where(Publication.document_id.in_(unselected))))
    filenames |= {filename for filename in session.scalars(
        select(DocumentIdentity.filename).where(DocumentIdentity.document_id.in_(unselected))) if filename}
    return filenames


def _record(session, kind, key):
    return session.get(Entity, (kind, "library", key))


def _title(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError("Enter a project name between 1 and 200 characters without line breaks")
    return value.strip()


def project_documents(entry: dict) -> list[dict]:
    from .project_configuration import project_documents as documents
    return documents(entry)


def _selected_document(documents, identifier):
    if not isinstance(identifier, str) or not identifier:
        return None
    return next((d for d in documents if identifier in {d.get("document_id"), d.get("id")}), None)


def connected_source_folders(project: dict) -> list[dict]:
    """Connected folders have equal standing; membership comes from the manifest."""
    configuration = project.get("project_configuration") or {}
    return [folder for folder in configuration.get("source_folders", [])
            if folder["folder_id"] != configuration.get("default_folder_id")]


def _html_import_owner(current: dict, requested: str | None) -> str:
    connected = {folder["path"] for folder in connected_source_folders(current)}
    owner = requested or (next(iter(connected)) if len(connected) == 1 else None)
    if not isinstance(owner, str) or owner not in connected:
        raise ValueError("Choose a connected source folder for the HTML attachment")
    return owner


def _source_entries(root, project, unavailable_sources=frozenset()):
    """Expose a source folder once, with paths relative to that root."""
    from .source_owner import is_owner
    if not is_owner(project):
        return []
    from .source_inventory import project_authored_source_entries

    base = root / project
    files = []
    for path in sorted({*(entry["path"] for entry in project_authored_source_entries(root, base)),
                        *_html_source_paths(root, base)}):
        if path in unavailable_sources:
            continue
        source = root / path
        files.append({"id": path, "name": source.name,
                      "relative_path": source.relative_to(base).as_posix(),
                      "size_bytes": source.stat().st_size})
    return [{"id": project, "name": project, "kind": "local", "files": files,
             "size_bytes": sum(f["size_bytes"] for f in files)}]


def _default_folder_files(root, owner, unavailable_sources=frozenset()):
    """Authored files of a Project's default folder, without reading its configuration from disk.

    The synchronized configuration object, not the worktree copy, defines the Project.
    """
    from .project_documents import MARKDOWN_ASSET_SUFFIXES, latex_folder_files, markdown_input_files
    from .source_inventory import AUTHORED_SOURCE_EXTENSIONS
    base = root / owner
    if not base.is_dir() or base.is_symlink():
        return []
    # Hidden subfolders hold no documents of the default folder.
    candidates = {path for path in markdown_input_files(base)
                  if path.suffix.lower() not in MARKDOWN_ASSET_SUFFIXES
                  and not any(part.startswith(".") for part in path.relative_to(base).parts[:-1])}
    candidates.update(latex_folder_files(base, AUTHORED_SOURCE_EXTENSIONS))
    if (base / ".ThoughtIgnore").is_file():
        candidates.add(base / ".ThoughtIgnore")
    files = []
    for source in sorted(candidates):
        path = source.relative_to(root).as_posix()
        if path in unavailable_sources or not source.is_file() or source.is_symlink():
            continue
        files.append({"id": path, "name": source.name, "relative_path": source.relative_to(base).as_posix(),
                      "size_bytes": source.stat().st_size})
    return files


def _html_source_paths(root, base):
    """HTML in the source tree is listed with the sources (37add50).

    The authored-source inventory covers editable text only; HTML syncs as a
    build input, under the same ThoughtIgnore and folder rules.
    """
    from .source_inventory import BUILD_INPUT_EXCLUDED_PARTS, real_project_path
    from .source_resources import is_html_source_path
    from .thought_ignore import ThoughtIgnore
    ignored = ThoughtIgnore(base)
    paths = []
    for tree in (base / "tex", base / "markdown"):
        if not tree.is_dir() or not real_project_path(base, tree):
            continue
        for directory, subdirectories, filenames in os.walk(tree, followlinks=False):
            parent = Path(directory)
            # Prune before descending: the catalogue lists sources on every read.
            subdirectories[:] = [name for name in subdirectories
                                 if name not in BUILD_INPUT_EXCLUDED_PARTS and not name.startswith(".")
                                 and not ignored.excludes((parent / name).relative_to(base).as_posix(), is_dir=True)]
            for name in filenames:
                path = parent / name
                relative = path.relative_to(root).as_posix()
                if (is_html_source_path(relative, base.name) and not path.is_symlink() and path.is_file()
                        and not ignored.excludes(path.relative_to(base).as_posix())):
                    paths.append(relative)
    return paths


def _missing_segment_sources(unavailable_sources: set[str], segment: dict) -> list[str]:
    paths = [segment.get("primary_source_file") or segment.get("source_id")]
    paths.extend(item.get("source_file") for item in segment.get("inputs", []) if isinstance(item, dict))
    missing = []
    for source in dict.fromkeys(path for path in paths if isinstance(path, str) and path):
        relative = Path(source)
        if relative.is_absolute() or ".." in relative.parts or source.startswith("third_party/"):
            continue
        if source in unavailable_sources:
            missing.append(source)
    return missing


def _publication_segments(pub, store, session=None, renamed=None):
    """Read document boundaries from the artifact bound to this publication.

    Publication metadata contains a current artifact reference, not its PDF segments.
    Without that artifact the manifest's documents remain pending; obsolete inline
    segment formats are not read.
    """
    from .default_folder_segment import alias, alias_active, rewrite_value
    from .source_folder_names import pairs, rewrite_value as rename_folders, same_location_in
    if renamed is None:
        renamed = pairs(session)
    metadata = pub.metadata_json or {}
    artifact = pub.source_artifact_sha256 or metadata.get("source_artifact_sha256")
    if not artifact:
        return []
    try:
        path = store.path(artifact)
        stat = path.stat()
        file_identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
        cached = _SOURCE_ARTIFACT_FACTS.get(artifact)
        if cached is not None and cached[0] == file_identity:
            facts = cached[1]
        else:
            # Artifacts are immutable and content-addressed: verify and parse each
            # file once per process instead of on every catalogue read.
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != artifact:
                raise ValueError("source artifact failed identity verification")
            manifest = json.loads(raw)
            pdf = manifest.get("pdf", {})
            facts = (manifest.get("kind"), manifest.get("filename"), pdf.get("filename"),
                     pdf.get("project"), pdf.get("pdf_sha256"), pdf.get("segments"))
            if len(_SOURCE_ARTIFACT_FACTS) >= 4096:
                _SOURCE_ARTIFACT_FACTS.clear()
            _SOURCE_ARTIFACT_FACTS[artifact] = (file_identity, facts)
        kind, filename, pdf_filename, project, pdf_sha256, segments = facts
        if (kind != "thoughts-document-source-artifact"
                or filename != pub.filename
                or pdf_filename != pub.filename
                or not same_location_in(renamed, project, pub.project)
                or pdf_sha256 != pub.object_sha256):
            raise ValueError("source artifact does not match the published PDF")
        if not isinstance(segments, list) or not segments or any(not isinstance(s, dict) for s in segments):
            raise ValueError("source artifact has no valid document list")
        # An artifact built before the default-folder or source-folder migration names
        # the old folder until the forced rebuild replaces it.
        result = rewrite_value(segments, alias) if alias_active() else copy.deepcopy(segments)
        return rename_folders(result, renamed) if renamed else result
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError(f"Cannot load Library documents for {pub.filename}: {error}") from error


def _composition_inputs(documents: list[dict], source_owners: set[str]) -> list[list]:
    return [[doc["id"], doc["name"], doc.get("sha256", ""),
             doc.get("start_page", 1) if doc.get("source_project") in source_owners else 1,
             doc.get("page_count", 0) if doc.get("source_project") in source_owners else 0]
            for doc in documents]


def _composition_signature(inputs: list[list]) -> str:
    return hashlib.sha256(json.dumps([1, inputs], ensure_ascii=False).encode()).hexdigest()


def _direct_project_pdf(documents, publications, store):
    """Reuse a publication only when it already contains exactly the requested order."""
    filenames = {d["filename"] for d in documents}
    if len(filenames) != 1:
        return ""
    filename = next(iter(filenames))
    pub = publications.get(filename)
    if pub is None or any(d.get("pending_build") for d in documents):
        return ""
    if not pub.project:
        return filename if len(documents) == 1 else ""
    segments = _publication_segments(pub, store)
    expected = [(s.get("primary_source_file") or s.get("source_id"), s.get("start_page", 0), s.get("page_count", 0))
                for s in segments]
    actual = [(d.get("source_file"), d.get("start_page", 0), d.get("page_count", 0)) for d in documents]
    return filename if actual == expected else ""


def catalogue(session, root: Path, store, *, sources: bool = True, listing: set[str] | None = None):
    """Render current manifests; publications supply bytes, never membership.

    Reading the catalogue must not create Projects, manifests, or input files.
    sources=False leaves out the source-folder file listings, which read every
    authored file of every Project from disk; documents do not depend on them.

    ``listing`` (PDF filenames) leaves out the Projects that cannot list any of
    them among their documents, so the caller composes only the Projects a build
    touched. A Project's documents are its declarations, each named by the
    publication of its source folder or else by its declared filename; a Project
    is kept when a named publication belongs to one of its folders or a declared
    filename is named. That is never fewer Projects than those whose documents
    name one of the filenames, and each kept entry is computed as in the full
    catalogue.
    """
    from . import project_configuration as pc
    from .library_project_manifests import source_manifests

    configurations = pc.canonical_projects(session, store)
    unavailable = set(session.scalars(select(SourceFile.path).where(SourceFile.deleted.is_(True))))
    saved = {e.entity_id: e for e in session.scalars(select(Entity).where(Entity.entity_type == TYPE))}
    retired = retired_filenames(session)
    publications = {p.filename: p for p in session.scalars(select(Publication)) if p.filename not in retired}
    owner_to_project = {}
    for configuration in configurations:
        for folder in configuration["source_folders"]:
            # The default folder's own documents publish under its full path.
            owner_to_project[folder["path"]] = configuration["id"]

    for configuration in configurations:
        record = saved.get(configuration["id"])
        if record is not None and record.deleted:
            raise ValueError("Deleted Project still has an active manifest: " + configuration["id"])
    if listing is not None:
        kept = {owner_to_project.get(pub.project) for filename, pub in publications.items() if filename in listing}
        kept.update(
            configuration["id"] for configuration in configurations
            if any(pc.declared_filename(configuration, declaration) in listing
                   for declaration in configuration["documents"])
        )
        configurations = [configuration for configuration in configurations if configuration["id"] in kept]

    components = {c["id"]: [] for c in configurations}
    from .source_folder_names import pairs
    # Artifacts built before the source-folder name migration name the old folders.
    renamed = pairs(session)
    for pub in publications.values():
        key = owner_to_project.get(pub.project)
        if key is None or key not in components:
            continue
        for segment in _publication_segments(pub, store, renamed=renamed):
            source = segment.get("primary_source_file") or segment.get("source_id") or ""
            component = {"id": segment.get("source_id") or source,
                "document_id": pub.document_id,
                "name": segment.get("title") or Path(source).stem.replace("_", " "),
                "filename": pub.filename, "sha256": pub.object_sha256,
                "start_page": segment.get("start_page", 0), "page_count": segment.get("page_count", 0),
                "source_file": source, "source_project": pub.project,
                "size_bytes": pub.size_bytes}
            missing = _missing_segment_sources(unavailable, segment)
            if missing:
                component["missing_sources"] = missing
            components[key].append(component)

    projects = []
    for configuration in configurations:
        key = configuration["id"]
        record = saved.get(key)
        if record is not None and record.deleted:
            raise ValueError("Deleted Project still has an active manifest: " + key)
        data = record.payload if record is not None else {}
        owners = [f["path"] for f in configuration["source_folders"]
                  if f["folder_id"] != configuration["default_folder_id"]]
        labels = {f["project"]: f for f in data.get("source_folders", [])}
        folders = [{**labels.get(owner, {}), "project": owner,
                    "folder": labels.get(owner, {}).get("folder", owner)} for owner in owners]
        entry = {"id": key, "name": configuration["name"], "revision": record.revision if record else 0,
                 "source_folders": folders, "target": "",
                 "documents": components[key],
                 "sources": [{**location, "folder_id": folder["folder_id"],
                              "name": labels.get(folder["path"], {}).get("folder", folder["path"])}
                             for folder in configuration["source_folders"]
                             if folder["folder_id"] != configuration["default_folder_id"]
                             for location in (_source_entries(root, folder["path"], unavailable)
                                              if sources else [])]}
        # Enrich only explicitly declared PDFs, checking permanent identity. An
        # unattached publication cannot create a Project. A document has one
        # current PDF: its publication, even if the declaration pinned an older
        # copy's bytes, so a re-saved PDF cannot drop out of the Project.
        for declaration in configuration["documents"]:
            if declaration["format"] != "pdf":
                continue
            filename = declaration["source"]["path"]
            pub = publications.get(filename)
            if pub is not None and pub.document_id == declaration["document_id"]:
                component = next((d for d in entry["documents"]
                    if d.get("source_file") in {"third_party/" + filename, pc.resolve(configuration, declaration)}
                    and d.get("document_id") in {None, declaration["document_id"]}), None)
                if component is not None:
                    component["document_id"] = declaration["document_id"]
                    continue
                entry["documents"].append({"id": next((d["id"] for d in data.get("imports", [])
                    if d.get("document_id") == declaration["document_id"] or d.get("filename") == filename),
                    "third_party/" + filename if owners else filename),
                    "document_id": declaration["document_id"], "filename": filename,
                    "sha256": pub.object_sha256, "start_page": 1, "size_bytes": pub.size_bytes,
                    "page_count": (pub.metadata_json or {}).get("page_count", 0),
                    "name": next((d.get("name", Path(filename).stem) for d in data.get("imports", [])
                                  if d.get("filename") == filename), Path(filename).stem)})
        pc.catalogue_entry(entry, configuration)
        default = pc.configured_default_folder(configuration)
        listed = next(source for source in entry["sources"] if source["id"] == default)
        known = {file["id"] for file in listed["files"]}
        # The default folder lists its own authored documents beside the configuration and PDFs.
        if sources:
            listed["files"].extend(file for file in _default_folder_files(root, default, unavailable)
                                   if file["id"] not in known)
        for document in entry["documents"]:
            owner = document.get("source_project")
            if owner == default:
                relative = document.get("source_file", "").removeprefix(owner + "/")
                document.update(folder=owner, relative_path=relative, group=str(Path(relative).parent))
            elif owner in owners:
                source = document.get("source_file", "")
                prefix = owner + ("/markdown/source/" if source.startswith(owner + "/markdown/source/") else "/tex/source/")
                relative = source.removeprefix(prefix) if source.startswith(prefix) else source.removeprefix(owner + "/")
                document.update(folder=labels.get(owner, {}).get("folder", owner),
                                relative_path=relative, group=str(Path(relative).parent))
            if document.get("source_file") in unavailable:
                missing = document.setdefault("missing_sources", [])
                if document["source_file"] not in missing:
                    missing.append(document["source_file"])
        from .models import ReadingPublication
        reading = session.get(ReadingPublication, key)
        if reading is not None:
            entry["target"] = reading.filename if reading.page_count else ""
            entry["reading_revision"] = reading.revision
            entry["page_count"] = reading.page_count
            entry["pdf_size_bytes"] = reading.pdf_size_bytes
            entry["published_at"] = reading.published_at.isoformat()
        else:
            entry["pdf_size_bytes"] = sum({d.get("sha256"): d.get("size_bytes", 0)
                                          for d in entry["documents"] if d.get("size_bytes", -1) >= 0}.values())
        # Page offsets are display metadata; annotations retain document-local pages.
        first = 1
        for document in entry["documents"]:
            document["project_start_page"] = first
            document["start_page"] = 1
            first += document.get("page_count", 0)
        entry["source_manifests"] = source_manifests(session, store, entry)
        entry["removed_identities"] = _removed_identities(session, configuration, data)
        # Every Project has a kind for its review cards (protocol/review-card-kinds.md 2.3).
        entry["review_kind"] = data.get("review_kind") if data.get("review_kind") in ("knowledge", "project") \
            else "knowledge"
        projects.append(entry)
    from .source_folder_names import renames
    # Map M of the source-folder naming migration ([{folder_id, old, new}], empty before
    # it): clients rename their local folders and state from it (source_folder_names).
    return {"schema_version": 1, "projects": sorted(projects, key=lambda p: p["name"].casefold()),
            "source_folder_renames": renames(session)}


def _removed_identities(session, configuration, data) -> dict:
    """Source alias -> permanent identity of documents removed from this Project.

    "Remove document" keeps the source file and its identity (only the identity's
    source path is cleared), so a client declaring the source again must reuse
    the identity: the PDF name stays registered to it. Removals recorded before
    identities were kept are recovered from the PDF name the source derives.
    """
    from .models import DocumentIdentity
    from .project_documents import document_output
    from .source_owner import split

    declared = {d["document_id"] for d in configuration["documents"]}
    result = {}
    for alias, document_id in data.get("removed_identities", {}).items():
        identity = session.get(DocumentIdentity, document_id)
        if document_id not in declared and identity is not None and not identity.source_path:
            result[alias] = document_id
    for alias in data.get("removed_documents", []):
        located = split(alias) if isinstance(alias, str) else None
        if alias in result or located is None:
            continue
        owner, relative = located
        suffix = PurePosixPath(relative).suffix.lower()
        kind = "latex" if suffix == ".tex" else "markdown" if suffix in {".md", ".markdown"} else None
        if kind is None:
            continue
        filenames = {document_output(owner, {"source": relative, "format": kind})}
        if kind == "markdown":
            # The desktop names Markdown PDFs by their path below markdown/source.
            name = (relative.removeprefix("markdown/source/") if relative.startswith("markdown/source/")
                    else "folder:" + relative)
            filenames.add("document_" + hashlib.sha256(name.encode()).hexdigest()[:20] + ".pdf")
        for identity in session.scalars(select(DocumentIdentity).where(DocumentIdentity.filename.in_(sorted(filenames)))):
            if identity.document_id not in declared and not identity.source_path:
                result[alias] = identity.document_id
                break
    return result


def _pdf_page_count(store, sha256, size_bytes):
    """Count pages of a stored PDF; a corrupt or unreadable PDF is a rejected input."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError
    if not store.matches(sha256, size_bytes):
        raise ValueError("Imported PDF failed verification")
    try:
        with PdfReader(store.path(sha256)) as pdf:
            count = len(pdf.pages)
    except (PyPdfError, OSError, RecursionError) as error:
        raise ValueError("Imported PDF is invalid: " + str(error)) from error
    if count < 1:
        raise ValueError("Imported PDF has no pages")
    return count


def publish_project_reading(session, store, root: Path, record, *, project=None):
    from .models import ReadingPublication
    from .project_reading import ProjectReading, ReadingDocument, publish_reading
    project = project or next((p for p in catalogue(session, root, store)["projects"] if p["id"] == record.entity_id), None)
    if record.deleted:
        previous = session.get(ReadingPublication, record.entity_id)
        if previous is not None:
            session.delete(previous)
            return True
        return False
    if project is None:
        return False
    documents = []
    for selected in project["documents"]:
        # One unbuilt or inconsistent document must not withhold the whole Project:
        # read the ready documents; the Library still lists the others.
        if selected.get("pending_build") or selected.get("missing_sources"):
            continue
        publication = session.get(Publication, selected["filename"])
        if (publication is None or publication.object_sha256 != selected.get("sha256")
                or publication.document_id != selected.get("document_id")):
            continue
        metadata = dict(publication.metadata_json or {})
        artifact = publication.source_artifact_sha256
        if artifact:
            package = json.loads(store.path(artifact).read_bytes())
            count = package["pdf"]["page_count"]
            if package["pdf"]["pdf_sha256"] != publication.object_sha256:
                raise ValueError("Document source package does not match its PDF")
        else:
            count = metadata.get("page_count")
            if not isinstance(count, int) or count < 1:
                count = _pdf_page_count(store, publication.object_sha256, publication.size_bytes)
                metadata["page_count"] = count
                publication.metadata_json = metadata
            package = {"schema_version": 1, "kind": "thoughts-document-reading-package",
                "document_id": publication.document_id, "filename": publication.filename,
                "pdf_sha256": publication.object_sha256,
                "page_count": count, "annotation_frontier": publication.annotation_frontier,
                "placement_manifest_sha256": publication.placement_manifest_sha256}
            artifact = store.put_bytes(session, (json.dumps(package, sort_keys=True) + "\n").encode(),
                "application/vnd.thoughts.document-reading-package+json").sha256
        documents.append(ReadingDocument(publication.document_id, selected["name"], publication.object_sha256,
                                         publication.size_bytes, count, artifact))
    if not documents:
        return False
    reading = ProjectReading(project["id"], project["name"], tuple(documents))
    previous = session.get(ReadingPublication, reading.project_id)
    revision = previous.revision if previous else None
    return publish_reading(session, store, reading).revision != revision


def _assembly_record(session, project):
    record = _record(session, TYPE, project["id"])
    if record is None and project["documents"] and not project.get("target"):
        record = Entity(entity_type=TYPE, collection="library", entity_id=project["id"],
                        revision=0, deleted=False, winner_operation_id=SYSTEM_DEVICE,
                        payload={"name": project["name"], "imports": [], "order": []})
        session.add(record)
        session.flush()
    return record


def publish_project_readings(session, store, root: Path, *, changed_filenames=None):
    """Publish document compositions in the same transaction as their document versions."""
    changed = 0
    # Readings are composed from documents only; skip the source-folder scan. A build
    # names the PDFs it published: only the Projects that can list one are composed
    # (catalogue), not every Project of the Library after every build.
    for project in catalogue(session, root, store, sources=False, listing=changed_filenames)["projects"]:
        if changed_filenames is not None and not changed_filenames.intersection(d["filename"] for d in project["documents"]):
            continue
        record = _assembly_record(session, project)
        if record is None:
            continue
        changed += publish_project_reading(session, store, root, record, project=project)
    return changed


def publish_configured_project_readings(session, store, root: Path, paths) -> int:
    """Republish the readings of Projects whose Project file was published.

    Builds and Library operations republish readings, but a Project-file
    publication such as excluding a document is neither and rebuilds no PDF.
    The reading is derived, so a failure here leaves it for the next build
    rather than failing the publication.
    """
    from .project_configuration import is_manifest_path, manifest_paths
    paths = {path for path in paths if is_manifest_path(path)}
    if not paths:
        return 0
    changed = 0
    try:
        for project in catalogue(session, root, store)["projects"]:
            if paths.isdisjoint(manifest_paths(project["id"])):
                continue
            record = _assembly_record(session, project)
            if record is not None:
                changed += publish_project_reading(session, store, root, record, project=project)
        # Declaring a removed document again restores its publication. Clients
        # need the stored identity of the changed source index, and the renderer
        # reads the retired set from its mirror file, or it leaves the restored
        # PDF out of the source index and every build packaging it fails.
        from .library import store_published_source_index, write_library_index
        store_published_source_index(session, store)
        write_library_index(session, root)
        session.commit()
    except (ValueError, OSError):
        session.rollback()
        logging.getLogger(__name__).exception("Could not republish Project readings after a Project file change")
        return 0
    return changed


def reconcile_detached_publications(session, store, root: Path):
    """Retire stray publications of sources detached before this was recorded.

    Detaching a source used to keep its publication live, so a workspace can hold
    publications no project lists. They are indistinguishable from real documents
    to the renderer, which keeps publishing them. This restores the retirement
    marker and leaves the source files and review history untouched.
    """
    retired = 0
    for record in list(session.scalars(select(Entity).where(Entity.entity_type == TYPE, Entity.deleted.is_(False)))):
        owners = record.payload.get("detached_source_projects") or []
        if not owners:
            continue
        filenames = set()
        for owner in owners:
            filenames |= _detached_publications(session, root, store, record.entity_id, owner)
        if not filenames:
            continue
        recorded = set(record.payload.get("retired_filenames", []))
        missing = filenames - recorded
        if not missing:
            continue
        data = copy.deepcopy(record.payload)
        data["retired_filenames"] = sorted(recorded | filenames)
        record.payload = data
        try:
            from .document_deletion import retire_document_items
            retire_document_items(session, store, operation_id=str(uuid.uuid4()), filenames=missing)
            session.commit()
        except (ValueError, RuntimeError):
            session.rollback()
            logging.getLogger(__name__).exception(
                "Could not retire detached publications for Library project %s", record.entity_id)
            continue
        retired += len(missing)
    return retired


def repair_project_readings(session, store, root: Path):
    """Verify pinned attachments and restore document-based reading metadata."""
    changed = 0
    from .document_moves import finish_pending_moves
    finish_pending_moves(session, store, root)
    for project in catalogue(session, root, store)["projects"]:
        record = _assembly_record(session, project)
        if record is None:
            continue
        try:
            changed += verify_project_attachments(session, store, root, record)
            changed += publish_project_reading(session, store, root, record, project=project)
            session.commit()
        except ValueError:
            # A damaged legacy import must not take down the entire service.
            session.rollback()
            logging.getLogger(__name__).exception("Could not prepare PDF for Library project %s", record.entity_id)
    session.commit()
    try:
        reconcile_detached_publications(session, store, root)
    except Exception:
        # A stray publication must not take down the entire service.
        session.rollback()
        logging.getLogger(__name__).exception("Could not reconcile detached publications")
    from .library import store_published_source_index, write_library_index
    # Changing the retired set changes the published index bytes, and a stored
    # object must exist for clients to receive a source-index identity.
    store_published_source_index(session, store)
    write_library_index(session, root)
    return changed


def verify_project_attachments(session, store, root, record):
    """Validate saved references without requiring or populating a local PDF folder."""
    from .models import ObjectRecord
    from .project_attachments import verified_attachment_object

    if record.deleted:
        return 0
    from .project_configuration import canonical_for_project
    configuration = canonical_for_project(session, store, record.entity_id)
    if configuration is None:
        return 0
    for document in configuration["documents"]:
        if document["format"] != "pdf":
            continue
        filename = document["source"]["path"]
        digest = configuration["rendering"][document["document_id"]]["sha256"]
        obj = session.get(ObjectRecord, digest)
        if obj is None:
            raise ValueError("Project attachment object is unavailable: " + filename)
        verified_attachment_object(store, filename, digest, obj.size_bytes)
    return 0


def _save_source(session, store, root, operation_id, path, content, *, materialize=True):
    """Publish composition metadata through the normal source revision log."""
    from .operations import latest_device_sequence
    from .schemas import SourceApplyRequest
    from .sources import _apply_source, _materialize_canonical_path
    if session.get(Device, SYSTEM_DEVICE) is None:
        session.add(Device(id=SYSTEM_DEVICE, name="Library project changes", enabled=False,
                           token_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest()))
        session.flush()
    original_path = path
    from . import project_manifest
    if session.info.get("legacy_project_manifest_migration"):
        from . import legacy_project_manifest_migration as project_manifest
    owner, _, relative = path.partition("/")
    if relative in (project_manifest.ORDER, project_manifest.OPTIONS, project_manifest.ATTACHMENTS):
        manifest = project_manifest.load_canonical(session, store, root, owner)
        update = json.loads(content)
        if manifest["schema_version"] == 4 and relative == project_manifest.ATTACHMENTS:
            from .document_identity import registered_document_id
            from .uniform_manifest import managed_source
            from .project_attachments import materialize_project_attachment
            from .project_configuration import configured_default_folder, find_canonical
            from .models import ObjectRecord
            owning = find_canonical(session, store, owner)
            if owning is None:
                raise ValueError("Missing current Project manifest for " + owner)
            project_key, project_folder = owning[1]["id"], configured_default_folder(owning[1])
            for attachment in update["attachments"]:
                attachment["document_id"] = registered_document_id(session, attachment["filename"])
                attachment["source"] = managed_source(project_key, attachment["filename"], project_folder)
                obj = session.get(ObjectRecord, attachment["sha256"])
                if obj is None:
                    raise ValueError("Project PDF object is unavailable")
                materialize_project_attachment(root, store, attachment["filename"], attachment["sha256"], obj.size_bytes,
                    destination=(root / owner / attachment["source"]).resolve())
        manifest = project_manifest.apply_legacy(manifest, relative, update)
        project_manifest.validate(manifest, owner, internal=True)
        path = owner + "/" + project_manifest.MANIFEST
        content = (json.dumps(project_manifest.for_storage(manifest), ensure_ascii=False, indent=2) + "\n").encode()
    if path.endswith("/thoughts-project.json"):
        from .project_configuration import find_canonical, is_manifest_path, replace_projection
        configuration = None if is_manifest_path(path) else find_canonical(session, store, owner)
        if configuration is not None:
            path, value = configuration
            registered = replace_projection(value, owner, json.loads(content))
        elif is_manifest_path(path):
            registered = json.loads(content)
        else:
            if not session.info.get("legacy_project_manifest_migration"):
                raise ValueError("Missing current Project manifest for " + owner)
            from .document_identity import prepare_source_registration
            registered = prepare_source_registration(session, owner, json.loads(content))
        content = (json.dumps(registered, ensure_ascii=False, indent=2) + "\n").encode()
    previous = session.get(SourceFile, path)
    empty = store.put_bytes(session, b"", "text/plain")
    incoming = store.put_bytes(session, content, "text/plain")
    result = _apply_source(session, store, root, SYSTEM_DEVICE, SourceApplyRequest(
        operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, operation_id + ":" + original_path)),
        device_sequence=latest_device_sequence(session, SYSTEM_DEVICE) + 1,
        path=path, base_revision=previous.revision if previous else 0,
        base_sha256=previous.sha256 if previous and not previous.deleted else empty.sha256,
        base_object_sha256=previous.object_sha256 if previous and not previous.deleted else empty.sha256,
        content_sha256=incoming.sha256, client_timestamp=utc_now().isoformat()), already_locked=True)
    if result.outcome not in {"applied", "merged", "duplicate"}:
        raise ValueError("The project composition changed on another device; Sync and try again")
    if materialize:
        _materialize_canonical_path(session, store, root, path, already_locked=True)


def _exclusive_filenames(session, root, store, key, filenames):
    """Keep a shared PDF and its review data while another project still uses it."""
    other_files = set()
    for project in catalogue(session, root, store)["projects"]:
        if project["id"] == key:
            continue
        for document in project["documents"]:
            other_files.add(document["filename"])
            if document.get("attachment_filename"):
                other_files.add(document["attachment_filename"])
    return filenames - other_files


def _detached_publications(session, root, store, key, owner):
    """Publications that lose their owner when a source folder is detached.

    A detached source keeps its files and review history, but the Library stops
    listing its publication. Leaving the publication live and unowned is what
    makes it a stray entry that clients still receive, so its filenames must be
    retired unless another project shares them.
    """
    candidates = {p.filename for p in session.scalars(select(Publication).where(Publication.project == owner))}
    if not candidates:
        return set()
    # Another project may still attach the same folder and list these PDFs, in
    # which case nothing is retired.
    return candidates if _exclusive_filenames(session, root, store, key, candidates) else set()


def mutate(session, store, root: Path, request: dict, *, settings=None, trace_owner_device_id=None):
    """Restore materialized Project metadata if its transaction is rolled back."""
    from .project_configuration import current_manifest_path, read, save
    if request.get("action") == "plan_move_source_document":
        # Read-only: what moving a source document would change, for review.
        from .source_document_moves import plan
        try:
            return {"plan": plan(session, store, root, request)}
        finally:
            session.rollback()
    previous = {}
    for key in {request.get("project_id"), request.get("destination_project_id")} - {None}:
        if isinstance(key, str):
            path = root / current_manifest_path(session, key)
            previous[path] = read(path) if path.exists() else None
    try:
        return _mutate_with_import_cleanup(session, store, root, request, settings=settings,
                                           trace_owner_device_id=trace_owner_device_id)
    except Exception:
        session.rollback()
        for path, value in previous.items():
            if value is None:
                path.unlink(missing_ok=True)
            else:
                save(root, value)
        raise


def _mutate_with_import_cleanup(session, store, root: Path, request: dict, *, settings=None, trace_owner_device_id=None):
    """Roll back newly materialized import files if the enclosing change fails."""
    if request.get("action") != "import_folder":
        return _mutate(session, store, root, request, settings=settings, trace_owner_device_id=trace_owner_device_id)
    from .folder_import import requested_destination
    operation_id = str(uuid.UUID(request.get("operation_id", "")))
    # The folder this operation creates: its proposed Rule-N name, or the legacy
    # docs-import-<hex> of an old client. Never guessed from a name prefix.
    owner = requested_destination(request)
    if owner is None:
        return _mutate(session, store, root, request, settings=settings, trace_owner_device_id=trace_owner_device_id)
    destination = root / owner
    fresh = not destination.exists() and not destination.is_symlink()
    try:
        return _mutate(session, store, root, request, settings=settings, trace_owner_device_id=trace_owner_device_id)
    except Exception:
        session.rollback()
        # A failure after commit must not remove an acknowledged import.
        if fresh and _record(session, RECEIPT, operation_id) is None:
            import shutil
            if destination.is_dir() and not destination.is_symlink():
                shutil.rmtree(destination)
            for target in _import_outputs(store, owner, request.get("sha256")):
                if session.get(Publication, target) is None:
                    (root / "current_pdfs" / target).unlink(missing_ok=True)
        raise


def _import_outputs(store, owner, sha256):
    """Document PDFs a failed import may have copied into current_pdfs."""
    from .folder_import import document_outputs, read_bundle
    try:
        documents, _ = read_bundle(store.path(sha256).read_bytes())
    except (OSError, TypeError, ValueError):
        return []
    return list(document_outputs(owner, documents).values())


def _default_folder(key: str) -> str:
    from .project_configuration import default_folder
    return default_folder(key)


def _default_folders(session, store, key: str) -> list[str]:
    """The Project's declared default folder, else the one this server creates."""
    from .project_configuration import canonical_for_project, configured_default_folder
    configuration = canonical_for_project(session, store, key)
    return [configured_default_folder(configuration) if configuration is not None else _default_folder(key)]


def standalone_latex(text: str) -> bool:
    """A complete LaTeX document: a class and a document body outside comments."""
    active = re.sub(r"(?<!\\)%.*", "", text)
    return bool(re.search(r"(?m)^\s*\\documentclass\b", active)
                and re.search(r"\\begin\s*\{document\}", active))


def _include_default_folder_source(session, store, root, operation_id, key, request, data):
    """Declare a Markdown or standalone LaTeX file of the Project's own default folder."""
    from . import project_configuration as pc
    from .models import new_document_id
    from .source_owner import default_folder_output
    from .thought_ignore import ThoughtIgnore
    owner = pc.default_folder(key)
    source_path = request["source_file"]
    relative = source_path.removeprefix(owner + "/")
    candidate = PurePosixPath(relative)
    path = root / owner / relative
    suffix = candidate.suffix.lower()
    if (candidate.is_absolute() or not candidate.parts or candidate.as_posix() != relative
            or any(part in {"", ".", ".."} or part.startswith(".") for part in candidate.parts)
            or suffix not in {".tex", ".md", ".markdown"}
            or not path.is_file() or path.is_symlink()
            or not path.resolve().is_relative_to((root / owner).resolve())):
        raise ValueError("Choose a Markdown or LaTeX document in this project's folder")
    if ThoughtIgnore(root / owner).excludes(relative):
        raise ValueError("This document is excluded by the folder's .ThoughtIgnore")
    kind = "latex" if suffix == ".tex" else "markdown"
    if kind == "latex" and not standalone_latex(path.read_text(encoding="utf-8")):
        raise ValueError("Choose a complete LaTeX document with a document class")
    configuration = pc.canonical_for_project(session, store, key)
    if configuration is None:
        raise ValueError("Missing current Project manifest: " + key)
    folder_id = configuration["default_folder_id"]
    if any(d["source"] == {"folder_id": folder_id, "path": relative} for d in configuration["documents"]):
        return
    document_id = request.get("new_document_id") or new_document_id()
    configuration["documents"].append({"document_id": document_id,
        "source": {"folder_id": folder_id, "path": relative}, "format": kind, "included": True})
    output = default_folder_output(owner, relative)
    configuration.setdefault("rendering", {})[document_id] = {"output": output}
    _save_source(session, store, root, operation_id, pc.manifest_path(key),
                 (json.dumps(pc.validate(configuration), ensure_ascii=False, indent=2) + "\n").encode())
    data.setdefault("included_sources", []).append({
        "id": source_path, "name": candidate.stem.replace("_", " "), "source_file": source_path,
        "filename": output, "sha256": "", "size_bytes": -1, "start_page": 0})


def _mutate(session, store, root: Path, request: dict, *, settings=None, trace_owner_device_id=None):
    """Caller holds the worktree lock. Receipt and project change commit together."""
    operation_id = str(uuid.UUID(request.get("operation_id", "")))
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    receipt = _record(session, RECEIPT, operation_id)
    if receipt:
        from .default_folder_segment import alias_active, legacy_value
        from .source_folder_names import legacy_spellings
        # A retry rewritten by the default-folder or source-folder alias hashes like its original.
        candidates = legacy_spellings(session, request)
        if alias_active():
            candidates += [legacy_value(value) for value in list(candidates)]
        digests = {hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                   for value in candidates}
        if receipt.payload["sha256"] not in digests:
            raise ValueError("Project operation ID was reused with different content")
        if receipt.payload.get("move_pending"):
            from .document_moves import finish_move
            finish_move(session, store, root, receipt)
        return catalogue(session, root, store)
    from .document_moves import finish_pending_moves
    finish_pending_moves(session, store, root)
    key = request.get("project_id", "")
    if not isinstance(key, str) or not key or len(key) > 512:
        raise ValueError("Invalid project identity")
    action = request.get("action")
    current = next((p for p in catalogue(session, root, store)["projects"] if p["id"] == key), None)
    record = _record(session, TYPE, key)
    # A new, empty connected folder (new_source_folders.py): (folder, label), declared below.
    new_folder = None
    if action == "create":
        if current or record:
            raise ValueError("This project already exists")
        name = _title(request.get("name"))
        if any(p["name"].casefold() == name.casefold() for p in catalogue(session, root, store)["projects"]):
            raise ValueError("A project with this name already exists")
        data = {"name": name, "imports": [], "order": []}
        current = {"documents": [], "target": ""}
        if "source_folder" in request:
            from .new_source_folders import folder_label, folder_name
            label = folder_label(request["source_folder"], name, [])
            new_folder = (folder_name(session, store, root, label, operation_id), label)
            data["source_folders"] = [{"project": new_folder[0], "folder": label}]
    else:
        if not current:
            raise ValueError("This project is no longer in the Library")
        data = copy.deepcopy(record.payload) if record else {"name": current["name"], "imports": [], "order": []}
        # Reordering/removal must never silently overwrite another device's edit.
        if int(request.get("base_revision", 0)) != (record.revision if record else 0):
            raise ValueError("This project changed on another device. Sync and try again")
    if action == "move_document":
        from .document_moves import move_document
        return move_document(session, store, root, request, current, record, data, digest)
    if action == "move_source_document":
        from .source_document_moves import move_source_document
        return move_source_document(session, store, root, request, current, record, data, digest)
    if action == "move_source":
        from .source_folder_moves import move_source_folder
        return move_source_folder(session, store, root, request, current, record, data, digest)
    if action == "rename":
        data["name"] = _title(request.get("name"))
    elif action == "rename_document":
        selected = _selected_document(project_documents(current), request.get("document_id"))
        if not selected:
            raise ValueError("The document is no longer in this project")
        data.setdefault("document_names", {})[selected.get("document_id") or selected["id"]] = _title(request.get("name"))
    elif action == "import_folder":
        from .folder_import import import_folder
        request_name = _title(request.get("name"))
        # Folder moves and renames keep label-only entries without a sha256.
        if any(f.get("sha256") and f["sha256"] == request.get("sha256") for f in data.get("source_folders", [])):
            raise ValueError("This folder copy is already in the project")
        if settings is None:
            raise ValueError("Folder rendering is not configured")
        folder = import_folder(
            session, store, root, {**request, "name": request_name}, settings,
            trace_owner_device_id=trace_owner_device_id,
        )
        data.setdefault("document_names", {}).update(folder.pop("document_names", {}))
        data.setdefault("source_folders", []).append(folder)
    elif action == "attach_source_project":
        raise ValueError("Attach source folders through a current Project import or move; legacy publication attachment is unsupported")
    elif action == "include_source" and isinstance(request.get("source_file"), str) and any(
            request["source_file"].startswith(folder + "/") for folder in _default_folders(session, store, key)):
        _include_default_folder_source(session, store, root, operation_id, key, request, data)
    elif action == "include_source":
        source_path = request.get("source_file", "")
        path = (root / source_path).resolve()
        project = next((folder["path"] for folder in connected_source_folders(current)
                        if path.is_relative_to((root / folder["path"]).resolve())), None)
        if not project or path.suffix != ".tex" or not path.is_file():
            raise ValueError("Choose a source document in this project")
        text = path.read_text()
        if not (re.search(r"(?m)^\s*\\documentclass\b", text) and re.search(r"\\begin\s*\{document\}", text)):
            raise ValueError("Choose a complete LaTeX document with a document class")
        relative = path.relative_to(root / project).as_posix()
        # The entry point is the document itself, built from its own directory.
        document_path = project + "/" + relative
        from . import project_manifest
        manifest = project_manifest.load_canonical(session, store, root, project)
        if not any(d["id"] == document_path for d in manifest["documents"]):
            document = {"id": document_path, "document_id": request.get("new_document_id"), "source": relative,
                        "format": "latex", "included": True}
            if PurePosixPath(relative).parent.as_posix() not in {".", "tex/source"}:
                document["working_directory"] = PurePosixPath(relative).parent.as_posix()
            manifest["documents"].append(document)
        _save_source(session, store, root, operation_id, project + "/" + project_manifest.MANIFEST,
                     (json.dumps(project_manifest.for_storage(manifest), indent=2) + "\n").encode())
        data.setdefault("included_sources", []).append({"id": document_path, "name": path.stem.replace("_", " "),
            "source_file": source_path, "filename": current["target"], "sha256": "", "size_bytes": -1, "start_page": 0})
    elif action == "import_html":
        from .html_attachments import add
        owner = _html_import_owner(current, request.get("source_project"))
        add(session, store, root, operation_id, owner, request)
    elif action == "import":
        filename = request.get("filename", "")
        pub = session.get(Publication, filename)
        if not pub or pub.object_sha256 != request.get("sha256") or pub.project:
            raise ValueError("The imported PDF is not a current standalone Library PDF")
        # Reject a corrupt PDF before anything is materialized for it.
        _pdf_page_count(store, pub.object_sha256, pub.size_bytes)
        from .project_attachments import materialize_project_attachment
        from .project_configuration import canonical_for_project, configured_default_folder
        owning = canonical_for_project(session, store, key)
        managed = (configured_default_folder(owning) if owning is not None else _default_folder(key)) + "/" + filename
        materialize_project_attachment(root, store, filename, pub.object_sha256, pub.size_bytes, destination=root / managed)
        if not any(d["filename"] == filename for d in current["documents"]):
            data["imports"].append({"document_id": pub.document_id, "id": filename,
                "name": _title(request.get("name") or Path(filename).stem), "filename": filename,
                "sha256": pub.object_sha256, "size_bytes": pub.size_bytes, "start_page": 1, "imported": True,
                "managed_path": managed})
        # Importing a PDF this Project removed earlier lists it again, as moving it back does;
        # left retired, the Project would declare it while no device ever receives it.
        if filename in data.get("retired_filenames", []):
            data["retired_filenames"] = [f for f in data["retired_filenames"] if f != filename]
    elif action == "order":
        order = request.get("documents")
        expected = [d["id"] for d in current["documents"]]
        translated = [_selected_document(current["documents"], identifier) for identifier in order] if isinstance(order, list) else []
        order = [d["id"] for d in translated if d is not None]
        if len(order) != len(expected) or len(translated) != len(expected) or set(order) != set(expected):
            raise ValueError("The document list changed. Sync and reorder the current documents")
        data["order"] = order
    elif action == "remove_source":
        owner = request.get("source_project")
        folders = current.get("source_folders", [])
        if not isinstance(owner, str) or not any(f["path"] == owner for f in connected_source_folders(current)):
            raise ValueError("The source is no longer attached to this project")
        data["source_folders"] = [f for f in folders if f["project"] != owner]
        data["detached_source_projects"] = list(dict.fromkeys(data.get("detached_source_projects", []) + [owner]))
        prefix = owner + "/"
        data["order"] = [identity for identity in data.get("order", []) if not identity.startswith(prefix)]
        # Detach membership only: keep source files, publications and review history.
        # The publication itself must still be retired, or it stays live and unowned
        # and keeps being listed and shipped to clients.
        retired = _detached_publications(session, root, store, key, owner)
        if retired:
            data["retired_filenames"] = sorted(set(data.get("retired_filenames", [])) | retired)
            from .document_deletion import retire_document_items
            retire_document_items(session, store, operation_id=operation_id, filenames=retired)
    elif action == "rename_source":
        owner = request.get("source_project")
        if not isinstance(owner, str) or not any(f["path"] == owner for f in connected_source_folders(current)):
            raise ValueError("The source is no longer attached to this project")
        name = request.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise ValueError("Enter a source folder name between 1 and 200 characters without line breaks")
        name = name.strip()
        # Only the label changes: the folder, its path and the Project manifest stay as they are.
        others = {f["folder"].casefold() for f in current.get("source_folders", []) if f["project"] != owner}
        if name.casefold() in others | {"default source folder"}:
            raise ValueError("Another source folder in this project already has this name")
        folders = data.setdefault("source_folders", [])
        entry = next((f for f in folders if f.get("project") == owner), None)
        if entry is None:
            folders.append({"project": owner, "folder": name})
        else:
            entry["folder"] = name
    elif action == "add_source_folder":
        from .new_source_folders import folder_label, folder_name
        labels = [f.get("folder") for f in current.get("source_folders", [])]
        label = folder_label(request.get("name"), current["name"], labels)
        new_folder = (folder_name(session, store, root, label, operation_id), label)
        data.setdefault("source_folders", []).append({"project": new_folder[0], "folder": label})
    elif action == "set_review_kind":
        # protocol/review-card-kinds.md 2.3: the record only; no manifest or card changes.
        from .review_kinds import project_kind_error
        if (error := project_kind_error(request.get("review_kind"))) is not None:
            raise ValueError(error)
        data["review_kind"] = request["review_kind"]
    elif action == "set_document_published":
        selected = _selected_document(project_documents(current), request.get("document_id"))
        if not selected or not selected.get("document_id"):
            raise ValueError("The document is no longer in this project")
        if type(request.get("published")) is not bool:
            raise ValueError("Say whether the document is published")
    elif action == "remove_document":
        selected = _selected_document(project_documents(current), request.get("document_id"))
        if not selected:
            raise ValueError("The document is no longer in this project")
        project = selected.get("source_project")
        data.setdefault("removed_documents", []).append(selected["id"])
        if project and not selected.get("imported") and selected.get("document_id"):
            # Declaring the source again must reuse this identity: its PDF name and
            # review history stay registered to it.
            data.setdefault("removed_identities", {})[selected["id"]] = selected["document_id"]
        retired = _exclusive_filenames(session, root, store, key, {selected.get("attachment_filename", selected["filename"])}) if selected.get("imported") or not project else set()
        if selected.get("imported") or not project:
            data.setdefault("retired_filenames", []).extend(sorted(retired))
        data["imports"] = [d for d in data["imports"] if d["id"] != selected["id"]]
        if project and not selected.get("imported"):
            # Its source file stays, but no declaration selects it, so the renderer
            # rejects it as a build target. Retire its publication as a source-less
            # one; declaring the document again registers its source back.
            from .models import DocumentIdentity
            identity = session.get(DocumentIdentity, selected.get("document_id"))
            if identity is not None:
                identity.source_path = None
        if project:
            from .document_deletion import retire_document_items
            retire_document_items(session, store, operation_id=operation_id,
                                  retired_source_documents={selected["source_file"]} if selected.get("source_file") and (not selected.get("imported") or retired) else set(),
                                  retired_component=selected,
                                  filenames=retired)
        else:
            from .document_deletion import retire_document_items
            retire_document_items(session, store, operation_id=operation_id, filenames=retired)
    elif action == "remove":
        from .document_deletion import retire_document_items
        filenames = {d["filename"] for d in current["documents"]}
        filenames.update(d["attachment_filename"] for d in current["documents"] if d.get("attachment_filename"))
        if current.get("target"):
            filenames.add(current["target"])
        filenames = _exclusive_filenames(session, root, store, key, filenames)
        data["retired_filenames"] = sorted(filenames)
        owned = {f["path"] for f in connected_source_folders(current)}
        retire_document_items(session, store, operation_id=operation_id, filenames=filenames,
                              removed_projects=owned)
    elif action != "create":
        raise ValueError("Unsupported project action")
    if record is None:
        record = Entity(entity_type=TYPE, collection="library", entity_id=key, revision=0,
                        deleted=False, winner_operation_id=operation_id, payload=data)
        session.add(record)
    from .project_configuration import canonical_for_project, apply_library_change, current_manifest_path
    configuration = canonical_for_project(session, store, key)
    if action == "create" and configuration is None:
        from .project_configuration import from_legacy
        configuration = from_legacy({"id": key, "name": data["name"], "documents": []}, {})
    if new_folder is not None:
        if configuration is None:
            raise ValueError("This project has no current Project manifest. Sync on the Mac and try again")
        from .new_source_folders import declare
        configuration = declare(configuration, new_folder[0])
    if configuration is not None and action == "remove":
        from .project_configuration import retire_source
        retire_source(session, store, root, operation_id, current_manifest_path(session, key))
    if configuration is not None and action not in {"remove", "rename_source", "set_review_kind"}:
        if action == "import_folder":
            from .project_configuration import retire_source
            new_owner = folder["project"]
            declared = {d["document_id"] for d in configuration["documents"]}
            configuration.setdefault("document_names", {}).update(
                {key: name for key, name in data.get("document_names", {}).items() if key in declared})
        configuration = apply_library_change(configuration, action, request, current, data)
        _save_source(session, store, root, operation_id + ":project-configuration", current_manifest_path(session, key),
                     (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode())
        if action == "import_folder":
            retire_source(session, store, root, operation_id, new_owner + "/thoughts-project.json")
    rules = None
    if new_folder is not None:
        # Its first file, published after the configuration that declares the folder.
        from .new_source_folders import write_rules
        rules = write_rules(session, store, root, operation_id, new_folder[0])
    record.revision += 1
    data.pop("source_project", None)
    record.payload = data
    record.deleted = action == "remove"
    record.winner_operation_id = operation_id
    session.add(Entity(entity_type=RECEIPT, collection="library", entity_id=operation_id, revision=1,
                       deleted=False, winner_operation_id=operation_id, payload={"sha256": digest}))
    session.flush()
    publish_project_reading(session, store, root, record)
    # Retiring a source publication changes the published source index, and
    # clients receive its identity only once its object is stored.
    from .library import store_published_source_index, write_library_index
    store_published_source_index(session, store)
    session.commit()
    if rules is not None:
        from .sources import _materialize_canonical_path
        _materialize_canonical_path(session, store, root, rules, already_locked=True)
    write_library_index(session, root)
    return catalogue(session, root, store)
