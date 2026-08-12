"""Singleton tests: concurrency queueing, daemon /health probe, proxy forwarding.

Covers the connect-or-spawn single-instance design:

- ``VisionSession`` enforces ``runtime.max_concurrency`` via a semaphore so
  requests beyond the limit queue.
- A shared ``ToolServer`` answers ``GET /health`` (probe target).
- ``ProxyVisionSession`` forwards tool calls over HTTP to the daemon and
  returns its envelope.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from types import SimpleNamespace

from PIL import Image

from lm_visual_mcp.config import AppConfig
from lm_visual_mcp.models import ProviderUsage
from lm_visual_mcp.services.control import ToolServer
from lm_visual_mcp.services.proxy import ProxyVisionSession, probe_primary
from lm_visual_mcp.tools import VisionSession


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _png(path) -> str:
    Image.new("RGB", (4, 4), "white").save(path)
    return str(path)


class _CountingRouter:
    """Records the max number of concurrently in-flight route() calls."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0

    async def route(self, request):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.05)
        self.in_flight -= 1
        return SimpleNamespace(
            provider="fake",
            model="m",
            result={"answer": "x"},
            usage=ProviderUsage().to_dict(),
            fallbacks=[],
            duration_ms=1.0,
        )


async def test_max_concurrency_queues_serial(tmp_path):
    cfg = AppConfig.model_validate({"runtime": {"max_concurrency": 1}})
    router = _CountingRouter()
    img = _png(tmp_path / "t.png")
    vs = VisionSession(cfg, router=router)
    await asyncio.gather(
        *(vs.analyze_images(tool="analyze_image", image_sources=[img], user_prompt="p")
          for _ in range(4))
    )
    assert router.max_in_flight == 1  # strictly serial -> everything queued


async def test_max_concurrency_allows_up_to_limit(tmp_path):
    cfg = AppConfig.model_validate({"runtime": {"max_concurrency": 2}})
    router = _CountingRouter()
    img = _png(tmp_path / "t.png")
    vs = VisionSession(cfg, router=router)
    await asyncio.gather(
        *(vs.analyze_images(tool="analyze_image", image_sources=[img], user_prompt="p")
          for _ in range(6))
    )
    assert 1 < router.max_in_flight <= 2


def test_probe_primary_health_and_absent():
    port = _free_port()
    cfg = AppConfig.model_validate({"runtime": {"host": "127.0.0.1", "port": port}})
    ts = ToolServer(cfg, "127.0.0.1", port, 60000)
    ts.bind()
    threading.Thread(target=ts.serve, daemon=True).start()
    try:
        deadline = time.monotonic() + 5
        while not probe_primary("127.0.0.1", port) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert probe_primary("127.0.0.1", port)
        assert not probe_primary("127.0.0.1", port + 1000, 0.2)
    finally:
        ts.stop()


async def test_proxy_forwards_to_daemon(tmp_path):
    port = _free_port()
    cfg = AppConfig.model_validate({"runtime": {"host": "127.0.0.1", "port": port, "timeout": 30}})

    class FakeRouter:
        async def route(self, request):
            return SimpleNamespace(
                provider="fake",
                model="m",
                result={"answer": "HELLO"},
                usage=ProviderUsage().to_dict(),
                fallbacks=[],
                duration_ms=1.0,
            )

    ts = ToolServer(
        cfg, "127.0.0.1", port, 60000,
        session_factory=lambda c: VisionSession(c, router=FakeRouter()),
    )
    ts.bind()
    threading.Thread(target=ts.serve, daemon=True).start()
    try:
        for _ in range(100):
            if probe_primary("127.0.0.1", port):
                break
            await asyncio.sleep(0.05)
        proxy = ProxyVisionSession(cfg, "127.0.0.1", port)
        img = _png(tmp_path / "p.png")
        out = await proxy.analyze_images(
            tool="analyze_image", image_sources=[img], user_prompt="describe"
        )
        assert out["provider"] == "fake"
        assert out["result"]["answer"] == "HELLO"
    finally:
        ts.stop()