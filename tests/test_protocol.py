# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2026 Tommaso Bendinelli
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from thoughts_server.api import create_app
from thoughts_server.schemas import BuildInputSnapshotRequest, OperationBatch, SourceApplyRequest

ROOT = Path(__file__).resolve().parents[1]


def test_protocol_fixtures_match_runtime_models():
    OperationBatch.model_validate_json((ROOT / "protocol/fixtures/operation-batch.json").read_text())
    SourceApplyRequest.model_validate_json((ROOT / "protocol/fixtures/source-apply.json").read_text())


@pytest.mark.parametrize(
    "project",
    ["docs-manual/../outside", "docs-manual/child", r"docs-manual\child"],
)
def test_build_input_project_is_one_safe_workspace_component(project):
    with pytest.raises(ValueError, match="invalid project"):
        BuildInputSnapshotRequest(
            target="manual.pdf",
            project=project,
            entries=[],
        )


def test_checked_in_openapi_covers_runtime_http_routes(settings):
    checked_in = yaml.safe_load((ROOT / "protocol/openapi.yaml").read_text())
    generated = create_app(settings).openapi()
    methods = {"get", "put", "post", "delete", "patch", "head", "options", "trace"}
    checked_routes = {
        (path, method)
        for path, path_item in checked_in["paths"].items()
        for method in path_item
        if method in methods
    }
    generated_routes = {
        (path, method)
        for path, path_item in generated["paths"].items()
        for method in path_item
        if method in methods
    }
    assert checked_routes == generated_routes


def test_checked_in_openapi_documents_trace_contract():
    checked_in = yaml.safe_load((ROOT / "protocol/openapi.yaml").read_text())
    assert "X-Thoughts-Trace-Id" in checked_in["info"]["description"]
    assert checked_in["components"]["headers"]["XThoughtsTraceId"]["schema"] == {
        "type": "string",
        "format": "uuid",
    }
    for schema_name in ("BuildRecord", "LibraryEntry", "SourcePublicationResult"):
        trace_id = checked_in["components"]["schemas"][schema_name]["properties"]["trace_id"]
        assert {"type": "string", "format": "uuid"} in trace_id["anyOf"]


def test_checked_in_openapi_exposes_only_build_input_schema_three():
    checked_in = yaml.safe_load((ROOT / "protocol/openapi.yaml").read_text())
    schemas = checked_in["components"]["schemas"]
    for schema_name in ("BuildInputSnapshotRequest", "BuildInputSnapshotRecord"):
        assert schemas[schema_name]["properties"]["schema_version"] == {
            "type": "integer",
            "const": 3,
            "title": "Schema Version",
            "default": 3,
        }


def test_checked_in_openapi_exposes_server_storage_usage():
    checked_in = yaml.safe_load((ROOT / "protocol/openapi.yaml").read_text())
    schemas = checked_in["components"]["schemas"]
    storage = schemas["ServerStorageUsage"]
    assert storage["required"] == ["total_bytes", "used_bytes", "available_bytes"]
    assert schemas["HealthResponse"]["properties"]["storage"] == {
        "$ref": "#/components/schemas/ServerStorageUsage"
    }
    assert schemas["RenderStateRecord"]["properties"]["storage"] == {
        "$ref": "#/components/schemas/ServerStorageUsage"
    }
    assert schemas["RenderStateRecord"]["properties"]["server_summary"] == {
        "$ref": "#/components/schemas/ServerSummary"
    }
    assert schemas["ServerSummary"]["properties"]["backup"] == {
        "$ref": "#/components/schemas/ServerMaintenanceSummary"
    }
    assert schemas["ServerSummary"]["properties"]["integrity_check"] == {
        "$ref": "#/components/schemas/ServerMaintenanceSummary"
    }
