"""Z.AI Vision MCP tool-schema compatibility tests.

Verifies the 8 primary tools + aliases exist and that their schemas never leak
server-policy fields (provider/model/api_key/workdir/timeout/fallback).
"""

from __future__ import annotations

from vision_mcp.config import AppConfig
from vision_mcp.server import build_server

FORBIDDEN = {"provider", "model", "api_key", "workdir", "timeout", "fallback",
             "provider_models", "credential", "token", "cookie"}

EXPECTED_TOOLS = {
    "ui_to_artifact",
    "extract_text_from_screenshot",
    "diagnose_error_screenshot",
    "understand_technical_diagram",
    "analyze_data_visualization",
    "ui_diff_check",
    "analyze_image",
    "analyze_video",
    "image_analysis",
    "video_analysis",
}


def _mcp():
    return build_server(AppConfig())


async def _tools():
    mcp = _mcp()
    return await mcp.list_tools()


async def test_all_tools_registered():
    names = {t.name for t in await _tools()}
    assert EXPECTED_TOOLS <= names


async def test_no_server_policy_fields_in_schemas():
    for tool in await _tools():
        schema = tool.inputSchema
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        for field in properties:
            assert field not in FORBIDDEN, f"{tool.name} leaks server field {field!r}"


async def test_ui_to_artifact_schema():
    for tool in await _tools():
        if tool.name != "ui_to_artifact":
            continue
        props = tool.inputSchema["properties"]
        assert {"image_source", "output_type", "prompt"} <= set(props)
        required = tool.inputSchema.get("required", [])
        assert {"image_source", "output_type", "prompt"} <= set(required)


async def test_extract_text_schema():
    for tool in await _tools():
        if tool.name != "extract_text_from_screenshot":
            continue
        props = tool.inputSchema["properties"]
        assert {"image_source", "prompt"} <= set(props)
        assert "programming_language" in props


async def test_ui_diff_check_schema():
    for tool in await _tools():
        if tool.name != "ui_diff_check":
            continue
        props = tool.inputSchema["properties"]
        assert {"expected_image_source", "actual_image_source", "prompt"} <= set(props)