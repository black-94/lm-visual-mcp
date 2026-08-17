"""MCP layer tests: tool registration matches tool_names; calls reach the client."""

from __future__ import annotations

from lm_visual_mcp.mcp.client import RemoteVision
from lm_visual_mcp.mcp.server import build_mcp
from lm_visual_mcp.tool_names import TOOL_NAMES


class RecordingVision:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def analyze_images(self, *, tool, image_sources, user_prompt, output_type=None):
        self.calls.append({"tool": tool, "image_sources": image_sources})
        return {"provider": "p", "result": {"answer": tool}, "meta": {}}


async def test_registered_tools_match_declaration():
    mcp = build_mcp(RecordingVision())
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == set(TOOL_NAMES)
    # Video is no longer declared.
    assert "analyze_video" not in names and "video_analysis" not in names


async def test_tool_call_forwarded_to_client():
    vision = RecordingVision()
    mcp = build_mcp(vision)
    result = await mcp.call_tool("analyze_image", {"image_source": "/tmp/x.png", "prompt": "hi"})
    if not isinstance(result, dict):
        result = getattr(result, "structuredContent", None) or result
    assert vision.calls[0]["tool"] == "analyze_image"
    if isinstance(result, dict):
        assert result.get("result", {}).get("answer") == "analyze_image"


async def test_unreachable_server_returns_hint_envelope():
    class Unreachable(RemoteVision):
        def __init__(self):  # no server; keep base url only
            self.base = "http://127.0.0.1:1"
            self.timeout = 0.2

    vision = Unreachable()
    out = await vision.analyze_images(
        tool="analyze_image", image_sources=["/tmp/x.png"], user_prompt="p"
    )
    assert out["error"]
    assert "lm-visual-mcp start" in out["error"]
