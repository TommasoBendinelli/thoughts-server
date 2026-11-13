# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Component review data survives an assembly change without rescanning PDFs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from thoughts_server.renderer_tools.component_review import component_plans, compose_plans, package_components
from thoughts_server.sync_plans import component_package_dependencies, reader_source_dependencies


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_pdf(path: Path, width: float) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=720)
    with path.open("wb") as stream:
        writer.write(stream)


def assemble(path: Path, parts: list[Path]) -> None:
    writer = PdfWriter()
    for part in parts:
        writer.add_page(PdfReader(part).pages[0])
    with path.open("wb") as stream:
        writer.write(stream)


def project(parts: list[Path], combined: Path) -> tuple[dict, dict]:
    sources, segments = [], []
    for page, path in enumerate(parts, 1):
        entry = {
            "source_id": path.name,
            "source_pdf": path.name,
            "start_page": page,
            "page_count": 1,
            "fingerprint": sha(path),
        }
        sources.append(entry)
        segments.append(
            {key: entry[key] for key in ("source_id", "start_page", "page_count", "fingerprint")}
            | {"source_available": False}
        )
    return ({"pdf": {"pdf_sha256": sha(combined), "segments": segments}}, {"sources": sources})


def test_reordering_reuses_exact_component_review_plans(tmp_path, monkeypatch):
    from thoughts_server.renderer_tools import component_review
    a, b, combined = (tmp_path / name for name in ("a.pdf", "b.pdf", "combined.pdf"))
    write_pdf(a, 500)
    write_pdf(b, 600)
    objects: dict[str, bytes] = {}

    def store(raw: bytes) -> str:
        object_id = hashlib.sha256(raw).hexdigest()
        objects[object_id] = raw
        return object_id

    def read(object_id: str) -> bytes:
        return objects[object_id]

    assemble(combined, [a, b])
    manifest, layout = project([a, b], combined)
    first, refs = component_plans(combined, manifest, layout, tmp_path, tmp_path / "cache", read, store)
    assert len(refs) == 2
    assert first["pdf_sha256"] == sha(combined)
    assert len(first["cards"]) == 4
    keys = {
        ref["source_id"]: [card["key"] for card in first["cards"] if card["regions"][0]["page"] == page]
        for page, ref in enumerate(refs, 1)
    }
    object_count = len(objects)
    # Fedora uses a separate object store for every isolated build workspace.
    prior_objects = objects
    objects = {}

    class NoReanalysis:
        __code__ = component_review.build_plan.__code__

        def __call__(self, *_args):
            raise AssertionError("unchanged component was analyzed again")

    monkeypatch.setattr(component_review, "build_plan", NoReanalysis())

    assemble(combined, [b, a])
    manifest, layout = project([b, a], combined)
    second, reordered_refs = component_plans(
        combined, manifest, layout, tmp_path, tmp_path / "cache", read, store
    )
    assert len(prior_objects) == object_count
    assert len(objects) == object_count
    assert objects == prior_objects
    assert [ref["basic_review_object"] for ref in reordered_refs] == [
        ref["basic_review_object"] for ref in reversed(refs)
    ]
    assert [card["key"] for card in second["cards"]] == keys["b.pdf"] + keys["a.pdf"]
    assert [card["regions"][0]["page"] for card in second["cards"]] == [1, 1, 2, 2]


def test_changed_assembled_page_falls_back_to_direct_analysis(tmp_path):
    component, other, combined = (tmp_path / name for name in ("component.pdf", "other.pdf", "combined.pdf"))
    write_pdf(component, 500)
    write_pdf(other, 600)
    assemble(combined, [other])
    manifest, layout = project([component], combined)
    plan, refs = component_plans(
        combined, manifest, layout, tmp_path, tmp_path / "cache", lambda _: b"", lambda _: ""
    )
    assert refs == []
    assert plan["pdf_sha256"] == sha(combined)
    assert len(plan["cards"]) == 2


def test_source_anchor_regions_are_translated_without_changing_local_plan():
    local = {
        "blocks": [{"regions": [{"page": 1, "rect": [0, 0, 1, 1]}]}],
        "cards": [
            {
                "key": "stable",
                "regions": [{"page": 1, "rect": [0, 0, 1, 1]}],
                "source_anchor": {"segments": [{"regions": [{"page": 1, "rect": [0, 0, 1, 1]}]}]},
            }
        ],
    }
    result = compose_plans([(11, local)], "a" * 64)
    assert result["blocks"][0]["regions"][0]["page"] == 11
    assert result["cards"][0]["regions"][0]["page"] == 11
    assert result["cards"][0]["source_anchor"]["segments"][0]["regions"][0]["page"] == 11
    assert local["cards"][0]["regions"][0]["page"] == 1


def test_component_packages_preserve_local_comment_positions_after_reorder():
    objects: dict[str, bytes] = {}

    def store(raw: bytes) -> str:
        object_id = hashlib.sha256(raw).hexdigest()
        objects[object_id] = raw
        return object_id

    def manifest(first: int, combined_sha: str) -> tuple[dict, list[dict]]:
        plan_id = store(
            json.dumps({"schema_version": 1, "pdf_sha256": "c" * 64, "blocks": [], "cards": []}).encode()
        )
        segment = {
            "source_id": "a.tex",
            "start_page": first,
            "page_count": 1,
            "fingerprint": "c" * 64,
            "primary_source_file": "a.tex",
            "inputs": [{"source_file": "a.tex"}],
        }
        source = {"path": "a.tex", "sha256": "a" * 64, "object": "a" * 64}
        comment = {
            "id": "comment-one",
            "source_file": "a.tex",
            "contents": "A comment",
            "pdf_filenames": ["joined.pdf"],
            "locations": [
                {
                    "pdf_filename": "joined.pdf",
                    "pdf_sha256": combined_sha,
                    "page": first,
                    "rect": [0.1, 0.2, 0.3, 0.4],
                }
            ],
        }
        correspondence = {
            "kind": "comment",
            "item_id": "comment-one",
            "state": "exact_text",
            "locations": [
                {
                    "pdf_sha256": combined_sha,
                    "page": first,
                    "regions": [[0.1, 0.2, 0.3, 0.4]],
                    "proof": {"method": "tex_shipout_text"},
                }
            ],
        }
        value = {
            "filename": "joined.pdf",
            "pdf": {"pdf_sha256": combined_sha, "page_count": first, "segments": [segment]},
            "sources": [source],
            "resources": [],
            "review_comments": [comment],
            "review_correspondences": [correspondence],
        }
        refs = [
            {
                "source_id": "a.tex",
                "start_page": first,
                "page_count": 1,
                "component_pdf_sha256": "c" * 64,
                "basic_review_object": plan_id,
            }
        ]
        return value, refs

    first_manifest, first_refs = manifest(1, "d" * 64)
    first_index = package_components(first_manifest, first_refs, [], store)
    second_manifest, second_refs = manifest(3, "e" * 64)
    second_index = package_components(second_manifest, second_refs, [], store)
    assert first_index["documents"][0]["package_sha256"] == second_index["documents"][0]["package_sha256"]
    assert first_index["composition_object"] != second_index["composition_object"]
    package = json.loads(objects[first_index["documents"][0]["package_sha256"]])
    assert package["review_comments"][0]["locations"][0]["page"] == 1
    assert package["review_comments"][0]["locations"][0]["pdf_sha256"] == "c" * 64
    assert package["comment_placements"][0]["page"] == 1
    first_manifest["pdf"]["component_review"] = first_refs
    first_manifest["pdf"]["composition"] = first_index
    dependencies = component_package_dependencies(first_manifest, objects.__getitem__)
    assert {item.role for item in dependencies} == {
        "project_composition",
        "document_component_package",
        "component_review",
    }
    reader_manifest = {
        **first_manifest,
        "kind": "thoughts-document-source-artifact",
        "agent_proposals": [],
        "proposal_objects": [],
    }
    reader_manifest["pdf"]["filename"] = "joined.pdf"
    reader_manifest["pdf"]["project"] = "docs-joined"
    reader_manifest["pdf"]["segments"][0]["source_available"] = False
    assert not {item.role for item in reader_source_dependencies(reader_manifest, "joined.pdf")} & {
        "project_composition",
        "document_component_package",
        "component_review",
    }
    with_components = reader_source_dependencies(
        reader_manifest,
        "joined.pdf",
        include_component_packages=True,
        read_object=objects.__getitem__,
    )
    assert {item.role for item in with_components} >= {
        "project_composition",
        "document_component_package",
        "component_review",
    }


def test_pdf_only_comment_stays_with_component_after_reorder():
    objects: dict[str, bytes] = {}

    def store(raw: bytes) -> str:
        object_id = hashlib.sha256(raw).hexdigest()
        objects[object_id] = raw
        return object_id

    plan_id = store(
        json.dumps({"schema_version": 1, "pdf_sha256": "c" * 64, "blocks": [], "cards": []}).encode()
    )
    logical = {
        "comment_id": "pdf-note",
        "thread_root_id": "pdf-note",
        "contents": "Keep this note",
        "origin": {"pdf_sha256": "d" * 64, "page_index": 0},
    }

    def package(first: int, combined_sha: str) -> dict:
        segment = {
            "source_id": "imported.pdf",
            "start_page": first,
            "page_count": 1,
            "fingerprint": "c" * 64,
            "source_available": False,
        }
        manifest = {
            "filename": "joined.pdf",
            "pdf": {"pdf_sha256": combined_sha, "page_count": first, "segments": [segment]},
        }
        refs = [
            {
                "source_id": "imported.pdf",
                "start_page": first,
                "page_count": 1,
                "component_pdf_sha256": "c" * 64,
                "basic_review_object": plan_id,
            }
        ]
        annotation = {"id": "pdf-note", "page": first, "contents": "Keep this note", "rect": [10, 20, 30, 40]}
        index = package_components(manifest, refs, [logical], store, [annotation])
        return json.loads(objects[index["documents"][0]["package_sha256"]])

    first = package(1, "d" * 64)
    moved = package(3, "e" * 64)
    assert first == moved
    assert moved["logical_comments"][0]["comment_id"] == "pdf-note"
    assert moved["pdf_comments"][0]["page"] == 1
    assert moved["comment_placements"][0]["page"] == 1



def test_page_proof_checks_rendering_resources_and_writer_normalization(tmp_path, monkeypatch):
    from thoughts_server.renderer_tools.component_review import _same_pages, _same_resources
    from pypdf.generic import DictionaryObject, NameObject, FloatObject, DecodedStreamObject
    p = tmp_path / "a.pdf"
    write_pdf(p, 500)
    component, combined = PdfReader(p), PdfReader(p)
    monkeypatch.setattr(type(combined.pages[0]), "extract_text", lambda page: (_ for _ in ()).throw(AssertionError("text extraction is unnecessary")))
    assert _same_pages(combined, component, 1, 1)
    # Identical operators with changed resources must not reuse old placement.
    original = DictionaryObject({NameObject("/Size"): FloatObject(398.9258151)})
    normalized = DictionaryObject({NameObject("/Size"): FloatObject(str(original["/Size"]))})
    assert _same_resources(normalized, original, set())
    normalized[NameObject("/Size")] = FloatObject(398.9259)
    assert not _same_resources(normalized, original, set())
    combined.pages[0][NameObject("/Resources")] = normalized
    component.pages[0][NameObject("/Resources")] = original
    assert not _same_pages(combined, component, 1, 1)
    left, right = DecodedStreamObject(), DecodedStreamObject()
    left.set_data(b"image-before"); right.set_data(b"image-after")
    assert not _same_resources(left, right, set())


def test_resource_proof_handles_cycles_without_accepting_changed_descendants():
    from thoughts_server.renderer_tools.component_review import _same_resources
    from pypdf.generic import DictionaryObject, NameObject, NumberObject
    left, right = DictionaryObject(), DictionaryObject()
    left[NameObject("/Self")] = left
    right[NameObject("/Self")] = right
    left[NameObject("/Value")] = NumberObject(1)
    right[NameObject("/Value")] = NumberObject(1)
    assert _same_resources(left, right, set())
    right[NameObject("/Value")] = NumberObject(2)
    assert not _same_resources(left, right, set())


def test_identical_encoded_streams_are_not_decompressed(monkeypatch):
    from thoughts_server.renderer_tools.component_review import _same_resources
    from pypdf.generic import EncodedStreamObject, NameObject
    left, right = EncodedStreamObject(), EncodedStreamObject()
    for stream in (left, right):
        stream._data = b"same compressed image"
        stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    monkeypatch.setattr(EncodedStreamObject, "get_data", lambda self: (_ for _ in ()).throw(AssertionError("should not decode")))
    assert _same_resources(left, right, set())
