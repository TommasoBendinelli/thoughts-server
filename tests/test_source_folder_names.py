# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Rule N (folder names) and Rule P (rewrite) against the shared test vectors.

tests/fixtures/source_folder_names_vectors.json is copied verbatim into the
desktop and Android repositories; all three implementations must pass it.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from thoughts_server import source_folder_names as names

VECTORS = json.loads((Path(__file__).parent / "fixtures/source_folder_names_vectors.json").read_text(encoding="utf-8"))


def _cases(group: str):
    for block in VECTORS[group]:
        for case in block["cases"]:
            yield pytest.param(block["map"], case, id=case.get("note", case["input"])[:60])


@pytest.mark.parametrize(("mapping", "case"), list(_cases("rule_p")))
def test_rule_p_vectors(mapping, case):
    assert names.rewrite_source_folder_names(case["input"], mapping) == case["output"]
    assert names.rewrite_source_folder_names_bytes(case["input"].encode(), mapping) == case["output"].encode()


@pytest.mark.parametrize(("mapping", "case"), list(_cases("rule_p_manifests")))
def test_rule_p_manifest_vectors(mapping, case):
    assert names.rewrite_source_folder_names(case["input"], mapping) == case["output"]
    assert names.rewrite_manifest_bytes(case["input"].encode(), mapping) == case["output"].encode()
    assert json.loads(case["output"])["id"] == json.loads(case["input"])["id"]


@pytest.mark.parametrize(("mapping", "case"), list(_cases("rule_p_values")))
def test_rule_p_value_vectors(mapping, case):
    assert names.rewrite_value(case["input"], mapping) == case["output"]
    assert names.count_value(case["output"], mapping) == 0
    assert names.count_value(case["input"], mapping) > 0


@pytest.mark.parametrize("case", VECTORS["slug"], ids=lambda case: case["name"][:40] or "empty")
def test_slug_vectors(case):
    assert names.slug(case["name"]) == case["slug"]


@pytest.mark.parametrize("case", VECTORS["folder_name"], ids=lambda case: case["old"])
def test_folder_name_vectors(case):
    result = names.folder_name(case["display"], case["old"], case["folder_id"], case["taken"])
    assert result == case["new"]
    assert re.fullmatch(VECTORS["valid_folder_regex"].strip("^$"), result)
    assert names.is_rule_n_name(result)


@pytest.mark.parametrize("case", VECTORS["compute_map"], ids=lambda case: case["note"][:40])
def test_compute_map_vectors(case):
    assert names.compute_map(case["folders"], case["taken"]) == case["expected"]
    # Deterministic whatever the input order.
    assert names.compute_map(list(reversed(case["folders"])), case["taken"]) == case["expected"]


@pytest.mark.parametrize("case", VECTORS["import_folder_name"], ids=lambda case: case["new"])
def test_import_folder_name_vectors(case):
    assert names.import_folder_name(case["display"], case["operation_id"], case["taken"]) == case["new"]


@pytest.mark.parametrize("case", VECTORS["is_rule_n_name"], ids=lambda case: case["name"])
def test_rule_n_shape_vectors(case):
    assert names.is_rule_n_name(case["name"]) is case["result"]


def test_spec_table_is_reproduced():
    """The expected map of the example Library in the spec, entry by entry."""
    [library] = [case for case in VECTORS["compute_map"] if "spec table" in case["note"]]
    assert {entry["old"]: entry["new"] for entry in names.compute_map(library["folders"], library["taken"])}[
        "docs-import-324e78cc0f1e2d3c4b5a69788796a5b4"] == "docs-open-benchmarks-grant-324e78"
    assert len(library["expected"]) == 24


def test_rewrite_is_a_bijection_on_the_map():
    mapping = VECTORS["rule_p"][0]["map"]
    for case in VECTORS["rule_p"][0]["cases"]:
        rewritten = names.rewrite_source_folder_names(case["input"], mapping)
        if rewritten == case["input"]:
            continue
        assert names.rewrite_source_folder_names(rewritten, names.inverse(mapping)) == case["input"]


def test_hex_exhaustion_is_an_error():
    digits = "0f1e2d3c4b5a49688776655443322110"
    taken = [f"docs-knowledge-{digits[:length]}" for length in names.HEX_LENGTHS]
    with pytest.raises(ValueError, match="No free source folder name"):
        names.import_folder_name("Knowledge", digits, taken)


def test_request_alias_keeps_deletions_free_text_and_filenames():
    from thoughts_server.source_folder_alias import alias_request_value
    pairs = [("docs-template", "docs-template-c8ee33")]
    request = {"mutations": [
        {"action": "delete", "path": "docs-template/a.tex", "operation_id": "1"},
        {"action": "upsert", "path": "docs-template/b.tex", "operation_id": "2"}],
        "comment": {"contents": "see docs-template/a.tex", "scope": {"source_file": "docs-template/a.tex"},
                    "filename": "docs-template.pdf", "note": "two\ndocs-template lines"}}
    assert alias_request_value(request, pairs) == {"mutations": [
        {"action": "delete", "path": "docs-template/a.tex", "operation_id": "1"},
        {"action": "upsert", "path": "docs-template-c8ee33/b.tex", "operation_id": "2"}],
        "comment": {"contents": "see docs-template/a.tex", "scope": {"source_file": "docs-template-c8ee33/a.tex"},
                    "filename": "docs-template.pdf", "note": "two\ndocs-template lines"}}


def test_build_stem_of_a_pinned_output():
    from thoughts_server.project_documents import build_stem
    assert build_stem("docs-template", "template_intro.pdf") == "intro"
    # After the rename the pinned output keeps its old prefix.
    assert build_stem("docs-template-c8ee33", "template_intro.pdf") == "template_intro"
    assert build_stem("docs-template-c8ee33", "template-c8ee33_new.pdf") == "new"


def test_manifest_alias_is_textual():
    manifest = {"schema_version": 5, "source_folders": [{"folder_id": "f", "path": "docs-template", "document_id": "d"}],
                "document_names": {"x": "docs-templates notes"}}
    raw = (json.dumps(manifest, indent=2) + "\n").encode()
    rewritten = names.rewrite_manifest_bytes(raw, [("docs-template", "docs-template-c8ee33")])
    assert rewritten == raw.replace(b'"docs-template"', b'"docs-template-c8ee33"')
    assert names.rewrite_manifest_bytes(raw, []) == raw
