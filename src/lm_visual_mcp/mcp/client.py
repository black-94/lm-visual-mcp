"""MCP thin client: every tool call is forwarded to the shared server.

The MCP process never instantiates a local :class:`VisionService` - the server
is the single owner of the provider chain, its rate limits and the
concurrency gate, so limiting stays centralized no matter how many MCP
sessions are running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Optional

from ..config import AppConfig
from ..server.lifecycle import ensure_server

logger = logging.getLogger("lm_visual_mcp.mcp.client")

#: Hint surfaced to the model/user when no server is reachable.
_NO_SERVER_HINT = (
    "vision server unreachable; start it with `lm-visual-mcp start` "
    "(or let the MCP process start it via --start-server)"
)


class RemoteVision:
    """Forwards vision tool calls to ``POST /vision/analyze`` on the server."""

    def __init__(self, host: str, port: int, *, timeout: float = 300.0) -> None:
        self.base = f"http://{host}:{port}"
        self.timeout = timeout

    async def analyze_images(
        self,
        *,
        tool: str,
        image_sources: list[str],
        user_prompt: str,
        output_type: Optional[str] = None,
    ) -> dict:
        payload = {
            "tool": tool,
            "image_sources": image_sources,
            "user_prompt": user_prompt,
            "output_type": output_type,
        }
        try:
            return await asyncio.to_thread(self._post, payload)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.error("vision server call failed: %s", exc)
            return _unreachable_envelope(exc)

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{self.base}/vision/analyze",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())


def _unreachable_envelope(exc: Exception) -> dict:
    return {
        "provider": None,
        "model": None,
        "result": {
            "summary": "",
            "answer": "",
            "observations": [],
            "texts": [],
            "elements": [],
            "warnings": [f"{_NO_SERVER_HINT} ({exc})"],
        },
        "meta": {"duration_ms": 0, "fallbacks": [], "usage": {}},
        "error": _NO_SERVER_HINT,
    }


def connect(cfg: AppConfig, config_path: Optional[str], *, start_server: bool = True) -> Optional[RemoteVision]:
    """Ensure a healthy server (spawning it when allowed), return the client.

    Returns ``None`` when no server is reachable and starting was disabled or
    failed; the MCP tools then answer with an actionable error envelope.
    """
    if not ensure_server(cfg, config_path, start=start_server):
        logger.error(
            "vision server not reachable on %s:%s and not started", cfg.server.host, cfg.server.port
        )
        return None
    return RemoteVision(
        cfg.server.host,
        cfg.server.port,
        timeout=cfg.vision.timeout + 300.0,
    )
