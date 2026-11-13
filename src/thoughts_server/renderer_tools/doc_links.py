#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Resolve semantic LaTeX document links in rendered PDFs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from thoughts_server import pdf_transform_cache
from thoughts_server.source_owner import owner_of, relative_to_owner

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        NullObject,
        NumberObject,
        TextStringObject,
    )
except ImportError as exc:  # pragma: no cover - exercised only in missing envs.
    print(
        "Error: pypdf is required to resolve document links. Install it with: python3 -m pip install pypdf",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


# \TexLink and \CrossTexLink are retired. A build still defines them (the
# render wrapper), so an old command typesets its text; rewrite_links leaves
# that link inactive and the warning names the \SourceLink/\CrossSourceLink
# that replaces it.
RETIRED_TEX_LINK_URI_PREFIX = "manual-texlink:"
RETIRED_CROSS_TEX_LINK_URI_PREFIX = "manual-cross-texlink:"
SOURCE_LINK_URI_PREFIX = "manual-sourcelink:"
CROSS_SOURCE_LINK_URI_PREFIX = "manual-cross-sourcelink:"
# A link within a project names the target's own destination (source-label:…),
# which only the component PDF keeps. Publication swaps in this canonical name.
TEX_LINK_DESTINATION_ANNOTATION_KEY = "/ThoughtsTexLinkDestination"
# Marks a link to another project's own published PDF. It names no build input,
# so it is never a build dependency of the linking project.
CROSS_PROJECT_LINK_ANNOTATION_KEY = "/ThoughtsCrossProjectLink"
TEX_LINK_LABEL_SEPARATOR = "?label="
PDF_LINK_URI_PREFIX = "manual-pdflink:"
CROSS_PDF_LINK_URI_PREFIX = "manual-cross-pdflink:"
PDF_LINK_ANCHOR_SEPARATOR = "?anchor="
PDF_LINK_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PDF_LINK_ANNOTATION_PREFIX = "doc-anchor:"
SOURCE_LABEL_PREFIX = "source-label:"
SOURCE_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
# A relative Markdown link that cannot name a document (markdown_links): it
# stays inactive and is reported, like a malformed LaTeX link.
MARKDOWN_INVALID_LINK_URI_PREFIX = "manual-markdown-link:"
MARKDOWN_INVALID_LINK_PROBLEM_SEPARATOR = "?problem="


@dataclass(frozen=True)
class TexLinkTarget:
    selector: str
    project_dir: Path
    source_path: Path
    pdf_path: Path


@dataclass(frozen=True)
class CanonicalTexLinkTarget:
    pdf_filename: str
    destination_name: str


@dataclass(frozen=True)
class CanonicalTexSource:
    selector: str
    pdf_path: Path
    start_page: int


@dataclass(frozen=True)
class CanonicalPdfSource:
    project: str
    key: str
    pdf_path: Path
    start_page: int


@dataclass(frozen=True)
class PdfLinkTarget:
    paper_key: str
    anchor_name: str
    pdf_path: Path
    page_number: int
    left: float
    top: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Resolve and inspect semantic links between docs-* PDFs.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser(
        "resolve-pdf",
        help="Replace semantic link URI markers (SourceLink, PdfLink, …) with PDF links.",
    )
    resolve_parser.add_argument("--docs-root", required=True, type=Path)
    resolve_parser.add_argument("--pdf", required=True, type=Path)

    list_parser = subparsers.add_parser(
        "list-linked-pdfs",
        help="Print appendable docs-* and third-party PDF link targets.",
    )
    list_parser.add_argument("--docs-root", required=True, type=Path)
    list_parser.add_argument("--pdf", required=True, type=Path)

    return parser.parse_args()


def normalize_selector(selector: str) -> str:
    normalized = selector.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    parts = path.parts
    # A docs-* folder or a Project's default folder, then a folder-relative path.
    relative = relative_to_owner(normalized)
    relative_parts = PurePosixPath(relative).parts if relative else ()
    legacy_tex = len(relative_parts) == 1 and relative_parts[0].endswith(".tex")
    source_document = (
        len(relative_parts) >= 1 and (
            Path(relative_parts[-1]).suffix.lower() in {".tex", ".md", ".markdown"}
        )
    )
    if (
        relative is None
        or any(part in {"", ".", ".."} for part in parts)
        or not (legacy_tex or source_document)
        or path.as_posix() != normalized
    ):
        raise ValueError(f"invalid source link target {selector!r}")
    return path.as_posix()


class UnavailableLinkTarget(ValueError):
    """A valid reference points to a missing or unselected document."""


SOURCE_LINK_AVAILABILITY = "library/source-link-availability.json"


def source_link_available(docs_root: Path, source: Path) -> bool:
    # Sparse worker snapshots omit unchanged target sources. Their current
    # availability is recorded separately from the PDFs used for navigation.
    inventory = docs_root / SOURCE_LINK_AVAILABILITY
    if inventory.is_file():
        value = json.loads(inventory.read_bytes())
        if value.get("schema_version") != 1 or not isinstance(value.get("sources"), list):
            raise ValueError("Invalid source-link availability snapshot")
        return source.relative_to(docs_root).as_posix() in value["sources"]
    return source.is_file()


def require_selected_target(docs_root: Path, selector: str) -> None:
    """A link may use a selected document, never enroll an unchecked target."""
    try:
        target_for_selector(docs_root, selector)
    except UnavailableLinkTarget as error:
        raise UnavailableLinkTarget(
            f"unresolved_tex_link: {selector} is not selected for building; "
            "select it in Documents > Select sources to build, or change the link"
        ) from error


def target_for_selector(docs_root: Path, selector: str) -> TexLinkTarget:
    normalized = normalize_selector(selector)
    project_name = owner_of(normalized)
    project_dir = docs_root.resolve() / project_name
    from thoughts_server.project_documents import project_documents, source_token
    from thoughts_server.project_configuration import find
    if find(docs_root, project_name) is None:
        raise UnavailableLinkTarget(f"Source folder is not attached to a Project: {project_name}")
    source_name = relative_to_owner(normalized)
    documents = project_documents(project_dir, allow_missing_inputs=True)
    exact = [entry for entry in documents if entry.source == source_name]
    aliases = [entry for entry in documents if source_token(entry.source) == source_name]
    matches = exact or aliases
    if not matches:
        raise UnavailableLinkTarget(f"Source document is not selected for building: {normalized}")
    if len(matches) != 1:
        raise ValueError(f"source link target is not a unique selected document: {normalized}")
    document = matches[0]
    source_name = document.source
    if not source_link_available(docs_root, project_dir / source_name):
        raise UnavailableLinkTarget(f"Source document cannot be found: {normalized}")
    pdf_path = project_dir / "pdf" / document.output
    return TexLinkTarget(
        selector=normalized,
        project_dir=project_dir,
        source_path=project_dir / source_name,
        pdf_path=pdf_path,
    )


def canonical_tex_destination_name(selector: str, label: str) -> str:
    """The published name of a source label (or document-start) of a document.

    Readers and earlier publications know these names, so the historical
    ``thoughts-texlink-`` prefix stays.
    """
    normalized = normalize_selector(selector)
    if not label:
        raise ValueError("source link label must not be empty")
    digest = hashlib.sha256(f"{normalized}\0{label}".encode()).hexdigest()
    return f"thoughts-texlink-{digest}"


def retired_tex_link_problem(uri: str, docs_root: Path, source_project: str | None) -> str | None:
    """Why an old TexLink/CrossTexLink stays inactive, naming its replacement.

    None when ``uri`` is not one of the retired link URIs.
    """
    if uri.startswith(RETIRED_CROSS_TEX_LINK_URI_PREFIX):
        name, payload = "CrossTexLink", uri[len(RETIRED_CROSS_TEX_LINK_URI_PREFIX):]
    elif uri.startswith(RETIRED_TEX_LINK_URI_PREFIX):
        name, payload = "TexLink", uri[len(RETIRED_TEX_LINK_URI_PREFIX):]
    else:
        return None
    selector, _separator, label = (unquote(part) for part in payload.partition(TEX_LINK_LABEL_SEPARATOR))
    retired = f"\\{name} is retired"
    replacement = "\\SourceLink{path in the same source folder}{label}{text} or " \
                  "\\CrossSourceLink{docs-<folder>/path}{label}{text}"
    try:
        normalized = normalize_selector(selector)
        owner = owner_of(normalized)
        try:
            path = target_for_selector(docs_root, normalized).source_path.relative_to(
                docs_root.resolve() / owner).as_posix()
        except (ValueError, FileNotFoundError):
            path = relative_to_owner(normalized)
        if owner == source_project:
            replacement = f"\\SourceLink{{{path}}}{{{label}}}{{…}}"
        else:
            replacement = f"\\CrossSourceLink{{{owner}/{path}}}{{{label}}}{{…}}"
    except (ValueError, TypeError):
        pass
    target_label = label if label and SOURCE_LABEL_PATTERN.fullmatch(label) else "label"
    return (f"{retired}; use {replacement} and mark the place in the target with "
            f"\\SourceLabel{{{target_label}}}")


def _source_link_payload(payload: str, command: str) -> tuple[str, str, str | None]:
    """(path, label, ignored fragment) of a SourceLink payload.

    LaTeX always writes ``path?label=name``. A Markdown link may omit the label
    (document start) and may carry a fragment that names no Source label; the
    link then opens the document start and the build reports the fragment.
    """
    payload, hash_mark, fragment = payload.partition("#")
    path, separator, label = payload.partition(TEX_LINK_LABEL_SEPARATOR)
    if not path or (separator and not label):
        raise ValueError(f"malformed {command} URI: {payload!r}")
    resolved_label = SOURCE_LABEL_PREFIX + validate_source_label(unquote(label)) if separator else "document-start"
    return unquote(path), resolved_label, unquote(fragment) if hash_mark else None


def parse_source_link_uri(uri: str, source_project: str) -> tuple[str, str] | None:
    if not uri.startswith(SOURCE_LINK_URI_PREFIX):
        return None
    path, label, _fragment = _source_link_payload(uri[len(SOURCE_LINK_URI_PREFIX):], "SourceLink")
    return normalize_selector(f"{source_project}/{path}"), label


def parse_cross_source_link_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith(CROSS_SOURCE_LINK_URI_PREFIX):
        return None
    path, label, _fragment = _source_link_payload(uri[len(CROSS_SOURCE_LINK_URI_PREFIX):], "CrossSourceLink")
    return normalize_selector(path), label


def ignored_source_link_fragment(uri: str) -> str | None:
    """The fragment of a Markdown SourceLink that names no Source label, if any."""
    if not uri.startswith((SOURCE_LINK_URI_PREFIX, CROSS_SOURCE_LINK_URI_PREFIX)) or "#" not in uri:
        return None
    return unquote(uri.partition("#")[2])


def validate_source_label(label: str) -> str:
    if not SOURCE_LABEL_PATTERN.fullmatch(label):
        raise ValueError(f"invalid Source label {label!r}")
    return label


def validate_pdf_link_identifier(value: str, description: str) -> str:
    if not PDF_LINK_IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"invalid PdfLink {description} {value!r}; use letters, digits, dots, underscores, or hyphens"
        )
    return value


def parse_pdf_link_uri(uri: str) -> tuple[str, str] | None:
    if not uri.startswith(PDF_LINK_URI_PREFIX):
        return None
    payload = uri[len(PDF_LINK_URI_PREFIX) :]
    paper_key, separator, anchor_name = payload.partition(PDF_LINK_ANCHOR_SEPARATOR)
    if not separator or not paper_key or not anchor_name:
        raise ValueError(f"malformed PdfLink URI: {uri!r}")
    return (
        validate_pdf_link_identifier(unquote(paper_key), "paper key"),
        validate_pdf_link_identifier(unquote(anchor_name), "anchor name"),
    )


def parse_cross_pdf_link_uri(uri: str) -> tuple[str, str, str] | None:
    if not uri.startswith(CROSS_PDF_LINK_URI_PREFIX):
        return None
    selector, separator, anchor = uri[len(CROSS_PDF_LINK_URI_PREFIX):].partition(PDF_LINK_ANCHOR_SEPARATOR)
    if not separator or not selector or not anchor:
        raise ValueError(f"malformed CrossPdfLink URI: {uri!r}")
    selector, anchor = unquote(selector), validate_pdf_link_identifier(unquote(anchor), "anchor name")
    parts = PurePosixPath(selector).parts
    if len(parts) != 2 or not parts[0].startswith("docs-") or parts[0] == "docs-":
        raise ValueError(f"invalid CrossPdfLink target {selector!r}")
    return parts[0], validate_pdf_link_identifier(parts[1], "paper key"), anchor


def canonical_pdf_destination_name(project: str, key: str, anchor: str) -> str:
    if not project.startswith("docs-") or project == "docs-" or "/" in project:
        raise ValueError(f"invalid PDF link project {project!r}")
    validate_pdf_link_identifier(key, "paper key")
    validate_pdf_link_identifier(anchor, "anchor name")
    digest = hashlib.sha256(f"{project}/third_party/{key}.pdf\0{anchor}".encode()).hexdigest()
    return f"thoughts-texlink-{digest}"


def file_spec_path(file_spec: object) -> str | None:
    if file_spec is None:
        return None
    if hasattr(file_spec, "get_object"):
        file_spec = file_spec.get_object()
    if isinstance(file_spec, dict):
        value = file_spec.get("/F") or file_spec.get("/UF")
        return str(value) if value is not None else None
    return str(file_spec)


def iter_actions(reader: PdfReader):
    for page in reader.pages:
        for annot_ref in page.get("/Annots") or []:
            annotation = annot_ref.get_object()
            action = annotation.get("/A")
            if action:
                yield annotation, action


def tex_links_in_pdf(pdf_path: Path, source_project: str | None = None) -> set[tuple[str, str]]:
    """SourceLink targets (selector, label) of one component PDF."""
    links: set[tuple[str, str]] = set()
    for _annotation, action in iter_actions(PdfReader(str(pdf_path))):
        if action.get("/S") != "/URI":
            continue
        uri = action.get("/URI")
        rendered = str(uri) if uri is not None else ""
        try:
            parsed = None
            if rendered.startswith(SOURCE_LINK_URI_PREFIX):
                if source_project is None:
                    raise ValueError("SourceLink requires a source project")
                parsed = parse_source_link_uri(rendered, source_project)
            if parsed is not None:
                selector, label = parsed
                links.add((normalize_selector(selector), label))
        except ValueError:
            # A malformed link stays inactive; rewrite_links reports it.
            continue
    return links


def cross_tex_links_in_pdf(pdf_path: Path) -> set[tuple[str, str]]:
    """CrossSourceLink targets (selector, label), certified against other folders' PDFs."""
    links: set[tuple[str, str]] = set()
    for _annotation, action in iter_actions(PdfReader(str(pdf_path))):
        if action.get("/S") != "/URI":
            continue
        uri = action.get("/URI")
        rendered = str(uri) if uri is not None else ""
        try:
            parsed = parse_cross_source_link_uri(rendered)
        except ValueError:
            # A malformed link stays inactive; rewrite_links reports it.
            continue
        if parsed is not None:
            links.add(parsed)
    return links


def unresolved_semantic_link_uris(pdf_path: Path) -> list[str]:
    unresolved: list[str] = []
    for _annotation, action in iter_actions(PdfReader(str(pdf_path))):
        if action.get("/S") != "/URI":
            continue
        uri = action.get("/URI")
        rendered = str(uri) if uri is not None else ""
        if rendered.startswith((RETIRED_TEX_LINK_URI_PREFIX, RETIRED_CROSS_TEX_LINK_URI_PREFIX,
                                SOURCE_LINK_URI_PREFIX, CROSS_SOURCE_LINK_URI_PREFIX,
                                PDF_LINK_URI_PREFIX, CROSS_PDF_LINK_URI_PREFIX,
                                MARKDOWN_INVALID_LINK_URI_PREFIX)):
            unresolved.append(rendered)
    return unresolved


def assert_no_unresolved_semantic_links(pdf_path: Path) -> None:
    unresolved = unresolved_semantic_link_uris(pdf_path)
    if unresolved:
        examples = ", ".join(repr(uri) for uri in unresolved[:3])
        raise ValueError(
            f"unresolved_semantic_link: {pdf_path} contains {len(unresolved)} "
            f"unresolved semantic link(s): {examples}"
        )


def _atomic_write_pdf(pdf_path: Path, writer: PdfWriter, original_mode: int) -> None:
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=pdf_path.parent,
            prefix=f".{pdf_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_name = temp_file.name
            writer.write(temp_file)
        os.replace(temp_name, pdf_path)
        os.chmod(pdf_path, original_mode)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def add_canonical_tex_destinations(
    pdf_path: Path,
    sources: list[CanonicalTexSource],
    *, _writer: PdfWriter | None = None,
) -> int:
    pdf_path = pdf_path.resolve()
    original_mode = pdf_path.stat().st_mode
    writer = _writer if _writer is not None else PdfWriter(clone_from=PdfReader(str(pdf_path)))
    # A standalone TeX PDF can retain a multi-level name tree. pypdf's
    # add_named_destination_array only writes /Names on the root; a root with
    # /Kids would then hide the new destinations from conforming readers.
    names = writer.root_object.get("/Names")
    if names is not None:
        names = names.get_object()
        tree = names.get("/Dests")
        if tree is not None and not hasattr(tree.get_object(), "indirect_reference"):
            # HTML/Markdown renderers may emit an inline destination dictionary.
            # pypdf requires an indirect reference when adding a destination.
            tree = writer._add_object(tree.get_object())
            names[NameObject("/Dests")] = tree
        if tree is not None and "/Kids" in tree.get_object():
            flattened = ArrayObject()
            for name, destination in sorted(writer.named_destinations.items()):
                flattened.extend([TextStringObject(name), destination.dest_array])
            names[NameObject("/Dests")] = writer._add_object(DictionaryObject({NameObject("/Names"): flattened}))
    readers: dict[Path, PdfReader] = {}
    added = 0
    seen: set[str] = set()

    def install_destination(name, target):
        entries = writer.get_named_dest_root()
        for index in range(len(entries) - 2, -1, -2):
            if str(entries[index]) == name:
                del entries[index:index + 2]
        writer.add_named_destination_array(TextStringObject(name), target)

    for source in sources:
        if source.pdf_path not in readers:
            readers[source.pdf_path] = PdfReader(str(source.pdf_path))
        component_reader = readers[source.pdf_path]
        component_destinations = component_reader.named_destinations
        labels = {"document-start": None}
        labels.update({name: name for name in component_destinations
                       if name.startswith(SOURCE_LABEL_PREFIX)})
        for label, component_name in labels.items():
            stable_name = canonical_tex_destination_name(source.selector, label)
            if stable_name in seen:
                raise ValueError(f"duplicate canonical source link destination: {stable_name}")
            seen.add(stable_name)
            if component_name is None:
                if not component_reader.pages:
                    raise ValueError(f"source link target {source.pdf_path} has no pages")
                target = ArrayObject([writer.pages[source.start_page].indirect_reference,
                                      NameObject("/XYZ"), NullObject(), NullObject(), NullObject()])
                install_destination(stable_name, target)
                added += 1
                continue
            if component_name not in component_destinations:
                raise ValueError(
                    f"PDF destination {component_name!r} for label {label!r} was not found "
                    f"in {source.pdf_path}"
                )
            destination = component_destinations[component_name]
            local_page = component_reader.get_destination_page_number(destination)
            if local_page is None or not 0 <= local_page < len(component_reader.pages):
                raise ValueError(
                    f"PDF destination {component_name!r} for label {label!r} has an invalid page"
                )
            combined_page = source.start_page + local_page
            if not 0 <= combined_page < len(writer.pages):
                raise ValueError(
                    f"canonical source link destination {stable_name!r} is outside {pdf_path}"
                )
            target = ArrayObject(
                [writer.pages[combined_page].indirect_reference, *destination.dest_array[1:]]
            )
            install_destination(stable_name, target)
            added += 1

    if added and _writer is None:
        _atomic_write_pdf(pdf_path, writer, original_mode)
    return added


def assert_canonical_tex_destinations(
    pdf_path: Path,
    sources: list[CanonicalTexSource],
) -> None:
    destinations = PdfReader(str(pdf_path)).named_destinations
    missing = []
    for source in sources:
        component_destinations = PdfReader(str(source.pdf_path)).named_destinations
        labels = ["document-start", *(name for name in component_destinations
                                      if name.startswith(SOURCE_LABEL_PREFIX))]
        missing.extend(canonical_tex_destination_name(source.selector, label)
                       for label in labels
                       if canonical_tex_destination_name(source.selector, label) not in destinations)
    if missing:
        raise ValueError(
            f"canonical_tex_destination_missing: {pdf_path} is missing "
            f"{len(missing)} canonical destination(s), including {missing[0]!r}"
        )


def add_canonical_pdf_destinations(pdf_path: Path, sources: list[CanonicalPdfSource], docs_root: Path, *, _writer: PdfWriter | None = None) -> int:
    """Give every selected PDF attachment a stable document and annotation target."""
    pdf_path = pdf_path.resolve()
    original_mode = pdf_path.stat().st_mode
    writer = _writer if _writer is not None else PdfWriter(clone_from=PdfReader(str(pdf_path)))
    added = 0
    seen: set[str] = set()
    for source in sources:
        reader = PdfReader(str(source.pdf_path))
        anchors = {"document-start"}
        for page in reader.pages:
            for reference in page.get("/Annots") or []:
                annotation = reference.get_object()
                if str(annotation.get("/Subtype", "")) == "/Link":
                    continue
                contents = str(annotation.get("/Contents", "")).strip()
                if contents.startswith(PDF_LINK_ANNOTATION_PREFIX):
                    anchors.add(validate_pdf_link_identifier(
                        contents[len(PDF_LINK_ANNOTATION_PREFIX):], "anchor name"
                    ))
        for anchor in sorted(anchors):
            target = pdf_link_target(
                docs_root, source.pdf_path,
                source.key, anchor, source.project,
            )
            name = canonical_pdf_destination_name(source.project, source.key, anchor)
            if name in seen:
                raise ValueError(f"duplicate canonical PDF destination: {name}")
            seen.add(name)
            page_number = source.start_page + target.page_number
            destination = ArrayObject([
                writer.pages[page_number].indirect_reference,
                NameObject("/XYZ"),
                FloatObject(target.left),
                FloatObject(target.top),
                NullObject(),
            ])
            writer.add_named_destination_array(TextStringObject(name), destination)
            added += 1
    if added and _writer is None:
        _atomic_write_pdf(pdf_path, writer, original_mode)
    return added


def add_canonical_destinations(pdf_path: Path, tex_sources: list[CanonicalTexSource],
                               pdf_sources: list[CanonicalPdfSource], docs_root: Path) -> int:
    """Install both destination sets in one full-PDF write."""
    if not tex_sources and not pdf_sources:
        return 0
    original_mode = pdf_path.stat().st_mode
    writer = PdfWriter(clone_from=PdfReader(str(pdf_path)))
    added = add_canonical_tex_destinations(pdf_path, tex_sources, _writer=writer)
    added += add_canonical_pdf_destinations(pdf_path, pdf_sources, docs_root, _writer=writer)
    retargeted = use_canonical_tex_link_destinations(writer)
    if added or retargeted:
        _atomic_write_pdf(pdf_path, writer, original_mode)
    return added


def use_canonical_tex_link_destinations(writer: PdfWriter) -> int:
    """Point links to other documents of the project at their canonical destinations.

    Publication drops the TeX destination names of each component PDF and installs
    canonical ones, so a remote link that still names a TeX destination opens nothing.
    """
    retargeted = 0
    for page in writer.pages:
        for annotation_ref in page.get("/Annots") or []:
            annotation = annotation_ref.get_object()
            destination = annotation.get(TEX_LINK_DESTINATION_ANNOTATION_KEY)
            if destination is None:
                continue
            action = annotation.get("/A")
            if action is not None and action.get_object().get("/S") == "/GoToR":
                action.get_object()[NameObject("/D")] = TextStringObject(str(destination))
            del annotation[TEX_LINK_DESTINATION_ANNOTATION_KEY]
            retargeted += 1
    return retargeted


def validate_target(target: TexLinkTarget, label: str) -> str:
    if not target.source_path.is_file():
        raise FileNotFoundError(f"Linked source does not exist for {target.selector}: {target.source_path}")
    if not target.pdf_path.is_file():
        raise FileNotFoundError(f"Linked PDF does not exist for {target.selector}: {target.pdf_path}")

    if label == "document-start":
        if not PdfReader(str(target.pdf_path)).pages:
            raise ValueError(f"source link target {target.pdf_path} has no pages")
        return canonical_tex_destination_name(target.selector, label)
    if label.startswith(SOURCE_LABEL_PREFIX):
        if label not in PdfReader(str(target.pdf_path)).named_destinations:
            raise ValueError(f"Source label {label!r} was not found in {target.pdf_path}")
        return label
    raise ValueError(f"{label!r} is neither document-start nor a Source label")


def pdf_link_pdf(
    docs_root: Path, _source_pdf: Path, paper_key: str, source_project: str | None = None
) -> Path:
    validated_key = validate_pdf_link_identifier(paper_key, "paper key")
    if source_project is not None:
        from thoughts_server.project_attachments import load_project_attachments
        from thoughts_server.project_configuration import find
        if find(docs_root, source_project) is None:
            raise UnavailableLinkTarget(f"Source folder is not attached to a Project: {source_project}")
        attachments = load_project_attachments(docs_root / source_project)
        if f"{validated_key}.pdf" not in {entry.filename for entry in attachments}:
            raise UnavailableLinkTarget(
                f"PdfLink target {validated_key}.pdf is not selected in {source_project}"
            )
        selected = next(entry for entry in attachments if entry.filename == f"{validated_key}.pdf")
        if selected.source:
            candidate = (docs_root / source_project / selected.source).resolve()
            if not candidate.is_file():
                raise FileNotFoundError(f"PdfLink target PDF is unavailable: {candidate}")
            return candidate
    candidate = docs_root.resolve() / "third_party" / f"{validated_key}.pdf"
    target_pdf = candidate.resolve()
    if target_pdf.parent != candidate.parent.resolve():
        raise ValueError(f"PdfLink target escapes the third-party PDF directory: {target_pdf}")
    if target_pdf.is_file():
        return target_pdf
    raise FileNotFoundError(f"PdfLink target PDF {validated_key!r} does not exist; searched: {candidate}")


@dataclass(frozen=True)
class LibraryPdf:
    """An imported PDF a Project selects: that Project's source folders and the PDF's docs-root path."""
    folders: tuple[str, ...]
    path: str


_LIBRARY_PDFS_CACHE: dict[str, tuple[tuple, dict[str, list[LibraryPdf]]]] = {}


def library_pdfs(docs_root: Path, filename: str) -> list[LibraryPdf]:
    """Every Project of the workspace that selects an imported PDF named ``filename``.

    A PDF is identified by its Library filename alone: it lies in its Project's
    default folder, and readers open a link to it by that name. So \\PdfLink and
    \\CrossPdfLink find it wherever it is, and moving it to another Project never
    breaks a link (rewrite_links).
    """
    from thoughts_server import project_configuration as pc
    paths = pc.manifest_files(docs_root)
    identity = tuple((path.as_posix(), *pc._link_identity(path)) for path in paths)
    cached = _LIBRARY_PDFS_CACHE.get(docs_root.as_posix())
    if cached is None or cached[0] != identity:
        index: dict[str, list[LibraryPdf]] = {}
        for path in paths:
            try:
                value = pc.read(path)
            except (OSError, ValueError):
                continue
            folders = tuple(folder["path"] for folder in value["source_folders"])
            for document in value["documents"]:
                if document["format"] == "pdf" and document["included"]:
                    index.setdefault(PurePosixPath(document["source"]["path"]).name, []).append(
                        LibraryPdf(folders, pc.resolve(value, document)))
        cached = (identity, index)
        _LIBRARY_PDFS_CACHE[docs_root.as_posix()] = cached
    return list(cached[1].get(filename, ()))


def _library_pdf_link(annotation: DictionaryObject, pdf_path: Path, docs_root: Path, source_project: str | None,
                      paper_key: str, anchor_name: str, named_owner: str | None) -> list[str]:
    """Resolve \\PdfLink{key} or \\CrossPdfLink{named_owner/key} to the Library PDF named key.pdf.

    The linking document's own Project comes first (a link inside the Project, as
    \\PdfLink always resolved), then the Project of the folder a \\CrossPdfLink
    names, then any Project that selects the PDF: a \\PdfLink to another
    Project's PDF and a \\CrossPdfLink whose folder no longer holds it resolve by
    the PDF's name. Raises UnavailableLinkTarget when no Project selects it.
    """
    filename = f"{validate_pdf_link_identifier(paper_key, 'paper key')}.pdf"
    holders = library_pdfs(docs_root, filename)
    if source_project is not None and any(source_project in holder.folders for holder in holders):
        target = pdf_link_target(docs_root, pdf_path, paper_key, anchor_name, source_project)
        annotation[NameObject("/A")] = pdf_link_action(pdf_path, target)
        return [str(annotation["/A"])]
    holder = next((holder for holder in holders if named_owner in holder.folders), None) if named_owner else None
    holder = holder or (holders[0] if holders else None)
    if holder is None:
        where = f"the Project of {named_owner}" if named_owner else (source_project or "the Library")
        raise UnavailableLinkTarget(f"{filename} is not selected in {where} or in any other Project of the Library")
    if anchor_name == "document-start":
        destination = ArrayObject([NumberObject(0), NameObject("/XYZ"), NullObject(), NullObject(), NullObject()])
    else:
        target = pdf_link_target(docs_root, pdf_path, paper_key, anchor_name, holder.folders[0])
        destination = ArrayObject([NumberObject(target.page_number), NameObject("/XYZ"),
                                   FloatObject(target.left), FloatObject(target.top), NullObject()])
    # Readers open the target's own Library PDF by its filename, which every device keeps.
    annotation[NameObject("/A")] = DictionaryObject({
        NameObject("/S"): NameObject("/GoToR"),
        NameObject("/F"): TextStringObject(filename),
        NameObject("/D"): destination,
    })
    annotation[NameObject(CROSS_PROJECT_LINK_ANNOTATION_KEY)] = BooleanObject(True)
    return ["cross-pdf:" + str(annotation["/A"])]


def annotation_coordinates(annotation: DictionaryObject) -> tuple[float, float]:
    quad_points = annotation.get("/QuadPoints")
    if hasattr(quad_points, "get_object"):
        quad_points = quad_points.get_object()
    if quad_points is not None:
        values = [float(value) for value in quad_points]
        if len(values) >= 8 and len(values) % 2 == 0:
            return min(values[0::2]), max(values[1::2])

    rectangle = annotation.get("/Rect")
    if hasattr(rectangle, "get_object"):
        rectangle = rectangle.get_object()
    if rectangle is not None:
        values = [float(value) for value in rectangle]
        if len(values) == 4:
            return min(values[0], values[2]), max(values[1], values[3])

    raise ValueError("annotation has neither usable QuadPoints nor Rect geometry")


def pdf_link_target(
    docs_root: Path,
    source_pdf: Path,
    paper_key: str,
    anchor_name: str,
    source_project: str | None = None,
) -> PdfLinkTarget:
    validated_anchor = validate_pdf_link_identifier(anchor_name, "anchor name")
    target_pdf = pdf_link_pdf(docs_root, source_pdf, paper_key, source_project)
    marker = f"{PDF_LINK_ANNOTATION_PREFIX}{validated_anchor}"
    reader = PdfReader(str(target_pdf))
    if validated_anchor == "document-start":
        if not reader.pages:
            raise ValueError(f"PdfLink target {target_pdf} has no pages")
        crop_box = reader.pages[0].cropbox
        return PdfLinkTarget(
            paper_key=paper_key,
            anchor_name=validated_anchor,
            pdf_path=target_pdf,
            page_number=0,
            left=float(crop_box.left),
            top=float(crop_box.top),
        )
    matches: list[tuple[int, DictionaryObject]] = []
    for page_number, page in enumerate(reader.pages):
        for annotation_reference in page.get("/Annots") or []:
            annotation = annotation_reference.get_object()
            if str(annotation.get("/Subtype", "")) == "/Link":
                continue
            contents = annotation.get("/Contents")
            if contents is not None and str(contents).strip() == marker:
                matches.append((page_number, annotation))

    if not matches:
        raise ValueError(f"PdfLink anchor {validated_anchor!r} was not found in {target_pdf}")
    if len(matches) != 1:
        raise ValueError(f"PdfLink anchor {validated_anchor!r} occurs {len(matches)} times in {target_pdf}")

    page_number, annotation = matches[0]
    try:
        left, top = annotation_coordinates(annotation)
    except ValueError as exc:
        raise ValueError(f"PdfLink anchor {validated_anchor!r} in {target_pdf} {exc}") from exc
    page = reader.pages[page_number]
    crop_box = page.cropbox
    left = max(float(crop_box.left), left - 12.0)
    top = min(float(crop_box.top), top + 12.0)
    return PdfLinkTarget(
        paper_key=paper_key,
        anchor_name=validated_anchor,
        pdf_path=target_pdf,
        page_number=page_number,
        left=left,
        top=top,
    )


def pdf_link_action(source_pdf: Path, target: PdfLinkTarget) -> DictionaryObject:
    return DictionaryObject(
        {
            NameObject("/S"): NameObject("/GoToR"),
            NameObject("/F"): TextStringObject(os.path.relpath(target.pdf_path, start=source_pdf.parent)),
            NameObject("/D"): ArrayObject(
                [
                    NumberObject(target.page_number),
                    NameObject("/XYZ"),
                    FloatObject(target.left),
                    FloatObject(target.top),
                    NullObject(),
                ]
            ),
        }
    )


SEMANTIC_LINK_COMMANDS = (
    (RETIRED_CROSS_TEX_LINK_URI_PREFIX, "CrossTexLink", TEX_LINK_LABEL_SEPARATOR),
    (CROSS_SOURCE_LINK_URI_PREFIX, "CrossSourceLink", TEX_LINK_LABEL_SEPARATOR),
    (CROSS_PDF_LINK_URI_PREFIX, "CrossPdfLink", PDF_LINK_ANCHOR_SEPARATOR),
    (RETIRED_TEX_LINK_URI_PREFIX, "TexLink", TEX_LINK_LABEL_SEPARATOR),
    (SOURCE_LINK_URI_PREFIX, "SourceLink", TEX_LINK_LABEL_SEPARATOR),
    (PDF_LINK_URI_PREFIX, "PdfLink", PDF_LINK_ANCHOR_SEPARATOR),
)


def semantic_link_command(uri: str, markdown_source: str | None = None) -> str | None:
    """The command a semantic link URI came from, e.g. \\PdfLink{key}{anchor}.

    For a link of a Markdown document (``markdown_source``, its
    docs-root-relative path) the Markdown link that means it, e.g.
    ``Markdown link (paper.pdf#anchor)``.
    """
    if uri.startswith(MARKDOWN_INVALID_LINK_URI_PREFIX):
        href = uri[len(MARKDOWN_INVALID_LINK_URI_PREFIX):].partition(MARKDOWN_INVALID_LINK_PROBLEM_SEPARATOR)[0]
        return f"Markdown link ({unquote(href)})"
    for prefix, name, separator in SEMANTIC_LINK_COMMANDS:
        if uri.startswith(prefix):
            payload = uri[len(prefix):]
            fragment = None
            if name.endswith("SourceLink"):
                payload, hash_mark, fragment = payload.partition("#")
                fragment = unquote(fragment) if hash_mark else None
            target, has_anchor, anchor = payload.partition(separator)
            target, anchor = unquote(target), unquote(anchor) if has_anchor else None
            if markdown_source is not None:
                from thoughts_server import markdown_links
                split = owner_of(markdown_source)
                if split is not None and name in markdown_links.KINDS:
                    try:
                        href = markdown_links.markdown_form(
                            name, target, anchor, split, markdown_source[len(split) + 1:])
                    except ValueError:
                        href = None
                    if href is not None:
                        if fragment is not None:
                            href += "#" + fragment
                        return f"Markdown link ({href})"
            return f"\\{name}{{{target}}}{{{anchor if anchor is not None else 'document-start'}}}"
    return None


def _make_inactive(annotation: DictionaryObject) -> None:
    # Preserve visible page content, but leave no clickable action
    # (including reader-specific markers) for unavailable documents.
    for key in ("/A", "/AA", "/Dest", CROSS_PROJECT_LINK_ANNOTATION_KEY):
        annotation.pop(key, None)
    annotation[NameObject("/Border")] = ArrayObject([NumberObject(0)] * 3)


def rewrite_links(
    pdf_path: Path,
    docs_root: Path,
    *,
    source_project: str | None = None,
    canonical_tex_links: dict[tuple[str, str], CanonicalTexLinkTarget] | None = None,
    warnings: list[str] | None = None,
    link_base: Path | None = None,
    markdown_source: str | None = None,
) -> int:
    """Resolve the semantic links of one component PDF.

    A link that cannot be resolved (its target is not selected, cannot be found,
    is malformed or names a missing anchor or label) stays visible text without
    an action, and only that link: the document and every other document still
    build. Each one is described in warnings, for the build to report.

    ``link_base`` is where the PDF counts as being for relative link targets
    (default: pdf_path), so a copy can be resolved as its component.
    ``markdown_source`` (the docs-root-relative path of a Markdown document)
    words the warnings as the Markdown links they came from.
    """
    pdf_path = pdf_path.resolve()
    docs_root = docs_root.resolve()
    link_base = link_base.resolve() if link_base is not None else pdf_path
    if source_project is None:
        try:
            relative = link_base.relative_to(docs_root)
        except ValueError:
            relative = PurePosixPath()
        owner_relative = relative_to_owner(relative.as_posix())
        if owner_relative is not None and len(PurePosixPath(owner_relative).parts) >= 2:
            source_project = owner_of(relative.as_posix())
    original_mode = pdf_path.stat().st_mode
    reader = PdfReader(str(pdf_path))
    actions = []

    rewrites = 0
    for annotation, action in iter_actions(reader):
        if action.get("/S") != "/URI":
            continue
        uri = action.get("/URI")
        rendered_uri = str(uri) if uri is not None else ""
        try:
            outcome = _rewrite_link(annotation, rendered_uri, link_base, docs_root, source_project, canonical_tex_links)
        except (ValueError, FileNotFoundError) as error:
            # UnavailableLinkTarget is a ValueError. One bad link never fails the build.
            outcome = [f"inactive:{rendered_uri}"], str(error)
        if outcome is None:
            continue
        recorded, problem = outcome
        if problem is not None:
            # Build status and trace events carry no absolute workspace paths.
            problem = problem.replace(str(docs_root) + os.sep, "")
        command = semantic_link_command(rendered_uri, markdown_source) or rendered_uri
        if problem is not None:
            _make_inactive(annotation)
            if warnings is not None:
                warnings.append(f"{command} stays inactive: {problem}")
        elif warnings is not None and (fragment := ignored_source_link_fragment(rendered_uri)) is not None:
            warnings.append(f"{command} opens the start of the document: #{fragment} is not a Source label; "
                            "link a heading marked {#source-label:name} as #source-label:name")
        actions.extend(recorded)
        rewrites += 1

    if rewrites == 0:
        return 0

    # Resolve and validate every current target before considering a cached transformation.
    cache = pdf_transform_cache.location(
        pdf_path, "resolved-links", str(pdf_path.relative_to(docs_root)), actions, Path(__file__)
    )
    if pdf_transform_cache.restore(cache, pdf_path):
        return rewrites
    writer = PdfWriter(clone_from=reader)
    _atomic_write_pdf(pdf_path, writer, original_mode)
    pdf_transform_cache.save(cache, pdf_path)

    return rewrites


def _rewrite_link(
    annotation: DictionaryObject,
    rendered_uri: str,
    pdf_path: Path,
    docs_root: Path,
    source_project: str | None,
    canonical_tex_links: dict[tuple[str, str], CanonicalTexLinkTarget] | None,
) -> tuple[list[str], str | None] | None:
    """Rewrite one URI annotation; None when it is no semantic link.

    Returns the cache action records and, for a link left inactive, why.
    Raises ValueError or FileNotFoundError for a link that cannot resolve.
    """
    if rendered_uri.startswith(MARKDOWN_INVALID_LINK_URI_PREFIX):
        problem = rendered_uri.partition(MARKDOWN_INVALID_LINK_PROBLEM_SEPARATOR)[2]
        return ["inactive:" + rendered_uri], unquote(problem) or "not a link to a document"
    retired = retired_tex_link_problem(rendered_uri, docs_root, source_project)
    if retired is not None:
        return ["inactive:" + rendered_uri], retired
    pdf_reference = parse_pdf_link_uri(rendered_uri)
    cross_pdf_reference = parse_cross_pdf_link_uri(rendered_uri)
    if cross_pdf_reference is not None or (pdf_reference is not None and source_project is not None):
        # A Library PDF is found by its filename, wherever it is (_library_pdf_link).
        named_owner, key, anchor = cross_pdf_reference or (None, *pdf_reference)
        try:
            return _library_pdf_link(annotation, pdf_path, docs_root, source_project, key, anchor, named_owner), None
        except (UnavailableLinkTarget, FileNotFoundError) as error:
            return ["inactive:" + rendered_uri], str(error)
    cross_reference = parse_cross_source_link_uri(rendered_uri)
    source_reference = cross_reference
    if cross_reference and owner_of(cross_reference[0]) == source_project:
        raise ValueError("CrossSourceLink must target a different project; use SourceLink within a project")
    if rendered_uri.startswith(SOURCE_LINK_URI_PREFIX):
        if source_project is None:
            raise ValueError("SourceLink requires a source project")
        source_reference = parse_source_link_uri(rendered_uri, source_project)
    if source_reference is not None or pdf_reference is not None:
        try:
            if source_reference is not None:
                target_for_selector(docs_root, source_reference[0])
            else:
                # Without a source project: the workspace's third_party/ PDFs (legacy layout).
                pdf_link_pdf(docs_root, pdf_path, pdf_reference[0], source_project)
        except (UnavailableLinkTarget, FileNotFoundError) as error:
            return ["inactive:" + rendered_uri], str(error)
        if cross_reference is not None:
            selector, label = cross_reference
            canonical_target = (canonical_tex_links or {}).get((normalize_selector(selector), label))
            if canonical_target is None:
                return ["inactive:" + rendered_uri], (
                    f"{selector} has no published PDF with label {label!r} yet"
                )
    parsed_cross_link = parse_cross_source_link_uri(rendered_uri)
    if parsed_cross_link is not None:
        selector, label = parsed_cross_link
        project = owner_of(selector)
        if project == source_project:
            raise ValueError("CrossSourceLink must target a different project; use SourceLink within a project")
        # The build certified the target document's published PDF and its
        # canonical destination; readers open that PDF, never a build input.
        canonical_target = canonical_tex_links[(normalize_selector(selector), label)]
        annotation[NameObject("/A")] = DictionaryObject({
            NameObject("/S"): NameObject("/GoToR"),
            NameObject("/F"): TextStringObject(canonical_target.pdf_filename),
            NameObject("/D"): TextStringObject(canonical_target.destination_name),
        })
        annotation[NameObject(CROSS_PROJECT_LINK_ANNOTATION_KEY)] = BooleanObject(True)
        return ["cross-tex:" + str(annotation["/A"])], None
    parsed_tex_link = None
    if rendered_uri.startswith(SOURCE_LINK_URI_PREFIX):
        if source_project is None:
            raise ValueError("SourceLink requires a source project")
        parsed_tex_link = parse_source_link_uri(rendered_uri, source_project)
    parsed_pdf_link = parse_pdf_link_uri(rendered_uri)
    if parsed_tex_link is None and parsed_pdf_link is None:
        return None
    recorded = []
    if parsed_tex_link is not None:
        selector, label = parsed_tex_link
        require_selected_target(docs_root, selector)
        target = target_for_selector(docs_root, selector)
        if source_project is not None and target.project_dir != docs_root / source_project:
            canonical_target = (canonical_tex_links or {}).get((target.selector, label))
            if canonical_target is None:
                raise ValueError(
                    f"unresolved_tex_link: no certified canonical target for "
                    f"{target.selector}?label={label}"
                )
            destination_name = canonical_target.destination_name
            relative_pdf = canonical_target.pdf_filename
            destination_marker = None
        else:
            destination_name = validate_target(target, label)
            relative_pdf = os.path.relpath(target.pdf_path, start=pdf_path.parent)
            destination_marker = canonical_tex_destination_name(target.selector, label)
        annotation[NameObject("/A")] = DictionaryObject(
            {
                NameObject("/S"): NameObject("/GoToR"),
                NameObject("/F"): TextStringObject(relative_pdf),
                NameObject("/D"): TextStringObject(destination_name),
            }
        )
        if destination_marker is not None:
            annotation[NameObject(TEX_LINK_DESTINATION_ANNOTATION_KEY)] = TextStringObject(destination_marker)
            recorded.append(str(annotation[TEX_LINK_DESTINATION_ANNOTATION_KEY]))
    else:
        assert parsed_pdf_link is not None
        paper_key, anchor_name = parsed_pdf_link
        target = pdf_link_target(docs_root, pdf_path, paper_key, anchor_name, source_project)
        annotation[NameObject("/A")] = pdf_link_action(pdf_path, target)
    recorded.append(str(annotation["/A"]))
    return recorded, None


def is_docs_pdf(path: Path, docs_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(docs_root.resolve())
    except ValueError:
        return False
    owner_relative = relative_to_owner(relative.as_posix())
    owner_parts = PurePosixPath(owner_relative).parts if owner_relative is not None else ()
    return (
        len(owner_parts) >= 2
        and owner_parts[0] == "pdf"
        and path.suffix.lower() == ".pdf"
    )


def is_third_party_pdf(path: Path, docs_root: Path) -> bool:
    try:
        relative = path.resolve().relative_to(docs_root.resolve())
    except ValueError:
        return False
    if path.suffix.lower() != ".pdf":
        return False
    from thoughts_server.uniform_manifest import managed_pdf_path
    return managed_pdf_path(relative.as_posix()) or (len(relative.parts) == 2 and relative.parts[0] == "third_party")


def linked_docs_pdfs(
    pdf_path: Path,
    docs_root: Path,
) -> list[Path]:
    pdf_path = pdf_path.resolve()
    docs_root = docs_root.resolve()
    reader = PdfReader(str(pdf_path))
    linked: list[Path] = []
    seen: set[Path] = set()
    for _annotation, action in iter_actions(reader):
        if CROSS_PROJECT_LINK_ANNOTATION_KEY in _annotation:
            continue
        action_type = action.get("/S")
        if action_type == "/URI":
            uri = action.get("/URI")
            rendered_uri = str(uri) if uri is not None else ""
            parsed_tex_link = None
            if rendered_uri.startswith(SOURCE_LINK_URI_PREFIX):
                relative = pdf_path.relative_to(docs_root)
                source_project = owner_of(relative.as_posix()) or relative.parts[0]
                parsed_tex_link = parse_source_link_uri(rendered_uri, source_project)
            parsed_pdf_link = parse_pdf_link_uri(rendered_uri)
            if parsed_tex_link is not None:
                selector, _label = parsed_tex_link
                try:
                    target = target_for_selector(docs_root, selector).pdf_path.resolve()
                except UnavailableLinkTarget:
                    continue
            elif parsed_pdf_link is not None:
                paper_key, _anchor_name = parsed_pdf_link
                try:
                    target = pdf_link_pdf(docs_root, pdf_path, paper_key)
                except (UnavailableLinkTarget, FileNotFoundError):
                    continue
            else:
                continue
        elif action_type == "/GoToR":
            file_path = file_spec_path(action.get("/F"))
            if not file_path:
                continue
            target = (pdf_path.parent / file_path).resolve()
        else:
            continue
        if not (is_docs_pdf(target, docs_root) or is_third_party_pdf(target, docs_root)) or target in seen:
            continue
        linked.append(target)
        seen.add(target)
    return linked


def main() -> int:
    args = parse_args()
    docs_root = args.docs_root.resolve()
    pdf_path = args.pdf.resolve()
    try:
        if args.command == "resolve-pdf":
            rewrites = rewrite_links(pdf_path, docs_root)
            print(f"Resolved {rewrites} semantic link(s) in {pdf_path}")
        elif args.command == "list-linked-pdfs":
            for linked_pdf in linked_docs_pdfs(pdf_path, docs_root):
                print(linked_pdf)
        else:  # pragma: no cover - argparse enforces the choices.
            raise ValueError(f"unsupported command: {args.command}")
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
