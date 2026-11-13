#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rewrite included-PDF links and local named links as combined-PDF jumps."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        BooleanObject,
        Destination,
        DictionaryObject,
        Fit,
        IndirectObject,
        NameObject,
        NumberObject,
    )
except ImportError as exc:  # pragma: no cover - exercised only in missing envs.
    print(
        "Error: pypdf is required to rewrite shared PDF links. Install it with: python3 -m pip install pypdf",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc


@dataclass
class IncludedPdf:
    path: Path
    start_page: int
    reader: PdfReader
    named_destinations: dict[str, object]
    logical_id: str
    display_title: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Convert relative links to project and appended shared PDFs into "
            "internal jumps inside a combined PDF."
        )
    )
    parser.add_argument("--pdf", required=True, type=Path, help="Combined PDF to rewrite in place.")
    parser.add_argument(
        "--project-pdf",
        action="append",
        default=[],
        type=Path,
        help=("Project PDF included before appended shared PDFs, in combined-PDF order. May be repeated."),
    )
    parser.add_argument(
        "--shared-pdf",
        action="append",
        default=[],
        type=Path,
        help="Shared PDF appended after project PDFs. May be repeated in append order.",
    )
    return parser.parse_args()


def normalize_path(path: Path) -> str:
    return os.path.normcase(os.path.realpath(path))


def file_spec_path(file_spec: object) -> str | None:
    if file_spec is None:
        return None
    if isinstance(file_spec, str):
        return file_spec
    if hasattr(file_spec, "get_object"):
        file_spec = file_spec.get_object()
    if isinstance(file_spec, dict):
        value = file_spec.get("/F") or file_spec.get("/UF")
        return str(value) if value is not None else None
    return str(file_spec)


def _outline_destinations(items: list[object]) -> list[Destination]:
    return [item for item in items if isinstance(item, Destination)]


def _normalized_title(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _sole_first_page_root_title(reader: PdfReader) -> str | None:
    roots = _outline_destinations(reader.outline)
    if len(roots) != 1 or reader.get_destination_page_number(roots[0]) != 0:
        return None
    title = str(roots[0].title).strip()
    return title or None


def humanized_pdf_title(path: Path) -> str:
    title = re.sub(r"[_-]+", " ", path.stem)
    title = re.sub(r"\s+", " ", title).strip()
    return title[:1].upper() + title[1:] if title else path.name


def resolve_display_title(
    path: Path,
    reader: PdfReader,
    explicit_title: str | None = None,
) -> str:
    if explicit_title and explicit_title.strip():
        return explicit_title.strip()
    metadata_title = str((reader.metadata or {}).get("/Title") or "").strip()
    if metadata_title:
        return metadata_title
    outline_title = _sole_first_page_root_title(reader)
    if outline_title:
        return outline_title
    return humanized_pdf_title(path)


def build_included_pdfs(
    project_pdfs: list[Path],
    shared_pdfs: list[Path],
    *,
    logical_ids: dict[Path, str] | None = None,
    display_titles: dict[Path, str] | None = None,
) -> dict[str, IncludedPdf]:
    start_page = 0
    included: dict[str, IncludedPdf] = {}
    normalized_logical_ids = {
        path.resolve(): value for path, value in (logical_ids or {}).items()
    }
    normalized_display_titles = {
        path.resolve(): value for path, value in (display_titles or {}).items()
    }
    for included_pdf in [*project_pdfs, *shared_pdfs]:
        resolved = included_pdf.resolve()
        reader = PdfReader(str(resolved))
        included[normalize_path(resolved)] = IncludedPdf(
            path=resolved,
            start_page=start_page,
            reader=reader,
            named_destinations=reader.named_destinations,
            logical_id=normalized_logical_ids.get(resolved, resolved.name),
            display_title=resolve_display_title(
                resolved,
                reader,
                normalized_display_titles.get(resolved),
            ),
        )
        start_page += len(reader.pages)
    return included


def page_number_for_reference(reader: PdfReader, reference: IndirectObject) -> int:
    for page_number, page in enumerate(reader.pages):
        page_reference = page.indirect_reference
        if (
            page_reference is not None
            and page_reference.idnum == reference.idnum
            and page_reference.generation == reference.generation
        ):
            return page_number
    raise ValueError(f"remote destination page {reference!r} was not found")


class MissingNamedDestination(ValueError):
    """An existing PDF contains a dangling named destination."""


def internal_destination(
    writer: PdfWriter,
    included_pdf: IncludedPdf,
    remote_destination: object,
) -> ArrayObject:
    local_page = 0
    view = [NameObject("/Fit")]

    if hasattr(remote_destination, "get_object") and not isinstance(remote_destination, (str, ArrayObject)):
        remote_destination = remote_destination.get_object()

    if isinstance(remote_destination, ArrayObject):
        if remote_destination:
            page_spec = remote_destination[0]
            if isinstance(page_spec, IndirectObject):
                local_page = page_number_for_reference(included_pdf.reader, page_spec)
            else:
                local_page = int(page_spec)
        if len(remote_destination) > 1:
            view = list(remote_destination[1:])
    elif remote_destination is not None:
        destination_name = str(remote_destination)
        named_destinations = included_pdf.named_destinations
        if destination_name not in named_destinations and destination_name.startswith("/"):
            destination_name = destination_name[1:]
        if destination_name not in named_destinations:
            raise MissingNamedDestination(f"named destination {destination_name!r} was not found in {included_pdf.path}")
        destination = named_destinations[destination_name]
        local_page = included_pdf.reader.get_destination_page_number(destination)
        view = list(destination.dest_array[1:])

    if local_page < 0 or local_page >= len(included_pdf.reader.pages):
        raise ValueError(f"destination page {local_page} is outside {included_pdf.path}")

    combined_page = writer.pages[included_pdf.start_page + local_page]
    return ArrayObject([combined_page.indirect_reference, *view])


def included_pdf_for_page(included_pdfs: dict[str, IncludedPdf], page_number: int) -> IncludedPdf:
    for included_pdf in included_pdfs.values():
        end_page = included_pdf.start_page + len(included_pdf.reader.pages)
        if included_pdf.start_page <= page_number < end_page:
            return included_pdf
    raise ValueError(f"combined page {page_number} is outside all included PDFs")


def copy_outline_items(
    writer: PdfWriter,
    included_pdf: IncludedPdf,
    items: list[object],
    parent: object | None = None,
) -> int:
    copied = 0
    previous_item = None
    for item in items:
        if isinstance(item, list):
            if previous_item is None:
                raise ValueError(f"outline in {included_pdf.path} has children without a parent")
            copied += copy_outline_items(
                writer,
                included_pdf,
                item,
                parent=previous_item,
            )
            continue

        if not isinstance(item, Destination):
            raise ValueError(f"unsupported outline item {item!r} in {included_pdf.path}")

        title = str(item.title)
        local_page = included_pdf.reader.get_destination_page_number(item)
        if local_page is None or not 0 <= local_page < len(included_pdf.reader.pages):
            raise ValueError(f"outline {title!r} in {included_pdf.path} has an invalid destination")

        color = item.color
        outline_color = tuple(float(component) for component in color) if color is not None else None
        font_format = int(item.font_format)
        fit = Fit(str(item.typ), tuple(item.dest_array[2:]))
        open_state = item.get("/%is_open%", True)
        previous_item = writer.add_outline_item(
            title,
            included_pdf.start_page + local_page,
            parent=parent,
            color=outline_color,
            bold=bool(font_format & 2),
            italic=bool(font_format & 1),
            fit=fit,
            is_open=bool(getattr(open_state, "value", open_state)),
        )
        copied += 1

    return copied


def replace_outlines(
    writer: PdfWriter,
    included_pdfs: dict[str, IncludedPdf],
) -> int:
    writer._root_object.pop(NameObject("/Outlines"), None)
    copied = 0
    for included_pdf in included_pdfs.values():
        parent = writer.add_outline_item(
            included_pdf.display_title,
            included_pdf.start_page,
            bold=True,
            fit=Fit.fit(),
            is_open=False,
        )
        copied += 1
        items = included_pdf.reader.outline
        roots = _outline_destinations(items)
        promotes_existing_root = (
            len(roots) == 1
            and included_pdf.reader.get_destination_page_number(roots[0]) == 0
            and _normalized_title(str(roots[0].title))
            == _normalized_title(included_pdf.display_title)
        )
        if promotes_existing_root:
            root_index = items.index(roots[0])
            child_items = (
                items[root_index + 1]
                if root_index + 1 < len(items) and isinstance(items[root_index + 1], list)
                else []
            )
            child_count = copy_outline_items(
                writer,
                included_pdf,
                child_items,
                parent=parent,
            )
        else:
            child_count = copy_outline_items(
                writer,
                included_pdf,
                items,
                parent=parent,
            )
        copied += child_count
        parent_object = parent.get_object()
        parent_object[NameObject("/%is_open%")] = BooleanObject(False)
        parent_object[NameObject("/Count")] = NumberObject(-child_count if child_count else 0)
    return copied


def rewrite_combined_pdf(
    pdf_path: Path,
    included_pdfs: dict[str, IncludedPdf],
) -> tuple[int, int]:
    original_mode = pdf_path.stat().st_mode
    reader = PdfReader(str(pdf_path))
    had_outlines = bool(reader.outline)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    outline_entries = replace_outlines(writer, included_pdfs)

    rewrites = 0
    for page_number, page in enumerate(writer.pages):
        annots = page.get("/Annots")
        if not annots:
            continue
        source_pdf = included_pdf_for_page(included_pdfs, page_number)
        for annot_ref in annots:
            annot = annot_ref.get_object()
            # Links to another Project's PDF stay external even if a file of that name is included.
            if "/ThoughtsCrossProjectLink" in annot:
                continue
            action = annot.get("/A")
            if action and action.get("/S") == "/GoToR":
                target = file_spec_path(action.get("/F"))
                if not target:
                    continue

                # Relative targets name files beside the component PDF, not the
                # combined copy, which may be written to another folder.
                resolved_target = normalize_path(source_pdf.path.parent / target)
                if resolved_target not in included_pdfs:
                    continue

                destination = internal_destination(
                    writer,
                    included_pdfs[resolved_target],
                    action.get("/D"),
                )
                annot[NameObject("/A")] = DictionaryObject(
                    {
                        NameObject("/S"): NameObject("/GoTo"),
                        NameObject("/D"): destination,
                    }
                )
                rewrites += 1
                continue

            if action and action.get("/S") == "/GoTo" and isinstance(action.get("/D"), str):
                try:
                    action[NameObject("/D")] = internal_destination(writer, source_pdf, action.get("/D"))
                except MissingNamedDestination:
                    annot.pop("/A", None)
                    annot.pop("/AA", None)
                    annot.pop("/Dest", None)
                rewrites += 1
                continue

            if isinstance(annot.get("/Dest"), str):
                try:
                    annot[NameObject("/Dest")] = internal_destination(writer, source_pdf, annot.get("/Dest"))
                except MissingNamedDestination:
                    annot.pop("/Dest", None)
                    annot.pop("/A", None)
                    annot.pop("/AA", None)
                rewrites += 1

    if rewrites == 0 and outline_entries == 0 and not had_outlines:
        return 0, 0

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

    return rewrites, outline_entries


def rewrite_links(pdf_path: Path, included_pdfs: dict[str, IncludedPdf]) -> int:
    rewrites, _outline_entries = rewrite_combined_pdf(pdf_path, included_pdfs)
    return rewrites


def main() -> int:
    args = parse_args()
    pdf_path = args.pdf.resolve()
    project_pdfs = [path.resolve() for path in args.project_pdf]
    shared_pdfs = [path.resolve() for path in args.shared_pdf]

    included_pdfs = build_included_pdfs(project_pdfs, shared_pdfs)
    try:
        rewrites, outline_entries = rewrite_combined_pdf(pdf_path, included_pdfs)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Rewrote {rewrites} combined-PDF link destination(s) and restored "
        f"{outline_entries} outline entry/entries in {pdf_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
