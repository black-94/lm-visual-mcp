"""Server app tests: /health and /vision/analyze with a stubbed vision service."""

from __future__ import annotations

import json

from aiohttp.test_utils import TestClient, TestServer

from lm_visual_mcp.config import AppConfig
from lm_visual_mcp.server.app import VisionServerApp


class StubVision:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.router = type("R", (), {"providers": [type("P", (), {"name": "stub"})()]})()

    async def analyze_images(self, *, tool, image_sources, user_prompt, output_type=None):
        self.calls.append(
            {"tool": tool, "image_sources": image_sources, "user_prompt": user_prompt,
             "output_type": output_type}
        )
        return {"provider": "stub", "model": None, "result": {"answer": "ok"}, "meta": {}}


def make_app(cfg: AppConfig | None = None, vision: StubVision | None = None):
    cfg = cfg or AppConfig()
    vision = vision or StubVision()
    return VisionServerApp(cfg, vision=vision, adapters={}), vision


async def test_health_reports_hooks_and_providers():
    app, _ = make_app()
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        resp = await client.get("/health")
        assert resp.status == 200
        doc = await resp.json()
        assert doc["ok"] is True
        assert "image" in doc["hooks"] and "classifier" in doc["hooks"]
        assert doc["providers"] == ["stub"]
    finally:
        await client.close()


async def test_health_reports_disabled_hooks():
    cfg = AppConfig()
    cfg.server.image_hook.enabled = False
    app, _ = make_app(cfg)
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        doc = await (await client.get("/health")).json()
        assert doc["hooks"] == ["classifier"]
    finally:
        await client.close()


async def test_vision_analyze_endpoint():
    app, vision = make_app()
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        resp = await client.post(
            "/vision/analyze",
            json={"tool": "analyze_image", "image_sources": ["/tmp/a.png"], "user_prompt": "p"},
        )
        assert resp.status == 200
        doc = await resp.json()
        assert doc["provider"] == "stub"
        assert vision.calls[0]["tool"] == "analyze_image"
    finally:
        await client.close()


async def test_vision_analyze_rejects_bad_payload():
    app, _ = make_app()
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        resp = await client.post("/vision/analyze", data=b"not json")
        assert resp.status == 400
        resp = await client.post("/vision/analyze", json={"image_sources": "nope"})
        assert resp.status == 400
    finally:
        await client.close()


async def test_vision_analyze_multi_image_is_single_call():
    """Two images in one MCP request -> exactly one upstream vision call.

    The provider chain receives both images in a single batched request, never
    one call per image, so a multi-image tool (ui_diff_check) costs one pass.
    """
    app, vision = make_app()
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        resp = await client.post(
            "/vision/analyze",
            json={
                "tool": "ui_diff_check",
                "image_sources": ["/tmp/a.png", "/tmp/b.png"],
                "user_prompt": "diff",
            },
        )
        assert resp.status == 200
        assert len(vision.calls) == 1  # single upstream call for both images
        assert vision.calls[0]["tool"] == "ui_diff_check"
        assert vision.calls[0]["image_sources"] == ["/tmp/a.png", "/tmp/b.png"]
    finally:
        await client.close()


async def test_proxy_unknown_path_404():
    app, _ = make_app()
    client = TestClient(TestServer(app.build()))
    await client.start_server()
    try:
        resp = await client.post("/nonsense", data=b"{}")
        assert resp.status == 404
    finally:
        await client.close()
