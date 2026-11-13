# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
"""Stored bytes are served as data, the API documentation can be off, and errors keep server internals."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from thoughts_server import api
from thoughts_server.api import create_app
from thoughts_server.config import Settings


def upload(client, headers, content: bytes, media_type: str) -> str:
    digest = hashlib.sha256(content).hexdigest()
    response = client.post("/v1/objects", content=content,
                           headers={**headers, "Content-Type": media_type, "X-Content-SHA256": digest})
    assert response.status_code == 200, response.text
    return digest


@pytest.mark.parametrize("media_type,content", [
    ("text/html", b"<script>alert(1)</script>"),
    ("application/xhtml+xml", b"<html xmlns='http://www.w3.org/1999/xhtml'><script>alert(1)</script></html>"),
    ("image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"),
    ("text/xml", b"<?xml version='1.0'?><x/>"),
    ("application/pdf", b"%PDF-1.4"),
    ("text/plain", b"<script>alert(1)</script>"),
    ("application/octet-stream", b"<html><script>alert(1)</script></html>"),
])
def test_stored_objects_are_served_as_inert_data_whatever_type_they_claim(client, auth_headers, media_type,
                                                                          content):
    digest = upload(client, auth_headers, content, media_type)

    served = client.get(f"/v1/objects/{digest}", headers=auth_headers)

    assert served.status_code == 200 and served.content == content
    assert served.headers["x-content-type-options"] == "nosniff"
    policy = served.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "sandbox" in policy.split("; ")
    assert "script-src" not in policy and "allow-scripts" not in policy


def test_every_response_forbids_type_sniffing(client, auth_headers):
    responses = [
        client.get("/v1/health"),
        client.get("/v2/library"),  # refused before the application
        client.get("/v2/library", headers=auth_headers),
        client.get("/v1/objects/" + "0" * 64, headers=auth_headers),  # 404
        client.get("/v2/artifacts/" + "0" * 64, headers=auth_headers),  # 428: no transfer plan
    ]
    assert [response.status_code for response in responses] == [200, 401, 200, 404, 428]
    assert all(response.headers["x-content-type-options"] == "nosniff" for response in responses)


def test_artifacts_are_served_with_the_same_headers_as_objects():
    # A transfer plan is needed to fetch an artifact; both routes build their headers in one place.
    source = Path(api.__file__).read_text(encoding="utf-8")
    assert source.count("headers=stored_content_headers(record.sha256)") == 2
    assert not re.search(r'headers=\{"ETag": f\'"\{record\.sha256\}"\'\}', source)


def test_the_api_documentation_can_be_turned_off(settings, monkeypatch, tmp_path):
    pages = ("/docs", "/redoc", "/openapi.json")
    with TestClient(create_app(settings)) as shown:
        assert [shown.get(path).status_code for path in pages] == [200, 200, 200]
    with TestClient(create_app(replace(settings, api_docs_enabled=False))) as hidden:
        assert [hidden.get(path).status_code for path in pages] == [404, 404, 404]
        assert hidden.get("/v1/health").status_code == 200

    monkeypatch.setenv("THOUGHTS_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("THOUGHTS_API_DOCS", raising=False)
    assert Settings.from_env().api_docs_enabled is True
    monkeypatch.setenv("THOUGHTS_API_DOCS", "false")
    assert Settings.from_env().api_docs_enabled is False
    example = Path(__file__).resolve().parents[1] / "deploy/fedora/server.env.example"
    assert "\nTHOUGHTS_API_DOCS=false\n" in example.read_text(encoding="utf-8")


CAUSES = {
    "database": IntegrityError("INSERT INTO devices (id, token_hash) VALUES (?, ?)", ("phone", "ab12"),
                               Exception("UNIQUE constraint failed: devices.id")),
    "filesystem": PermissionError(13, "Permission denied", "/srv/thoughts/state/private/file"),
    "rule": ValueError("operation ID was reused with different source content"),
    "said-by-the-server": FileNotFoundError("standalone input bundle is not uploaded"),
}


@pytest.fixture
def failing_client(client):
    def fail(kind: str):
        try:
            raise CAUSES[kind]
        except Exception as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    client.app.add_api_route("/v1/test-failure/{kind}", fail)
    return client


def test_database_and_filesystem_error_text_goes_to_the_log_not_the_client(failing_client, auth_headers,
                                                                           caplog):
    with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
        database = failing_client.get("/v1/test-failure/database", headers=auth_headers)
        filesystem = failing_client.get("/v1/test-failure/filesystem", headers=auth_headers)

    for response in (database, filesystem):
        assert response.status_code == 409
        assert response.json() == {
            "detail": "The server could not apply this request; the reason is in the server log."}
    assert "devices" not in database.text and "UNIQUE" not in database.text and "INSERT" not in database.text
    assert "/srv/thoughts" not in filesystem.text and "Permission" not in filesystem.text
    logged = [json.loads(record.getMessage()) for record in caplog.records
              if "internal_error_text_withheld" in record.getMessage()]
    assert [entry["route"] for entry in logged] == [
        "/v1/test-failure/database", "/v1/test-failure/filesystem"]
    assert "UNIQUE constraint failed: devices.id" in logged[0]["error"]
    assert "/srv/thoughts/state/private/file" in logged[1]["error"]


def test_the_servers_own_reasons_still_reach_the_client(failing_client, auth_headers):
    rule = failing_client.get("/v1/test-failure/rule", headers=auth_headers)
    said = failing_client.get("/v1/test-failure/said-by-the-server", headers=auth_headers)

    assert rule.status_code == 409 and said.status_code == 409
    assert rule.json() == {"detail": "operation ID was reused with different source content"}
    assert said.json() == {"detail": "standalone input bundle is not uploaded"}


def test_every_route_that_answers_with_an_errors_text_names_its_cause():
    # The handler recognises a database or filesystem error by the exception's cause.
    source = Path(api.__file__).read_text(encoding="utf-8")
    raises = [line.strip() for line in source.splitlines()
              if "raise HTTPException(" in line and "detail=str(error)" in line]
    assert len(raises) > 20
    assert all(line.endswith("from error") for line in raises)
