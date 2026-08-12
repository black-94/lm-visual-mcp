"""Client-side proxy for the shared single-instance daemon.

The default CLI entry runs this proxy: it presents the normal MCP stdio server
to Claude Code, but forwards every tool call over loopback HTTP to the one
shared daemon (see :mod:`lm_visual_mcp.services.control`).

Startup policy (probe-then-launch): probe the daemon's ``/health``; if present,
reuse it; otherwise spawn the daemon once and wait for it to come up, then
proxy to it. A dead daemon mid-request is restarted on the next call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

from ..config import AppConfig

logger = logging.getLogger("lm_visual_mcp.proxy")


def probe_primary(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a healthy daemon answers on host:port."""
    req = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return isinstance(data, dict) and data.get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def start_primary(
    cfg: AppConfig,
    config_path: Optional[str],
    host: str,
    port: int,
    max_wait: float = 5.0,
) -> bool:
    """Spawn the shared daemon (``--daemon``) and wait until it answers.

    The daemon re-resolves its own config via the same priority (file/env/CLI)
    given ``config_path``. Safe under concurrency: only one spawned daemon can
    bind the port; the others exit quietly and every caller connects to the
    winner.
    """
    cmd = [sys.executable, "-m", "lm_visual_mcp", "daemon"]
    if config_path:
        cmd += ["--config", config_path]
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # survive the spawning client's exit
        )
    except OSError as exc:
        logger.warning("failed to start daemon: %s", exc)
        return False

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if probe_primary(host, port, 0.5):
            return True
        time.sleep(0.1)
    return probe_primary(host, port, 0.5)


class ProxyVisionSession:
    """Drop-in for :class:`VisionSession` that forwards over HTTP to the daemon.

    Same signature surface (``analyze_images`` / ``analyze_video``) so
    ``build_server(cfg, session=...)`` reuses the existing tool handlers
    unchanged. Only the daemon owns a real ``VisionSession`` and computes
    ``get_system_prompt``; this side just shuttles normalized payloads.
    """

    def __init__(
        self,
        cfg: AppConfig,
        host: Optional[str] = None,
        port: Optional[int] = None,
        *,
        config_path: Optional[str] = None,
    ) -> None:
        self.cfg = cfg
        self.host = host or cfg.runtime.host
        self.port = port or cfg.runtime.port
        self.config_path = config_path
        self._timeout = cfg.runtime.timeout + 300.0
        # Set True after one in-flight restart+retry; second consecutive failure raises.
        self._started_once = False

    # -- public API (mirrors VisionSession) -------------------------------
    async def analyze_images(
        self,
        *,
        tool: str,
        image_sources: list[str],
        user_prompt: str,
        output_type: Optional[str] = None,
    ) -> dict:
        return await self._call(
            tool=tool,
            image_sources=image_sources,
            user_prompt=user_prompt,
            output_type=output_type,
        )

    async def analyze_video(
        self,
        *,
        tool: str,
        video_source: str,
        user_prompt: str,
    ) -> dict:
        return await self._call(
            tool=tool,
            video_sources=[video_source],
            user_prompt=user_prompt,
        )

    # -- core ----------------------------------------------------------------
    async def _call(self, **payload: object) -> dict:
        try:
            return await asyncio.to_thread(self._post, payload)
        except (urllib.error.URLError, ConnectionError, OSError):
            if self._started_once:
                raise
            self._started_once = True
            logger.info("daemon unresponsive; restarting on %s:%s", self.host, self.port)
            start_primary(self.cfg, self.config_path, self.host, self.port)
            return await asyncio.to_thread(self._post, payload)

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/tool",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read())