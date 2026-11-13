# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Figures that agent proposals add to a note (protocol/agent-proposals.md section 11).

An agent figure is an SVG drawing stored as a canonical text source next to the
note that uses it:

    <tex or markdown tree>/source/[<dir>/]agent-figures/<name>.svg

Its first line names the proposal that added it,

    <!-- thoughts-agent-figure proposal="<proposal-id>" -->

and the proposal's replacement references it (``agent-figures/<name>``), so it
appears in the PDF while the proposal is pending and stays when it is accepted.
The proposal ID is deliberately not part of the path: a decided proposal's ID must
occur in no note any more (section 10.2), and an accepted reference stays. LaTeX cannot include SVG, so
every build converts the figures of its project to PDF under
``build/thoughts-agent-figures/`` (outside the authored tree, like every other
generated file); Markdown renders the SVG itself. Figures of a proposal that was
rejected and is no longer referenced are deleted by ``collect_orphans``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import timedelta
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

FIGURE_DIRECTORY = "agent-figures"
GENERATED_DIRECTORY = "thoughts-agent-figures"
FIGURE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
MAX_FIGURE_BYTES = 1024 * 1024
SVG_NAMESPACE = "http://www.w3.org/2000/svg"
XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
# Elements that run code or embed another document; neither belongs in a note figure.
FORBIDDEN_ELEMENTS = {"script", "foreignObject", "iframe", "object", "embed", "audio", "video"}
# A decided proposal's figures stay this long, so Undo on the phone still finds them.
ORPHAN_GRACE = timedelta(days=7)
# Documents of one project compile in parallel threads and share the generated folder.
_CONVERSION_LOCK = threading.Lock()
RETIRED_WITHOUT_FIGURE = {"rejected", "withdrawn", "dismissed", "document_deleted", "decided_in_parts"}
OWNER_MARK = re.compile(r'\A\s*<!-- thoughts-agent-figure proposal="([a-z0-9][a-z0-9._:-]{0,127})" -->')


class FigureError(ValueError):
    pass


def figure_name(path: str) -> str | None:
    """The figure's name when ``path`` (Library- or owner-relative) is an agent figure."""
    parts = PurePosixPath(path).parts
    if len(parts) < 4 or parts[-2] != FIGURE_DIRECTORY or not parts[-1].endswith(".svg"):
        return None
    name = parts[-1][:-4]
    if FIGURE_NAME.fullmatch(name) is None:
        return None
    if any(part.startswith(".") or part in {"", ".", ".."} for part in parts):
        return None
    # The figure must sit inside an authored source tree: .../(tex|markdown)/source/...
    if not any(parts[index] in {"tex", "markdown"} and parts[index + 1] == "source"
               for index in range(len(parts) - 3)):
        return None
    return name


def is_figure_path(path: str) -> bool:
    return figure_name(path) is not None


def figure_owner(content: bytes) -> str | None:
    """The proposal ID a figure's first line names, if any."""
    match = OWNER_MARK.match(content.decode("utf-8", errors="replace"))
    return match.group(1) if match else None


def owner_mark(proposal_id: str) -> str:
    return f'<!-- thoughts-agent-figure proposal="{proposal_id}" -->\n'


def figure_reference(name: str, *, markdown: bool) -> str:
    """How a note in the figure's parent folder refers to it."""
    reference = f"{FIGURE_DIRECTORY}/{name}"
    return reference + ".svg" if markdown else reference


def validate_svg(content: bytes) -> None:
    """Refuse anything but a self-contained SVG drawing."""
    if len(content) > MAX_FIGURE_BYTES:
        raise FigureError(f"the figure exceeds {MAX_FIGURE_BYTES // 1024} KiB")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FigureError("the figure is not valid UTF-8") from error
    if "\0" in text:
        raise FigureError("the figure contains a NUL byte")
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.IGNORECASE):
        raise FigureError("the figure may not declare a DOCTYPE or entities")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as error:
        raise FigureError(f"the figure is not well-formed XML: {error}") from error
    if root.tag not in {f"{{{SVG_NAMESPACE}}}svg", "svg"}:
        raise FigureError("the figure's root element must be <svg> in the SVG namespace")
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1] if isinstance(element.tag, str) else ""
        if local in FORBIDDEN_ELEMENTS:
            raise FigureError(f"the figure may not contain <{local}>")
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1]
            if name.lower().startswith("on"):
                raise FigureError(f"the figure may not contain event handlers ({name})")
            if attribute in {"href", XLINK_HREF}:
                target = value.strip()
                if not (target.startswith("#") or target.startswith("data:image/")):
                    raise FigureError("the figure may only link to its own elements or embed data:image/ URIs")
        style = element.text if local == "style" else element.attrib.get("style", "")
        if style and re.search(r"url\(\s*['\"]?(?!#|data:image/)", style):
            raise FigureError("the figure's styles may only reference its own elements")
        if style and "@import" in style:
            raise FigureError("the figure's styles may not import other stylesheets")


def project_figures(project: Path, roots: Iterable[Path] | None = None) -> list[Path]:
    """The agent figures of one source folder (or of ``roots`` in it), in path order."""
    found = []
    for directory, subdirectories, filenames in (
            item for root in (roots if roots is not None else [project]) if root.is_dir()
            for item in os.walk(root, followlinks=False)):
        parent = Path(directory)
        subdirectories[:] = sorted(
            name for name in subdirectories
            if not name.startswith(".") and not (parent == project and name in {"build", "pdf", "current_pdfs"})
            and not (parent / name).is_symlink()
        )
        if parent.name != FIGURE_DIRECTORY:
            continue
        for name in filenames:
            path = parent / name
            if path.is_file() and not path.is_symlink() and is_figure_path(path.relative_to(project).as_posix()):
                found.append(path)
    return sorted(found)


def generated_pdf(project: Path, figure: Path) -> Path:
    relative = figure.relative_to(project).with_suffix(".pdf")
    return project / "build" / GENERATED_DIRECTORY / relative


def source_of_generated(owner_relative: str) -> str | None:
    """The owner-relative SVG a generated figure PDF was converted from."""
    parts = PurePosixPath(owner_relative).parts
    if parts[:2] != ("build", GENERATED_DIRECTORY) or not owner_relative.endswith(".pdf"):
        return None
    svg = PurePosixPath(*parts[2:]).with_suffix(".svg").as_posix()
    return svg if is_figure_path(svg) else None


def convert_project_figures(project: Path, run: Callable | None = None) -> Path | None:
    """Convert every agent figure of ``project``'s TeX trees to PDF; the generated root, or None without figures.

    The output is deterministic (fixed SOURCE_DATE_EPOCH), and a PDF is only
    rewritten when its SVG changed, so compiled components stay cacheable.
    PDFs whose SVG is gone are removed, so TeX cannot find a stale drawing.
    """
    with _CONVERSION_LOCK:
        return _convert_project_figures(project, run)


def _convert_project_figures(project: Path, run: Callable | None) -> Path:
    root = project / "build" / GENERATED_DIRECTORY
    expected: set[Path] = set()
    converter = shutil.which("rsvg-convert") or "rsvg-convert"
    from .source_inventory import project_tex_roots
    found = project_figures(project, [tree / "source" for tree in project_tex_roots(project)])
    for figure in found:
        content = figure.read_bytes()
        try:
            validate_svg(content)
        except FigureError as error:
            from .render_types import RenderError
            raise RenderError(f"agent_figure_invalid: {figure.relative_to(project).as_posix()}: {error}") from error
        destination = generated_pdf(project, figure)
        stamp = destination.with_name(destination.name + ".sha256")
        expected.update({destination, stamp})
        digest = hashlib.sha256(content).hexdigest()
        if destination.is_file() and stamp.is_file() and stamp.read_text().strip() == digest:
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        environment = {**os.environ, "SOURCE_DATE_EPOCH": "946684800"}
        command = [converter, "--format=pdf", f"--output={destination}", str(figure)]
        try:
            if run is not None:
                run(command, cwd=figure.parent, env=environment)
            else:
                subprocess.run(command, cwd=figure.parent, env=environment, check=True,
                               capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            from .render_types import RenderError
            raise RenderError(
                f"agent_figure_conversion_failed: {figure.relative_to(project).as_posix()}: {error}"
            ) from error
        stamp.write_text(digest + "\n")
    if root.is_dir():
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file() and path not in expected:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if not found and root.is_dir() and not any(root.iterdir()):
            root.rmdir()
    # Without figures TeX's search path stays as it was, so cached components stay valid.
    return root if found else None


def referenced_names(texts: Iterable[str]) -> set[str]:
    """Figure names any note mentions (conservative: the folder is not compared)."""
    pattern = re.compile(re.escape(FIGURE_DIRECTORY) + r"/(" + FIGURE_NAME.pattern + r")(?![a-z0-9_-])")
    found: set[str] = set()
    for text in texts:
        found.update(pattern.findall(text))
    return found


def collect_orphans(session_factory, store, worktree_root: Path, *, now=None) -> list[str]:
    """Delete the figures of retired proposals that no note references any more.

    Only figures whose first line names their proposal are considered. A figure
    stays while its proposal is pending or removed outside Thoughts
    (Restore puts the wrapper back), while it is referenced from any source
    (an accepted proposal), and for ORPHAN_GRACE after the decision (Undo).
    Returns the deleted paths.
    """
    from sqlalchemy import func, select

    from .models import Device, Entity, Operation, SourceFile
    from .schemas import SourcePublicationMutation, SourcePublicationRequest
    from .sources import publish_sources
    from .models import utc_now

    now = now or utc_now()
    with session_factory() as session:
        rows = list(session.scalars(select(SourceFile).where(SourceFile.deleted.is_(False))))
        figures = [row for row in rows if is_figure_path(row.path)]
        if not figures:
            return []
        texts = []
        for row in rows:
            if row.path.lower().endswith((".tex", ".md", ".markdown", ".sty")):
                try:
                    texts.append(store.path(row.object_sha256).read_bytes().decode("utf-8", errors="replace"))
                except OSError:
                    return []  # Without every text the references are unknown: keep everything.
        referenced = referenced_names(texts)
        candidates = []
        for row in figures:
            if figure_name(row.path) in referenced:
                continue
            try:
                proposal_id = figure_owner(store.path(row.object_sha256).read_bytes())
            except OSError:
                continue
            if proposal_id is None:
                continue
            entity = session.scalar(
                select(Entity).where(Entity.entity_type == "proposal", Entity.entity_id == proposal_id)
            )
            if entity is None or (entity.payload or {}).get("status") not in RETIRED_WITHOUT_FIGURE:
                continue
            decided = entity.updated_at
            if decided is not None and decided.tzinfo is None:
                decided = decided.replace(tzinfo=now.tzinfo)
            if decided is None or now - decided < ORPHAN_GRACE:
                continue
            candidates.append(row)
        if not candidates:
            return []
        device_id = "thoughts-agent-figures"
        if session.get(Device, device_id) is None:
            from .auth import internal_token_hash
            session.add(Device(id=device_id, name="Thoughts agent figures",
                               token_hash=internal_token_hash(), enabled=True))
            session.flush()
        next_sequence = int(session.scalar(
            select(func.max(Operation.device_sequence)).where(Operation.device_id == device_id)) or 0) + 1
        mutations = [
            SourcePublicationMutation(
                action="delete",
                operation_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"agent-figure-orphan:{row.path}:{row.sha256}")),
                device_sequence=next_sequence + index,
                path=row.path,
                base_revision=row.revision,
                base_sha256=row.sha256,
                base_object_sha256=row.object_sha256,
                client_timestamp=now.isoformat().replace("+00:00", "Z"),
            )
            for index, row in enumerate(candidates)
        ]
        session.commit()
        publish_sources(
            session, store, worktree_root, device_id,
            SourcePublicationRequest(publication_id=str(uuid.uuid4()), mutations=mutations),
        )
        return [row.path for row in candidates]
