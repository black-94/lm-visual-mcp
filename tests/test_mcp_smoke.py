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


#: Per-tool argument sets; each maps 1:1 to a tool in TOOL_NAMES. Two sources
#: exercise the multi-image tool (ui_diff_check); the rest are single-image.
TOOL_ARGS = {
    "ui_to_artifact": {"image_source": "/x.png", "output_type": "html", "prompt": "p"},
    "extract_text_from_screenshot": {"image_source": "/x.png", "prompt": "p"},
    "diagnose_error_screenshot": {"image_source": "/x.png", "prompt": "p"},
    "understand_technical_diagram": {"image_source": "/x.png", "prompt": "p"},
    "analyze_data_visualization": {"image_source": "/x.png", "prompt": "p"},
    "ui_diff_check": {"expected_image_source": "/a.png", "actual_image_source": "/b.png", "prompt": "p"},
    "analyze_image": {"image_source": "/x.png", "prompt": "p"},
    "image_analysis": {"image_source": "/x.png", "prompt": "p"},
}


async def test_every_registered_tool_is_callable():
    """All eight MCP tools are invokable end-to-end, not just registered."""
    assert set(TOOL_ARGS) == set(TOOL_NAMES)  # the coverage map stays in step
    vision = RecordingVision()
    mcp = build_mcp(vision)
    for name in TOOL_NAMES:
        before = len(vision.calls)
        await mcp.call_tool(name, TOOL_ARGS[name])
        # The call reached the client and was routed under this tool's own name.
        assert len(vision.calls) == before + 1
        assert vision.calls[-1]["tool"] == name


async def test_different_tools_do_not_reuse_same_result():
    """Same image through two tools yields two distinct upstream results.

    Guards against a cache keyed on image alone leaking one tool's answer into
    another: analyze_image then ui_to_artifact must each reach the client, not
    a shared/deduped entry.
    """
    vision = RecordingVision()
    mcp = build_mcp(vision)
    await mcp.call_tool("analyze_image", {"image_source": "/shared.png", "prompt": "what is this"})
    await mcp.call_tool(
        "ui_to_artifact",
        {"image_source": "/shared.png", "output_type": "spec", "prompt": "what is this"},
    )
    assert len(vision.calls) == 2
    assert vision.calls[0]["tool"] == "analyze_image"
    assert vision.calls[1]["tool"] == "ui_to_artifact"
    assert len(vision.calls[0]["image_sources"]) == len(vision.calls[1]["image_sources"]) == 1
    # RecordingVision answers with the tool name: distinct tools => distinct results.
    assert vision.calls[0]["tool"] != vision.calls[1]["tool"]


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
