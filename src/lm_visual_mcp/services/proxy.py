"""Client-side proxy for the shared single-instance lm-vision-server.

The default CLI entry runs this proxy: it presents the normal MCP stdio server
to Claude Code, but forwards every tool call over loopback HTTP to the one
shared lm-vision-server (see :mod:`lm_visual_mcp.services.control`).

Startup policy (probe-then-launch): probe the server's ``/health``; if present,
reuse it; otherwise spawn the server once and wait for it to come up, then
proxy to it. There is no runtime keep-alive: if the server dies mid-request,
the call fails and the user restarts the MCP session or runs a manual command.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Optional

from ..config import AppConfig

logger = logging.getLogger("lm_visual_mcp.proxy")


def probe_server(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a healthy lm-vision-server answers on host:port."""
    req = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return isinstance(data, dict) and data.get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def probe_proxy(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a healthy vision proxy answers on host:port."""
    req = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return isinstance(data, dict) and data.get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _spawn_detached(cmd: list[str], log_name: str = "service") -> bool:
    """Launch ``cmd`` as a detached background process.

    On POSIX, ``start_new_session`` detaches it into its own session so it
    survives the spawning client's exit. On Windows, the same flag plus
    ``CREATE_NO_WINDOW`` keeps the child console-less and in its own process
    group (no flashing console; survives the client's console close).

    stdout/stderr are redirected to a per-service log file (not DEVNULL) so the
    background process's pre-detach noise (and any crash before its own
    ``_setup_server_logging`` swaps the root logger) is not silently lost. The
    file is truncated on each (re)spawn; the long-term log history lives in
    ``~/.cache/lm-visual-mcp/{service,proxy}.log`` with size-based rotation.
    """
    log_path = os.path.join(tempfile.gettempdir(), f"lm-visual-mcp-{log_name}.log")
    logf = open(log_path, "wb")  # truncate: each run is its own paper trail
    logger.info("spawning %s -> log %s", cmd, log_path)
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": logf,
        "stderr": logf,
        "start_new_session": True,
    }
    if platform.system() == "Windows":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except OSError as exc:  # e.g. missing interpreter
        logger.warning("failed to start background process %s: %s", cmd, exc)
        return False


def start_proxy(
    cfg: AppConfig,
    config_path: Optional[str],
    max_wait: float = 6.0,
) -> bool:
    """Spawn the vision proxy (``--proxy``) and wait until it answers /health.

    The proxy re-resolves its own config via the same priority (file/env/CLI)
    given ``config_path``. Safe under concurrency: only one spawned proxy can
    bind the port; the others exit quietly and every caller connects to the
    winner.
    """
    host, port = cfg.proxy.host, cfg.proxy.port
    cmd = [sys.executable, "-m", "lm_visual_mcp", "proxy"]
    if config_path:
        cmd += ["--config", config_path]
    if not _spawn_detached(cmd, log_name="proxy"):
        return False

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if probe_proxy(host, port, 0.5):
            return True
        time.sleep(0.1)
    return probe_proxy(host, port, 0.5)


def start_server(
    cfg: AppConfig,
    config_path: Optional[str],
    host: str,
    port: int,
    max_wait: float = 5.0,
) -> bool:
    """Spawn the shared lm-vision-server (``--server``) and wait until it answers.

    The server re-resolves its own config via the same priority (file/env/CLI)
    given ``config_path``. Safe under concurrency: only one spawned server can
    bind the port; the others exit quietly and every caller connects to the
    winner.
    """
    cmd = [sys.executable, "-m", "lm_visual_mcp", "server"]
    if config_path:
        cmd += ["--config", config_path]
    if not _spawn_detached(cmd, log_name="server"):
        return False

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if probe_server(host, port, 0.5):
            return True
        time.sleep(0.1)
    return probe_server(host, port, 0.5)


class ProxyVisionSession:
    """Drop-in for :class:`VisionSession` that forwards over HTTP to the server.

    Same signature surface (``analyze_images`` / ``analyze_video``) so
    ``build_server(cfg, session=...)`` reuses the existing tool handlers
    unchanged. Only the lm-vision-server owns a real ``VisionSession`` and
    computes ``get_system_prompt``; this side just shuttles normalized payloads.
    """

    def __init__(
        self,
        cfg: AppConfig,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> None:
        self.cfg = cfg
        self.host = host or cfg.runtime.host
        self.port = port or cfg.runtime.port
        self._timeout = cfg.runtime.timeout + 300.0

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
        # No runtime keep-alive: the server is probed+launched once at MCP
        # startup. If it dies mid-session, this call fails and the user restarts
        # the MCP session or runs a manual ``lm-visual-mcp start``.
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