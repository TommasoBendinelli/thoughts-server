# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""One-time Markdown and LaTeX folder copies, validated before Library activation.

The upload is an immutable JSON object, never an archive to extract or commands
from a README to execute. Each import has its own source namespace; the existing
Library project owns it without replacing its prior sources or publications.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import posixpath
import re
import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

from sqlalchemy import func, select

from .models import BuildJob, ObjectRecord, Operation, Publication
from .project_documents import MARKDOWN_ASSET_SUFFIXES, MARKDOWN_SUFFIXES

LATEX_SUFFIXES = {".tex", ".sty", ".cls", ".bib", ".bst", ".txt", ".png", ".jpg", ".jpeg", ".pdf", ".eps", ".svg", ".csv", ".dat"}
HTML_SUFFIXES = {".html", ".htm"}
LATEX_ROOTS = {"source", "fragments", "support", "assets", "generated"}

IN_PROCESS_RENDER_LOCK = threading.Lock()


def latex_file(path):
    parts = PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "tex" or parts[1] not in LATEX_ROOTS:
        return False
    suffixes = LATEX_SUFFIXES if parts[1] in {"source", "assets", "generated"} else {".tex", ".sty", ".cls", ".bib", ".bst", ".txt"}
    return PurePosixPath(path).suffix.lower() in suffixes | HTML_SUFFIXES


def input_role(path, latex):
    if path == ".ThoughtIgnore":
        return None
    if PurePosixPath(path).suffix.lower() in HTML_SUFFIXES:
        return "asset"
    if latex:
        parts = PurePosixPath(path).parts
        if parts[1] == "source" and PurePosixPath(path).suffix.lower() in LATEX_SUFFIXES - {".tex", ".sty", ".cls", ".bib", ".bst", ".txt"}:
            return "asset"
        return {"assets": "asset", "generated": "generated"}.get(parts[1])
    return "asset" if PurePosixPath(path).suffix.lower() in MARKDOWN_ASSET_SUFFIXES else None


MAX_FILES = 1000
MAX_BYTES = 100 * 1024 * 1024
MAX_FILE_BYTES = 10 * 1024 * 1024


def safe_path(value):
    if not isinstance(value, str) or not value or len(value) > 700:
        raise ValueError("Invalid folder path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(p in {".", ".."} or p.startswith(".") for p in path.parts)
        or "\\" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError(f"Unsafe folder path: {value}")
    return value


def local_target(source, url, files, kind="Link"):
    """The folder file a relative link names, or None for a link checked elsewhere.

    Links to other documents of the Library are checked by the build, like
    LaTeX links (markdown_links): a bare ``paper.pdf`` names an imported PDF of
    the Project, and a path that leaves the imported folder names a document
    or imported PDF of another source folder. Neither is a file of the import.
    """
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc or not parsed.path:
        if parsed.scheme and parsed.scheme not in {"http", "https", "mailto"}:
            raise ValueError(f"Unsupported link scheme: {parsed.scheme}")
        return None
    relative = unquote(parsed.path)
    if kind == "Link" and "/" not in relative and PurePosixPath(relative).suffix.lower() == ".pdf":
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(source), relative))
    if kind == "Link" and (path == ".." or path.startswith("../")):
        if PurePosixPath(path).suffix.lower() not in {".md", ".markdown", ".tex", ".pdf"}:
            raise ValueError(f"A link that leaves the folder must name a document or an imported PDF: {url}")
        return None
    if path == ".":
        for name in ("README.md", "readme.md", "index.md"):
            if name in files:
                return name
    safe_path(path)
    if path not in files:
        for name in ("README.md", "readme.md", "index.md"):
            candidate = posixpath.join(path, name)
            if candidate in files:
                return candidate
        if any(p.startswith(path + "/") for p in files):
            return path + "/"
    return path


def references(ast):
    if isinstance(ast, dict):
        if ast.get("t") in {"Link", "Image"}:
            yield ast["t"], ast["c"][-1][0]
        for value in ast.values():
            yield from references(value)
    elif isinstance(ast, list):
        for value in ast:
            yield from references(value)


def title(raw, path):
    match = re.search(r"^#\s+(.+?)\s*#*\s*$", raw.decode("utf-8"), re.M)
    return (match[1].strip() if match else PurePosixPath(path).stem)[:200]


def read_bundle(raw):
    if len(raw) > MAX_BYTES * 2:
        raise ValueError("The folder exceeds the 100 MB import limit")
    bundle = json.loads(raw)
    if not isinstance(bundle, dict) or bundle.get("schema_version") != 1:
        raise ValueError("Unsupported folder import format")
    source_format = bundle.get("format", "markdown")
    if source_format not in {"markdown", "latex"}:
        raise ValueError("Unsupported source folder format")
    latex = source_format == "latex"
    entries = bundle.get("files")
    selected = bundle.get("documents")
    if not isinstance(entries, list) or not 0 <= len(entries) <= MAX_FILES:
        raise ValueError("Choose a folder with at most 1000 files")
    if (
        not isinstance(selected, list)
        or (not selected and "included_documents" not in bundle)
        or any(not isinstance(p, str) for p in selected)
        or len(selected) != len(set(selected))
    ):
        raise ValueError("Select at least one source document, without duplicates")
    files, folded, total = {}, set(), 0
    included = bundle.get("included_documents", selected)
    if not isinstance(included, list) or any(p not in selected for p in included) or len(included) != len(set(included)):
        raise ValueError("Invalid build selection")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "content"}
            or not isinstance(entry["content"], str)
        ):
            raise ValueError("Invalid folder file entry")
        path = entry["path"] if entry["path"] == ".ThoughtIgnore" else safe_path(entry["path"])
        if path.casefold() in folded:
            raise ValueError(f"Duplicate folder path: {path}")
        suffix = PurePosixPath(path).suffix.lower()
        supported = latex_file(path) if latex else suffix in MARKDOWN_SUFFIXES | MARKDOWN_ASSET_SUFFIXES | HTML_SUFFIXES
        if path != ".ThoughtIgnore" and not supported:
            raise ValueError(f"Unsupported folder file: {path}")
        data = base64.b64decode(entry["content"], validate=True)
        if suffix in MARKDOWN_SUFFIXES and len(data) > 4 * 1024 * 1024:
            raise ValueError(f"Markdown documents must be at most 4 MB: {path}")
        total += len(data)
        if len(data) > (100_000_000 if suffix in HTML_SUFFIXES else MAX_FILE_BYTES) or total > MAX_BYTES:
            raise ValueError(f"Folder import size limit exceeded: {path}")
        if path == ".ThoughtIgnore" or suffix in HTML_SUFFIXES or (latex and input_role(path, True) is None):
            try:
                data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(f"Folder file is not UTF-8: {path}") from error
        files[path] = data
        folded.add(path.casefold())
    from .thought_ignore import ThoughtIgnore
    ignored = ThoughtIgnore(Path("."), files.get(".ThoughtIgnore"))
    for path in files:
        if path != ".ThoughtIgnore" and ignored.excludes(path if latex else "markdown/source/" + path):
            raise ValueError(f"Folder includes a file excluded by .ThoughtIgnore: {path}")
    if latex:
        documents = []
        for path in selected:
            safe_path(path)
            parent = PurePosixPath(path).parent
            if path not in files or not parent.is_relative_to("tex/source") or not path.endswith(".tex"):
                raise ValueError(f"LaTeX document unavailable: {path}")
            document = {"format": "latex", "source": path, "title": PurePosixPath(path).stem}
            if "included_documents" in bundle:
                document["included"] = path in included
            if bundle.get("repository_layout") or parent.as_posix() != "tex/source":
                # An entry point inputs its files relative to its own directory.
                document["working_directory"] = parent.as_posix()
            documents.append(document)
        return documents, files
    from .markdown_source import parse_markdown

    documents, assets = [], {p for p in files if PurePosixPath(p).suffix.lower() in HTML_SUFFIXES}
    for path in selected:
        safe_path(path)
        if path not in files or PurePosixPath(path).suffix.lower() not in MARKDOWN_SUFFIXES:
            raise ValueError(f"Markdown document unavailable: {path}")
        if path not in included:
            documents.append({"format": "markdown", "source": "markdown/source/" + path,
                              "output": "document_" + hashlib.sha256(path.encode()).hexdigest()[:20] + ".pdf",
                              "title": title(files[path], path), "included": False})
            continue
        try:
            parsed = parse_markdown(files[path])
            for kind, url in references(parsed.ast):
                target = local_target(path, url, files, kind)
                if target is None:
                    if kind == "Image":
                        raise ValueError("Images must be included in the selected folder")
                    continue
                if target not in files:
                    if kind == "Link" and target.endswith("/"):
                        assets.update(
                            p
                            for p in files
                            if p.startswith(target)
                            and PurePosixPath(p).suffix.lower() in MARKDOWN_ASSET_SUFFIXES
                        )
                        continue
                    raise ValueError(f"Missing {'image' if kind == 'Image' else 'link target'}: {url}")
                if kind == "Image":
                    if PurePosixPath(target).suffix.lower() not in MARKDOWN_ASSET_SUFFIXES:
                        raise ValueError(f"Unsupported image: {url}")
                    assets.add(target)
                elif PurePosixPath(target).suffix.lower() in MARKDOWN_SUFFIXES:
                    if target not in included:
                        raise ValueError(f"Linked document is not selected: {target}")
                else:
                    assets.add(target)
            documents.append(
                {
                    "format": "markdown",
                    "source": "markdown/source/" + path,
                    "output": "document_" + hashlib.sha256(path.encode()).hexdigest()[:20] + ".pdf",
                    "title": title(files[path], path),
                }
            )
        except Exception as error:
            raise ValueError(f"{path}: {error}") from error
    retained = [*selected, *sorted(assets)]
    if ".ThoughtIgnore" in files:
        retained.append(".ThoughtIgnore")
    return documents, {p: files[p] for p in retained}


def document_outputs(project, documents):
    """Per-document PDF names, derived exactly as Project builds derive them."""
    from .project_documents import document_output
    return {d["source"]: document_output(project, d) for d in documents}


def requested_destination(request) -> str | None:
    """The folder an import creates, from its request alone, or None when the request is invalid.

    A current client proposes a Rule-N name (``source_project``,
    source_folder_names.import_folder_name); an old client proposes none and
    gets the legacy ``docs-import-<32 hex>`` of its operation id. Import and its
    cleanup both use this, so they always name the same folder.
    """
    import uuid

    from .source_folder_names import is_rule_n_name
    proposed = request.get("source_project")
    if proposed is not None:
        return proposed if is_rule_n_name(proposed) else None
    try:
        return "docs-import-" + uuid.UUID(str(request.get("operation_id", ""))).hex
    except ValueError:
        return None


def import_destination(session, root, request) -> str:
    """Validate the folder an import creates (HTTP 409 ``source_folder_exists`` when refused)."""
    from .source_folder_names import SourceFolderExists, taken_names
    proposed = request.get("source_project")
    destination = requested_destination(request)
    if destination is None:
        if proposed is not None:
            raise SourceFolderExists(f"Invalid source folder name {proposed!r}: expected docs-<slug>-<hex>")
        raise ValueError("Invalid import operation")
    if proposed is not None and destination.casefold() in {name.casefold() for name in taken_names(session, root)}:
        raise SourceFolderExists(f"A source folder named {destination} exists or existed; choose another name")
    return destination


def import_folder(session, store, root, request, settings, *, trace_owner_device_id=None):
    """Return a verified, registered private source project; caller commits binding.

    Only a fresh namespace is materialized. The caller owns the DB transaction.
    """
    from .build_inputs import _manifest_bytes, register_build_input_snapshot
    from .builds import BuildWorker
    from .library_projects import SYSTEM_DEVICE, _save_source
    from .native_renderer import CLOSURE_EXPECTATIONS, render
    from .schemas import BuildInputSnapshotRequest
    from .source_resources import api_source_resources
    from .sources import source_snapshot_identity

    sha = request.get("sha256", "")
    record = session.get(ObjectRecord, sha)
    if not record or record.size_bytes > MAX_BYTES * 2 or not store.matches(sha, record.size_bytes):
        raise ValueError("The uploaded folder is unavailable or failed verification")
    documents, files = read_bundle(store.path(sha).read_bytes())
    project = import_destination(session, root, request)
    latex = json.loads(store.path(sha).read_bytes()).get("format") == "latex"
    prefix = "" if latex else "markdown/source/"
    outputs = document_outputs(project, documents)
    from .document_identity import register_document
    document_id = request.get("document_id")
    if not document_id:
        raise ValueError("The imported source project has no permanent document identifier")
    # The folder keeps its permanent identity; PDFs are published per document.
    register_document(session, document_id, project.removeprefix("docs-") + ".pdf")
    source_ids = request.get("source_document_ids", {})
    for document in documents:
        document["document_id"] = source_ids.get(document["source"])
        register_document(session, document["document_id"], outputs[document["source"]], project + "/" + document["source"])
    names = {d["document_id"]: d["title"] for d in documents}
    destination = root / project
    from .uniform_manifest import storage
    from . import project_configuration as pc
    manifest = (
        json.dumps(storage({"schema_version": 4, "document_id": document_id, "documents": [{**{k: v for k, v in d.items() if k != "title"}, "included": d.get("included", True)} for d in documents]}), ensure_ascii=False, indent=2) + "\n"
    ).encode()
    declaration = json.loads(manifest)
    configuration = pc.canonical_for_project(session, store, request["project_id"])
    if configuration is None:
        raise ValueError("The destination Project has no current manifest")
    configuration = pc.add_folder(configuration, project, declaration)
    configuration.setdefault("document_names", {}).update(names)
    if destination.exists() or destination.is_symlink() or any(session.get(Publication, o) for o in outputs.values()):
        raise ValueError("The import destination already exists; reconcile it before importing again")
    _save_source(session, store, root, request["operation_id"] + ":import-configuration", pc.configured_manifest_path(configuration),
                 (json.dumps(configuration, ensure_ascii=False, indent=2) + "\n").encode())
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".folder-import-", dir=root) as temporary:
        stage = Path(temporary)
        inputs, captured = [], []
        for path, content in files.items():
            relative = project + "/" + ("" if path == ".ThoughtIgnore" else prefix) + path
            out = stage / relative
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(content)
            if input_role(path, latex) is None:
                # Canonical sources are captured, as a worker build captures them;
                # LaTeX closure verification rejects any uncaptured authored input.
                captured.append({"path": relative, "sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content)})
            elif (role := input_role(path, latex)):
                inputs.append(
                    {
                        "path": relative,
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size_bytes": len(content),
                        "media_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                        "role": role,
                    }
                )
        inputs.sort(key=lambda x: x["path"])
        stage_configuration = pc.add_folder(pc.from_legacy({"id": request["project_id"], "name": configuration["name"], "documents": []}, {}), project, declaration)
        stage_configuration["document_names"] = names
        pc.save(stage, stage_configuration)

        def save_sources():
            for path, content in files.items():
                relative = project + "/" + ("" if path == ".ThoughtIgnore" else prefix) + path
                if input_role(path, latex) is None:
                    _save_source(session, store, root, request["operation_id"], relative, content)
                else:
                    store.put_bytes(session, content, mimetypes.guess_type(path)[0] or "application/octet-stream")
                    out = root / relative
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_bytes(content)

        def register_inputs():
            # Every declared document builds from the folder's inputs, so a later
            # worker build of any of them, including unselected ones, is verifiable.
            for target in dict.fromkeys(outputs.values()):
                register_build_input_snapshot(session, store, SYSTEM_DEVICE, BuildInputSnapshotRequest(
                    schema_version=3, target=target, project=project, discovery_policy="source-closure-v2", entries=inputs),
                    trace_owner_device_id=trace_owner_device_id, commit=False)

        def save_sources_without_pdf(render_error: str | None = None):
            try:
                save_sources()
                register_inputs()
                result = {"project": project, "folder": request.get("name", "Imported folder"), "sha256": sha, "format": "latex" if latex else "markdown", "document_names": names}
                if render_error is not None:
                    result["build_error"] = render_error
                return result
            except Exception:
                shutil.rmtree(destination, ignore_errors=True)
                raise
        built = [d for d in documents if d.get("included", True)]
        if not built:
            return save_sources_without_pdf()
        targets = [outputs[d["source"]] for d in built]
        snapshots = {
            target: hashlib.sha256(
                _manifest_bytes(target, inputs, project=project, discovery_policy="source-closure-v2")
            ).hexdigest()
            for target in targets
        }
        (stage / CLOSURE_EXPECTATIONS).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "expectations": [
                        {
                            "target": target,
                            "project": project,
                            "discovery_policy": "source-closure-v2",
                            "snapshot_sha256": snapshots[target],
                            "entries": inputs,
                            "source_entries": captured,
                        }
                        for target in targets
                    ],
                }
            )
        )
        (stage / "library").mkdir()
        (stage / "library/source-project-policy.json").write_text(
            '{"schema_version":1,"excluded_projects":[]}'
        )
        publication_root = stage / "current_pdfs"
        publication_root.mkdir()
        (publication_root / ".thoughts-source-index.json").write_text(
            json.dumps({"schema_version": 1, "pdfs": [{"filename": target, "project": project} for target in targets]})
        )
        artifacts = stage / "artifacts"
        try:
            # The renderer's page extractor is one per process and not thread-safe;
            # with several Libraries in one API process, imports render one at a time.
            from .build_sandbox import sandboxed
            with IN_PROCESS_RENDER_LOCK, sandboxed(settings.sandbox_builds):
                render(stage, publication_root, artifacts, targets=targets, jobs=1, render_validation=False)
        except Exception as error:
            # Source delivery is independent of the selected PDF build.  Keep
            # the saved files and the build failure, without publishing a PDF.
            return save_sources_without_pdf(str(error))
        worker = BuildWorker(settings, None, store)
        verified = []
        for document in built:
            target = outputs[document["source"]]
            metadata = worker._document_artifact_entry(target, artifacts)
            pdf = store.put_bytes(session, (publication_root / target).read_bytes(), "application/pdf")
            source_manifest = worker._ingest_document_source_artifact(
                session, target, metadata["source_artifact_sha256"], artifacts
            )
            verified.append((document, target, metadata, pdf, source_manifest))
        current = root / "current_pdfs"
        # The canonical workspace is touched only after every page and artifact passed validation.
        try:
            save_sources()
            frontier = (
                session.scalar(
                    select(func.max(Operation.server_sequence)).where(Operation.entity_type == "source")
                )
                or 0
            )
            for document, target, metadata, pdf, source_manifest in verified:
                metadata.update(
                    kind="source_linked",
                    display_name=document["title"],
                    source_resources=api_source_resources(source_manifest.get("resources"), project),
                )
                placement = worker._placement_manifest(
                    session,
                    BuildJob(source_frontier=frontier, annotation_frontier=0),
                    target,
                    document["document_id"],
                    pdf.sha256,
                    publication_root / target,
                )
                placement_object = store.put_bytes(
                    session,
                    (json.dumps(placement, sort_keys=True) + "\n").encode(),
                    "application/vnd.thoughts.annotation-placement+json",
                )
                session.add(Publication(
                    filename=target,
                    document_id=document["document_id"],
                    project=project,
                    object_sha256=pdf.sha256,
                    size_bytes=pdf.size_bytes,
                    source_frontier=frontier,
                    source_snapshot_sha256=source_snapshot_identity(session, frontier),
                    input_snapshot_sha256=snapshots[target],
                    placement_manifest_sha256=placement_object.sha256,
                    source_artifact_sha256=metadata["source_artifact_sha256"],
                    metadata_json=metadata,
                ))
            session.flush()
            register_inputs()
            current.mkdir(exist_ok=True)
            for _, target, *_ in verified:
                shutil.copyfile(publication_root / target, current / target)
            return {"project": project, "folder": request.get("name", "Imported folder"), "sha256": sha, "format": "latex" if latex else "markdown", "document_names": names}
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            for _, target, *_ in verified:
                (current / target).unlink(missing_ok=True)
            raise
