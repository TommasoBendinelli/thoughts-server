# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unicodedata
from contextlib import suppress
from pathlib import Path
from typing import Any

import pdfplumber
from PIL import Image, ImageDraw, ImageOps
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import native_renderer, project_configuration
from .agent_proposals import AgentProposalError, _load_bundle
from .build_inputs import BuildInputError, materialize_build_input_snapshots
from .child_python import SAFE_PATH_ENVIRONMENT, module_command
from .config import Settings
from .database import Database
from .models import BuildInputSnapshot, BuildJob, Operation, Publication
from .object_store import ObjectStore
from .project_attachments import ProjectAttachmentError, materialize_workspace_attachments
from .source_owner import is_owner
from .sources import _materialize, source_snapshot_identity, worktree_lock

SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROPOSAL_ID = re.compile(r"^[a-z0-9][a-z0-9._:-]{0,127}$")
PREVIEW_SCHEMA_VERSION = 1
PREVIEW_DPI = 144
PREVIEW_PADDING_POINTS = 20.0
PREVIEW_PAGE_GAP_PIXELS = 18
PREVIEW_RENDER_REVISION = 4
PREVIEW_HIGHLIGHT = (255, 230, 64, 96)


class ProposalPreviewError(ValueError):
    pass


class ProposalPreviewTimeout(ProposalPreviewError):
    pass


def _acquire_preview_lock(lock, deadline: float) -> None:
    while True:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProposalPreviewTimeout(
                    "Proposal preview timed out waiting for an existing render"
                ) from None
            time.sleep(min(0.05, remaining))


def _preview_environment(settings: Settings) -> dict[str, str] | None:
    """The API's environment; for a sandboxed Library without credentials and sandboxed."""
    if not settings.sandbox_builds:
        # Never inherit a flag a concurrent sandboxed import set in this process, and say
        # so explicitly: the sandbox is on by default where it exists.
        return dict(os.environ) | {"THOUGHTS_SANDBOX_BUILDS": "false"} | SAFE_PATH_ENVIRONMENT
    environment = {
        name: value for name, value in os.environ.items()
        if name not in {"OPENAI_API_KEY", "THOUGHTS_REVIEW_AGENT_SCRIPT", "THOUGHTS_BOOTSTRAP_TOKENS"}
        and not name.startswith("RESTIC_")
    }
    environment["THOUGHTS_SANDBOX_BUILDS"] = "true"
    return environment | SAFE_PATH_ENVIRONMENT


def _preview_command(request: Path) -> list[str]:
    # The worker renders a Library's files; they must never be importable.
    return module_command("thoughts_server.proposal_preview_worker", str(request))


def _run_preview_job(
    settings: Settings,
    manifest: dict[str, Any],
    current_pdf: Path,
    artifact_sha256: str,
    proposal_id: str,
    log_root: Path,
    timeout: float,
) -> dict[str, Any]:
    if timeout <= 0:
        raise ProposalPreviewTimeout("Proposal preview exceeded its rendering time limit")
    logger = logging.getLogger("uvicorn.error")
    started = time.monotonic()
    # Preview inputs include the complete existing dependency set. Keep their
    # isolated copies on the state volume rather than exhausting Fedora's /tmp
    # tmpfs while another preview is still running.
    job_root = settings.state_root / "proposal-preview-jobs"
    job_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="job-", dir=job_root) as directory:
        workspace = Path(directory)
        request = workspace / "request.json"
        request.write_text(
            json.dumps(
                {
                    "worktree_root": str(settings.worktree_root),
                    "database_url": settings.database_url,
                    "object_root": str(settings.object_root),
                    "build_parallel_jobs": settings.build_parallel_jobs,
                    "manifest": manifest,
                    "current_pdf": str(current_pdf),
                    "artifact_sha256": artifact_sha256,
                    "proposal_id": proposal_id,
                }
            ),
            encoding="utf-8",
        )
        log_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=proposal_id + "-", suffix=".render.log", dir=log_root, delete=False
        ) as log:
            logger.info(
                "proposal_preview_started artifact=%s proposal=%s target=%s log=%s",
                artifact_sha256,
                proposal_id,
                manifest["pdf"]["filename"],
                log.name,
            )
            process = subprocess.Popen(
                _preview_command(request), stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                env=_preview_environment(settings),
            )
            try:
                code = process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
            except BaseException as error:
                # The group contains only this preview and its compiler children.
                # Wait for them to exit before removing their private workspace.
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                logger.warning(
                    "proposal_preview_stopped artifact=%s proposal=%s elapsed=%.3f reason=%s log=%s",
                    artifact_sha256,
                    proposal_id,
                    time.monotonic() - started,
                    type(error).__name__,
                    log.name,
                )
                if isinstance(error, subprocess.TimeoutExpired):
                    raise ProposalPreviewTimeout(
                        "Proposal preview exceeded its rendering time limit; cached previews were preserved"
                    ) from error
                raise
            result_path = workspace / "result.json"
            if code != 0 or not result_path.is_file():
                error_path = workspace / "error.json"
                error = json.loads(error_path.read_text()).get("error", "") if error_path.is_file() else ""
                logger.warning(
                    "proposal_preview_failed artifact=%s proposal=%s elapsed=%.3f exit=%s log=%s",
                    artifact_sha256,
                    proposal_id,
                    time.monotonic() - started,
                    code,
                    log.name,
                )
                raise ProposalPreviewError(error or "The server could not render the requested proposal preview")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if (
                result.get("artifact_sha256") != artifact_sha256
                or result.get("proposal_id") != proposal_id
                or result.get("target") != manifest["pdf"]["filename"]
            ):
                raise ProposalPreviewError("Rendered proposal preview has an inconsistent identity")
            logger.info(
                "proposal_preview_completed artifact=%s proposal=%s elapsed=%.3f log=%s",
                artifact_sha256,
                proposal_id,
                time.monotonic() - started,
                log.name,
            )
            return result


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _proposal(manifest: dict[str, Any], proposal_id: str) -> dict[str, Any]:
    for value in manifest.get("agent_proposals", []):
        if isinstance(value, dict) and value.get("id") == proposal_id:
            return value
    raise ProposalPreviewError("agent proposal is not in the active document artifact")


def _manifest(
    store: ObjectStore,
    settings: Settings,
    artifact_sha256: str,
) -> dict[str, Any]:
    path = store.path(artifact_sha256)
    if not path.is_file():
        try:
            return _load_bundle(settings, artifact_sha256)
        except AgentProposalError as error:
            raise ProposalPreviewError(str(error)) from error
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ProposalPreviewError("source artifact is unreadable") from error
    if _sha256(raw) != artifact_sha256:
        raise ProposalPreviewError("source artifact failed its identity check")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProposalPreviewError("source artifact is invalid JSON") from error
    if (
        manifest.get("schema_version") != 1
        or manifest.get("kind") != "thoughts-document-source-artifact"
        or not isinstance(manifest.get("agent_proposals"), list)
    ):
        raise ProposalPreviewError("source artifact does not support agent proposals")
    return manifest


def _role_is_empty(proposal: dict[str, Any], role: str) -> bool:
    content = proposal.get(role)
    return (
        isinstance(content, dict)
        and isinstance(content.get("byte_start"), int)
        and content.get("byte_start") == content.get("byte_end")
    )


def _role_regions(manifest: dict[str, Any], proposal_id: str, role: str) -> list[dict[str, Any]]:
    proposal = _proposal(manifest, proposal_id)
    placement = proposal.get("placement")
    if not isinstance(placement, dict):
        return []
    fragments: list[dict[str, Any]] = []
    for region in placement.get("regions", []):
        if not isinstance(region, dict) or region.get("role") != role:
            continue
        fragments.extend(value for value in region.get("quad_fragments", []) if isinstance(value, dict))
    return fragments


def _highlighted_crop(
    rendered: Image.Image,
    crop_box: tuple[int, int, int, int],
    fragments: list[dict[str, Any]],
    scale_x: float,
    scale_y: float,
) -> Image.Image:
    crop = rendered.crop(crop_box).convert("RGBA")
    overlay = Image.new("RGBA", crop.size, (0, 0, 0, 0))
    painter = ImageDraw.Draw(overlay)
    for fragment in fragments:
        left = round(float(fragment["left"]) * scale_x) - crop_box[0]
        top = round(float(fragment["top"]) * scale_y) - crop_box[1]
        right = round(float(fragment["right"]) * scale_x) - crop_box[0]
        bottom = round(float(fragment["bottom"]) * scale_y) - crop_box[1]
        if right > left and bottom > top:
            painter.rectangle((left, top, right, bottom), fill=PREVIEW_HIGHLIGHT)
    return Image.alpha_composite(crop, overlay).convert("RGB")


def _normalized_rendered_text(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", value).casefold() if character.isalnum()
    )


def _exact_text_fragments(
    document: Any,
    fragments: list[dict[str, Any]],
    selected_text: str,
) -> list[dict[str, Any]]:
    needle = _normalized_rendered_text(selected_text)
    candidate_pages = sorted(
        {
            int(fragment["page"])
            for fragment in fragments
            if isinstance(fragment.get("page"), int) and int(fragment["page"]) > 0
        }
    )
    if len(needle) < 4 or not candidate_pages:
        return []

    stream: list[str] = []
    source_characters: list[tuple[int, dict[str, Any]]] = []
    for page_number in candidate_pages:
        if page_number > len(document.pages):
            continue
        for character in document.pages[page_number - 1].chars:
            normalized = _normalized_rendered_text(str(character.get("text") or ""))
            for item in normalized:
                stream.append(item)
                source_characters.append((page_number, character))
    haystack = "".join(stream)
    offsets: list[int] = []
    offset = 0
    while (offset := haystack.find(needle, offset)) >= 0:
        offsets.append(offset)
        offset += max(1, len(needle))
    if len(offsets) != 1:
        return []

    matched = source_characters[offsets[0] : offsets[0] + len(needle)]
    grouped: dict[tuple[int, float, float], list[dict[str, Any]]] = {}
    seen: set[tuple[int, int]] = set()
    for page_number, character in matched:
        identity = (page_number, id(character))
        if identity in seen:
            continue
        seen.add(identity)
        key = (
            page_number,
            round(float(character["top"]), 1),
            round(float(character["bottom"]), 1),
        )
        grouped.setdefault(key, []).append(character)
    return [
        {
            "page": page_number,
            "left": min(float(character["x0"]) for character in characters),
            "top": min(float(character["top"]) for character in characters),
            "right": max(float(character["x1"]) for character in characters),
            "bottom": max(float(character["bottom"]) for character in characters),
        }
        for (page_number, _top, _bottom), characters in sorted(grouped.items())
    ]


def _preview_image(
    pdf_path: Path,
    fragments: list[dict[str, Any]],
    selected_text: str,
) -> bytes:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for fragment in fragments:
        page = fragment.get("page")
        coordinates = [fragment.get(key) for key in ("left", "top", "right", "bottom")]
        if (
            isinstance(page, int)
            and page > 0
            and all(isinstance(value, (int, float)) for value in coordinates)
        ):
            by_page.setdefault(page, []).append(fragment)
    if not by_page:
        raise ProposalPreviewError("rendered proposal regions are unavailable")

    crops: list[Image.Image] = []
    with pdfplumber.open(pdf_path) as document:
        exact_fragments = _exact_text_fragments(document, fragments, selected_text)
        if exact_fragments:
            by_page.clear()
            for fragment in exact_fragments:
                by_page.setdefault(int(fragment["page"]), []).append(fragment)
        for page_number, page_fragments in sorted(by_page.items()):
            if page_number > len(document.pages):
                continue
            page = document.pages[page_number - 1]
            page_fragments = [
                {
                    **fragment,
                    "left": float(fragment["left"]) * float(page.width),
                    "right": float(fragment["right"]) * float(page.width),
                    "top": float(fragment["top"]) * float(page.height),
                    "bottom": float(fragment["bottom"]) * float(page.height),
                } if fragment.get("normalized") else fragment
                for fragment in page_fragments
            ]
            rendered = page.to_image(resolution=PREVIEW_DPI, antialias=True).original.convert("RGB")
            left = max(0.0, min(float(value["left"]) for value in page_fragments) - PREVIEW_PADDING_POINTS)
            top = max(0.0, min(float(value["top"]) for value in page_fragments) - PREVIEW_PADDING_POINTS)
            right = min(
                float(page.width),
                max(float(value["right"]) for value in page_fragments) + PREVIEW_PADDING_POINTS,
            )
            bottom = min(
                float(page.height),
                max(float(value["bottom"]) for value in page_fragments) + PREVIEW_PADDING_POINTS,
            )
            if right <= left or bottom <= top:
                continue
            scale_x = rendered.width / float(page.width)
            scale_y = rendered.height / float(page.height)
            crop_box = (
                max(0, round(left * scale_x)),
                max(0, round(top * scale_y)),
                min(rendered.width, round(right * scale_x)),
                min(rendered.height, round(bottom * scale_y)),
            )
            if crop_box[2] <= crop_box[0] or crop_box[3] <= crop_box[1]:
                continue
            highlighted = _highlighted_crop(
                rendered,
                crop_box,
                page_fragments,
                scale_x,
                scale_y,
            )
            crops.append(ImageOps.expand(highlighted, border=1, fill=(205, 205, 205)))
    if not crops:
        raise ProposalPreviewError("rendered proposal regions are outside the preview PDF")

    width = max(image.width for image in crops)
    height = sum(image.height for image in crops) + PREVIEW_PAGE_GAP_PIXELS * (len(crops) - 1)
    combined = Image.new("RGB", (width, height), "white")
    y = 0
    for image in crops:
        combined.paste(image, ((width - image.width) // 2, y))
        y += image.height + PREVIEW_PAGE_GAP_PIXELS
    output = io.BytesIO()
    combined.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _copy_preview_inputs(source_root: Path, destination: Path, project: str) -> None:
    # Project configuration is restored from publication provenance below;
    # copying the live configuration could change the published document order.
    ignored = shutil.ignore_patterns(".git", "build", "pdf", "__pycache__", ".DS_Store")
    project_source = source_root / project
    if not project_source.is_dir():
        raise ProposalPreviewError("proposal source project is unavailable on the server")
    shutil.copytree(project_source, destination / project, ignore=ignored)
    for relative in ("library", "comments", "third_party"):
        source = source_root / relative
        if source.is_dir():
            shutil.copytree(source, destination / relative, ignore=ignored)
    source_index = source_root / "current_pdfs/.thoughts-source-index.json"
    if not source_index.is_file():
        raise ProposalPreviewError("Library source index is unavailable on the server")
    target_index = destination / "current_pdfs/.thoughts-source-index.json"
    target_index.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_index, target_index)


def _materialize_preview_configuration(
    session: Session, store: ObjectStore, publication: Publication, destination: Path,
) -> None:
    """Restore a current-format configuration at the published source frontier."""
    if (
        not publication.source_snapshot_sha256
        or source_snapshot_identity(session, publication.source_frontier)
        != publication.source_snapshot_sha256
    ):
        raise ProposalPreviewError("published Project source provenance is unavailable or inconsistent")
    winners = {}
    for operation in session.scalars(
        select(Operation).where(
            Operation.entity_type == "source",
            Operation.server_sequence <= publication.source_frontier,
            Operation.outcome.in_({"applied", "merged"}),
            # History before the default-folder migration spells the folder ``documents``.
            project_configuration.manifest_path_filter(Operation.entity_id),
        ).order_by(Operation.server_sequence)
    ):
        winners[operation.entity_id] = operation
    matches = []
    try:
        for path, operation in winners.items():
            if operation.payload.get("deleted") is True:
                continue
            digest = operation.payload.get("result_object_sha256")
            if not isinstance(digest, str) or not SHA256.fullmatch(digest):
                raise ValueError("missing configuration object identity")
            raw = store.path(digest).read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest or operation.payload.get("result_sha256") != digest:
                raise ValueError("configuration object identity mismatch")
            value = project_configuration.validate(json.loads(raw))
            if path != project_configuration.configured_manifest_path(value):
                raise ValueError("configuration path identity mismatch")
            if any(folder["path"] == publication.project for folder in value["source_folders"]):
                matches.append((path, raw))
        if len(matches) != 1:
            raise ValueError("published source has no unique current Project configuration")
        path, raw = matches[0]
        _materialize(destination, path, raw, already_locked=True)
    except (OSError, UnicodeError, ValueError) as error:
        raise ProposalPreviewError(f"published Project configuration is unavailable or unsupported: {error}") from error


def _materialize_preview_inputs(
    settings: Settings,
    manifest: dict[str, Any],
    artifact_sha256: str,
    destination: Path,
) -> None:
    """Restore the published build's immutable inputs only in the private copy."""
    from .source_folder_names import same_location
    if destination.resolve() == settings.worktree_root.resolve():
        raise ProposalPreviewError("proposal inputs require an isolated workspace")
    database = Database(settings.database_url)
    try:
        with database.session_factory() as session:
            pdf = manifest["pdf"]
            publication = session.get(Publication, pdf["filename"])
            if (
                publication is None
                or not same_location(session, publication.project, pdf["project"])
                or publication.source_artifact_sha256 != artifact_sha256
                or publication.object_sha256 != pdf["pdf_sha256"]
            ):
                raise ProposalPreviewError("proposal preview PDF is no longer active")
            snapshot = (
                session.get(BuildInputSnapshot, publication.input_snapshot_sha256)
                if publication.input_snapshot_sha256 else None
            )
            if (
                snapshot is None
                or snapshot.target != pdf["filename"]
                or not same_location(session, snapshot.project, pdf["project"])
                or snapshot.schema_version != 3
            ):
                raise ProposalPreviewError("published proposal build inputs are unavailable")
            snapshot_ids = [snapshot.sha256]
            job = session.get(BuildJob, publication.build_id) if publication.build_id else None
            linked_inputs = (job.progress or {}).get("input_snapshot_sources") if job else None
            if isinstance(linked_inputs, dict) and linked_inputs:
                # Reproduce the original build's overlap resolution, including
                # shared external PDFs, rather than selecting newer uploads.
                if linked_inputs.get(pdf["filename"]) != snapshot.sha256:
                    raise ProposalPreviewError("published proposal build inputs are inconsistent")
                snapshot_ids = list(linked_inputs.values())
            store = ObjectStore(settings.object_root)
            _materialize_preview_configuration(session, store, publication, destination)
            materialize_build_input_snapshots(session, store, destination, snapshot_ids)
            materialize_workspace_attachments(session, store, destination, [publication.project])
    except (BuildInputError, ProjectAttachmentError) as error:
        raise ProposalPreviewError(f"proposal build inputs: {error}") from error
    finally:
        database.close()


def _seed_preview_publications(
    settings: Settings, workspace: Path, publication_root: Path, target: str
) -> None:
    """Make certified cross-document link targets available without touching active PDFs."""
    index = workspace / "current_pdfs/.thoughts-source-index.json"
    payload = json.loads(index.read_text(encoding="utf-8"))
    publication_root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(index, publication_root / index.name)
    store = ObjectStore(settings.object_root)
    for entry in payload.get("pdfs", []):
        if not isinstance(entry, dict):
            continue
        filename, digest = entry.get("filename"), entry.get("pdf_sha256")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.endswith(".pdf")
            or filename == target
            or not isinstance(digest, str)
            or not SHA256.fullmatch(digest)
        ):
            continue
        # Only references to immutable objects are shared; the preview target is
        # always a new file. The renderer verifies hashes and named destinations.
        source = store.path(digest)
        destination = publication_root / filename
        if source.is_file() and not destination.exists():
            destination.symlink_to(source.resolve())


def _candidate_manifest(
    settings: Settings,
    manifest: dict[str, Any],
    proposal_id: str,
    target: str,
    project: str,
    *,
    artifact_sha256: str,
    temporary_root: Path | None = None,
) -> tuple[Path, dict[str, Any], tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory(prefix="thoughts-proposal-preview-", dir=temporary_root)
    root = Path(temporary.name)
    try:
        with worktree_lock(settings.worktree_root):
            source_proposal = _proposal(manifest, proposal_id)
            source_file = source_proposal.get("source_file")
            source_sha256 = source_proposal.get("source_sha256")
            source_path = settings.worktree_root / str(source_file)
            if (
                not isinstance(source_file, str)
                or not isinstance(source_sha256, str)
                or not source_path.is_file()
                or _sha256(source_path.read_bytes()) != source_sha256
            ):
                raise ProposalPreviewError("proposal source changed after preview publication")
            _copy_preview_inputs(settings.worktree_root, root, project)
        _materialize_preview_inputs(settings, manifest, artifact_sha256, root)
        publication_root = root / "preview-pdfs"
        artifact_root = root / "preview-artifacts"
        _seed_preview_publications(settings, root, publication_root, target)
        outputs = native_renderer.render(
            root,
            publication_root,
            artifact_root,
            targets=[target],
            jobs=settings.build_parallel_jobs,
            render_validation=False,
            prepare_comments=False,
            preview_proposal_id=proposal_id,
        )
        preview_pdf = next((path for path in outputs if path.name == target), None)
        pointer_path = artifact_root / "documents" / f"{target}.json"
        if preview_pdf is None or not pointer_path.is_file():
            raise ProposalPreviewError("The server did not produce the proposal preview")
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        artifact_sha256 = pointer.get("artifact_sha256")
        if not isinstance(artifact_sha256, str) or not SHA256.fullmatch(artifact_sha256):
            raise ProposalPreviewError("proposal preview artifact identity is invalid")
        preview_manifest = json.loads(
            (artifact_root / "objects" / artifact_sha256).read_text(encoding="utf-8")
        )
        return preview_pdf, preview_manifest, temporary
    except Exception:
        temporary.cleanup()
        raise


def _role_record(state: str, message: str, image: bytes | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"state": state, "message": message}
    if image is not None:
        record.update(
            {
                "media_type": "image/png",
                "image_base64": base64.b64encode(image).decode("ascii"),
                "image_sha256": _sha256(image),
            }
        )
    return record


def _render_preview(
    settings: Settings,
    manifest: dict[str, Any],
    current_pdf: Path,
    artifact_sha256: str,
    proposal_id: str,
    workspace: Path,
) -> dict[str, Any]:
    """Render the same preview payload inside an independently bounded process."""
    proposal = _proposal(manifest, proposal_id)
    target = manifest["pdf"]["filename"]
    project = manifest["pdf"]["project"]
    roles: dict[str, dict[str, Any]] = {}
    if _role_is_empty(proposal, "after"):
        roles["after"] = _role_record(
            "empty",
            "No rendered content after this removal.",
        )
    else:
        after_regions = _role_regions(manifest, proposal_id, "after")
        roles["after"] = _role_record(
            "ready",
            "Rendered from the active PDF.",
            _preview_image(
                current_pdf,
                after_regions,
                str(proposal.get("after", {}).get("preview") or ""),
            ),
        )

    if _role_is_empty(proposal, "before"):
        roles["before"] = _role_record(
            "empty",
            "No rendered content before this insertion.",
        )
    else:
        preview_pdf, preview_manifest, temporary = _candidate_manifest(
            settings,
            manifest,
            proposal_id,
            target,
            project,
            artifact_sha256=artifact_sha256,
            temporary_root=workspace,
        )
        try:
            before_regions = _role_regions(preview_manifest, proposal_id, "before")
            roles["before"] = _role_record(
                "ready",
                "Rendered by the server from an isolated original-content preview.",
                _preview_image(
                    preview_pdf,
                    before_regions,
                    str(proposal.get("before", {}).get("preview") or ""),
                ),
            )
        finally:
            temporary.cleanup()

    result = {
        "schema_version": PREVIEW_SCHEMA_VERSION,
        "render_revision": PREVIEW_RENDER_REVISION,
        "kind": "thoughts-agent-proposal-rendered-preview",
        "artifact_sha256": artifact_sha256,
        "proposal_id": proposal_id,
        "target": target,
        "roles": roles,
    }
    return result


def proposal_preview_record(
    session: Session,
    store: ObjectStore,
    settings: Settings,
    artifact_sha256: str,
    proposal_id: str,
) -> dict[str, Any]:
    if not SHA256.fullmatch(artifact_sha256) or not PROPOSAL_ID.fullmatch(proposal_id):
        raise ProposalPreviewError("invalid proposal preview identity")
    manifest = _manifest(store, settings, artifact_sha256)
    _proposal(manifest, proposal_id)
    pdf = manifest.get("pdf")
    target = pdf.get("filename") if isinstance(pdf, dict) else None
    project = pdf.get("project") if isinstance(pdf, dict) else None
    if (
        not isinstance(target, str)
        or Path(target).name != target
        or not target.endswith(".pdf")
        or not is_owner(project)
    ):
        raise ProposalPreviewError("proposal preview target is invalid")
    publication = session.get(Publication, target)
    if (
        publication is None
        or publication.source_artifact_sha256 != artifact_sha256
        or publication.object_sha256 != pdf.get("pdf_sha256")
    ):
        raise ProposalPreviewError("proposal preview PDF is no longer active")
    current_pdf = store.path(publication.object_sha256)
    if not current_pdf.is_file() or _sha256(current_pdf.read_bytes()) != publication.object_sha256:
        raise ProposalPreviewError("proposal preview PDF failed its identity check")

    cache = settings.state_root / "proposal-previews" / artifact_sha256 / f"{proposal_id}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    lock_path = cache.with_suffix(".lock")
    deadline = time.monotonic() + min(240, settings.build_timeout_seconds)
    # Waiting for another render and compiling can take minutes. End the read-only
    # transaction so the connection goes back to the pool meanwhile; the publication
    # is read again below. A caller in the middle of a write keeps its transaction.
    if not (session.new or session.dirty or session.deleted):
        session.commit()
    with lock_path.open("a+b") as lock:
        _acquire_preview_lock(lock, deadline)
        try:
            if cache.is_file():
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if (
                    cached.get("schema_version") == PREVIEW_SCHEMA_VERSION
                    and cached.get("render_revision") == PREVIEW_RENDER_REVISION
                    and cached.get("artifact_sha256") == artifact_sha256
                    and cached.get("proposal_id") == proposal_id
                ):
                    return cached

            result = _run_preview_job(
                settings,
                manifest,
                current_pdf,
                artifact_sha256,
                proposal_id,
                cache.parent,
                deadline - time.monotonic(),
            )
            # Rendering may outlive a concurrent publication. Never activate a
            # preview against a PDF revision that stopped being current.
            session.refresh(publication)
            if publication.source_artifact_sha256 != artifact_sha256 or publication.object_sha256 != pdf.get(
                "pdf_sha256"
            ):
                raise ProposalPreviewError("proposal preview PDF is no longer active")
            encoded = (json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n").encode()
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{cache.name}.", dir=cache.parent)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, cache)
            finally:
                Path(temporary_name).unlink(missing_ok=True)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
