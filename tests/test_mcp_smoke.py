"""MCP smoke tests: tools/list surfaces all tools; tools/call completes E2E.

Uses a fake VisionSession so no real provider/network is needed.
"""

from __future__ import annotations

from vision_mcp.config import AppConfig
from vision_mcp.server import build_server


class FakeSession:
    async def analyze_images(self, *, tool, image_sources, user_prompt, output_type=None):
        return {
            "provider": "codex",
            "model": "gpt-x",
            "result": {"summary": "s", "answer": "ok", "observations": [],
                       "texts": [], "elements": [], "warnings": []},
            "meta": {"duration_ms": 1.0, "fallbacks": [], "usage": {}},
        }

    async def analyze_video(self, *, tool, video_source, user_prompt):
        return {
            "provider": "codex",
            "model": "gpt-x",
            "result": {"summary": "s", "answer": "video-ok", "observations": [],
                       "texts": [], "elements": [], "warnings": []},
            "meta": {"duration_ms": 1.0, "fallbacks": [], "usage": {}},
        }


def _mcp():
    return build_server(AppConfig(), session=FakeSession())


async def test_tools_list_contains_all():
    mcp = _mcp()
    names = {t.name for t in await mcp.list_tools()}
    for expected in (
        "ui_to_artifact", "extract_text_from_screenshot", "diagnose_error_screenshot",
        "understand_technical_diagram", "analyze_data_visualization", "ui_diff_check",
        "analyze_image", "analyze_video", "image_analysis", "video_analysis",
    ):
        assert expected in names


async def test_tools_call_analyze_image_e2e():
    mcp = _mcp()
    result = await mcp.call_tool("analyze_image", {"image_source": "/tmp/x.png", "prompt": "look"})
    text = result[0].text if hasattr(result[0], "text") else str(result[0])
    import json
    payload = json.loads(text)
    assert payload["provider"] == "codex"
    assert payload["result"]["answer"] == "ok"


async def test_tools_call_ui_diff_e2e():
    mcp = _mcp()
    result = await mcp.call_tool(
        "ui_diff_check",
        {"expected_image_source": "/tmp/a.png", "actual_image_source": "/tmp/b.png",
         "prompt": "compare"},
    )
    assert result


async def test_tools_call_video_e2e():
    mcp = _mcp()
    result = await mcp.call_tool("analyze_video", {"video_source": "/tmp/v.mp4", "prompt": "watch"})
    text = result[0].text if hasattr(result[0], "text") else str(result[0])
    import json
    payload = json.loads(text)
    assert payload["result"]["answer"] == "video-ok"