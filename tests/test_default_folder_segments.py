# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Default-folder rename (``documents`` -> ``default``): Rule R, both-segment parsing,
output-name invariance, the Library segment and the request alias."""
from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from thoughts_server import default_folder_segment as segments
from thoughts_server import project_configuration as pc
from thoughts_server.source_owner import default_folder_output, is_default_folder, owner_of, source_folders
from thoughts_server.uniform_manifest import managed_pdf_path

H = "0123456789abcdef" * 4
LEGACY = f".thoughts/projects/{H}/documents"
CANONICAL = f".thoughts/projects/{H}/default"


# ------------------------------------------------------------------------ Rule R


@pytest.mark.parametrize("text, expected", [
    (LEGACY, CANONICAL),
    (LEGACY + "/notes/a.md", CANONICAL + "/notes/a.md"),
    ("../" + LEGACY + "/paper.pdf", "../" + CANONICAL + "/paper.pdf"),
    ("/Users/me/docs/" + LEGACY + "/x.pdf", "/Users/me/docs/" + CANONICAL + "/x.pdf"),
    ('{"path": "' + LEGACY + '"}', '{"path": "' + CANONICAL + '"}'),
    ("'" + LEGACY + "'", "'" + CANONICAL + "'"),
    ("C:\\docs\\" + LEGACY + "\\x", "C:\\docs\\" + CANONICAL + "\\x"),
    (LEGACY + " and " + LEGACY + "\n", CANONICAL + " and " + CANONICAL + "\n"),
    (LEGACY + "\t", CANONICAL + "\t"),
])
def test_rule_r_rewrites_every_legacy_spelling(text, expected):
    assert segments.rewrite_legacy_default_folder(text) == expected
    assert segments.rewrite_legacy_default_folder_bytes(text.encode()) == expected.encode()
    # Idempotent, and the inverse recovers the original.
    assert segments.rewrite_legacy_default_folder(expected) == expected
    assert segments.legacy_spelling(expected) == text


@pytest.mark.parametrize("text", [
    LEGACY + "2/a.md",
    LEGACY + ".pdf",
    LEGACY + "-old",
    f".thoughts/projects/{H[:63]}/documents/a.md",
    f".thoughts/projects/{H}a/documents/a.md",
    f".thoughts/projects/{H.upper()}/documents/a.md",
    "docs-manual/documents/a.md",
    "documents/a.md",
    f".thoughts/projects/{H}/other/documents/a.md",
    LEGACY.replace("/", "\\/"),  # a JSON-escaped slash is never written by Thoughts
])
def test_rule_r_has_no_false_positives(text):
    assert segments.rewrite_legacy_default_folder(text) == text
    assert segments.count_legacy(text) == 0


def test_json_written_by_thoughts_does_not_escape_slashes():
    encoded = json.dumps({"path": LEGACY + "/a.md"}, ensure_ascii=False, indent=2)
    assert "\\/" not in encoded
    assert json.loads(segments.rewrite_legacy_default_folder(encoded)) == {"path": CANONICAL + "/a.md"}


def test_rule_r_on_structures_rewrites_keys_and_values():
    value = {LEGACY + "/a.md": [LEGACY, {"x": LEGACY + "/b"}, 3, None], "n": "documents"}
    assert segments.count_legacy(value) == 3
    assert segments.rewrite_value(value) == {
        CANONICAL + "/a.md": [CANONICAL, {"x": CANONICAL + "/b"}, 3, None], "n": "documents"}
    assert segments.legacy_value(segments.rewrite_value(value)) == value
    assert segments.same_location(LEGACY + "/a", CANONICAL + "/a")
    assert not segments.same_location(LEGACY + "/a", CANONICAL + "/b")


# ------------------------------------------------------------- both segments


@pytest.mark.parametrize("folder", [LEGACY, CANONICAL])
def test_parsers_accept_both_segments(folder):
    assert owner_of(folder + "/notes/a.md") == folder
    assert is_default_folder(folder)
    assert pc.is_manifest_path(folder + "/thoughts-project.json")
    assert managed_pdf_path(folder + "/paper.pdf")
    assert not pc.is_manifest_path(folder.replace(H, H[:63]) + "/thoughts-project.json")
    assert owner_of(f".thoughts/projects/{H}/defaults/a.md") is None
    assert len(folder) == (93 if folder == LEGACY else 91)


def test_output_names_never_change():
    for source in ("notes/a.md", "paper.tex"):
        assert default_folder_output(LEGACY, source) == default_folder_output(CANONICAL, source)
        assert default_folder_output(CANONICAL, source) == (
            "document_" + hashlib.sha256(f"{LEGACY}/{source}".encode()).hexdigest()[:20] + ".pdf")


def project_value(key: str, segment: str) -> dict:
    default = str(uuid.uuid4())
    return {"schema_version": 5, "kind": "thoughts-project", "id": key, "name": key,
            "default_folder_id": default,
            "source_folders": [{"folder_id": default, "path": pc.default_folder(key, segment),
                                "document_id": str(uuid.uuid5(uuid.NAMESPACE_URL, key))}],
            "documents": [{"document_id": str(uuid.uuid4()), "format": "markdown", "included": True,
                           "source": {"folder_id": default, "path": "notes.md"}}]}


@pytest.mark.parametrize("segment", segments.SEGMENTS)
def test_manifest_validation_accepts_both_segments_in_their_own_folder(tmp_path, segment):
    value = pc.validate(project_value("reading", segment))
    path = pc.save(tmp_path, value)
    assert path == tmp_path / pc.manifest_path("reading", segment)
    assert pc.read(path) == value
    assert pc._parsed(pc.manifest_path("reading", segment), path.read_bytes()) == value
    other = segments.LEGACY if segment == segments.CANONICAL else segments.CANONICAL
    # The manifest must live inside the folder it declares.
    assert pc._parsed(pc.manifest_path("reading", other), path.read_bytes()) is None
    wrong = tmp_path / pc.manifest_path("reading", other)
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(path.read_bytes())
    pc._READ_CACHE.clear()
    with pytest.raises(ValueError, match="outside its default folder"):
        pc.read(wrong)
    foreign = project_value("reading", segment)
    foreign["source_folders"][0]["path"] = pc.default_folder("other", segment)
    with pytest.raises(ValueError, match="another Project"):
        pc.validate(foreign)


def test_disk_discovery_prefers_default_over_documents(tmp_path):
    legacy_only = pc.save(tmp_path, project_value("a", segments.LEGACY))
    canonical = pc.save(tmp_path, project_value("b", segments.CANONICAL))
    assert pc.manifest_files(tmp_path) == sorted([legacy_only, canonical])
    leftover = tmp_path / pc.manifest_path("b", segments.LEGACY)
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(canonical.read_bytes())
    assert canonical in pc.manifest_files(tmp_path) and leftover not in pc.manifest_files(tmp_path)
    folders = [path.relative_to(tmp_path).as_posix() for path in source_folders(tmp_path)]
    assert pc.default_folder("b", segments.CANONICAL) in folders
    assert pc.default_folder("b", segments.LEGACY) not in folders


def test_created_folders_follow_the_library_segment():
    assert segments.current() == segments.LEGACY
    assert pc.default_folder("x").endswith("/documents")
    segments.set_current(segments.CANONICAL)
    assert pc.default_folder("x").endswith("/default")
    assert pc.from_legacy({"id": "x", "name": "X", "documents": []}, {})["source_folders"][0]["path"] \
        == pc.default_folder("x", segments.CANONICAL)
    assert pc.default_folders("x") == (pc.default_folder("x", "default"), pc.default_folder("x", "documents"))



def test_alias_is_inactive_before_the_migration():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from thoughts_server.default_folder_alias import DefaultFolderAliasMiddleware

    async def echo(request):
        body = await request.body()
        return JSONResponse({"path": request.url.path, "query": request.url.query,
                             "body": body.decode(), "length": request.headers.get("content-length")})

    app = DefaultFolderAliasMiddleware(Starlette(routes=[Route("/{rest:path}", echo, methods=["GET", "POST"])]))
    client = TestClient(app)
    payload = json.dumps({"path": LEGACY + "/a.md"})
    assert client.post("/x", content=payload, headers={"content-type": "application/json"}).json()["body"] == payload
    segments.set_current(segments.CANONICAL)
    echoed = client.post(f"/v1/sources/{LEGACY}/a.md?path={LEGACY}/b.md", content=payload,
                         headers={"content-type": "application/json"}).json()
    assert echoed["path"] == f"/v1/sources/{CANONICAL}/a.md"
    assert CANONICAL.replace("/", "%2F") in echoed["query"]
    assert json.loads(echoed["body"]) == {"path": CANONICAL + "/a.md"}
    assert int(echoed["length"]) == len(echoed["body"].encode())
    # Uploaded object bytes are content-addressed and never rewritten.
    raw = client.post("/x", content=payload, headers={"content-type": "application/octet-stream"}).json()
    assert raw["body"] == payload
