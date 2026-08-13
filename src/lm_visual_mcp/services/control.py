"""Shared single-instance daemon (server side).

The default CLI entry is a *client* that proxies MCP tool calls over HTTP to a
single shared daemon. This module is that daemon: a plain HTTP loopback service
(no stdio, no MCP transport) that owns the one global ``VisionSession`` and
serializes every request through its concurrency semaphore.

Endpoints
    GET  /health  -> ``{"ok": true, "version": ..., "tools": N, "pid": ...}``
    POST /tool    -> ``{"tool","image_sources","video_sources","user_prompt",
                       "output_type"}`` in, MCP envelope JSON out.

The daemon binds the port *before* daemonizing so a port conflict (another live
daemon) is detected and exits cleanly without writing a pidfile. It reclaims
itself after ``idle_timeout_ms`` of no traffic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .. import __version__
from ..config import AppConfig

logger = logging.getLogger("lm_visual_mcp.control")

#: Where the daemon writes its PID (used for diagnosis / cleanup).
DEFAULT_PIDFILE = Path("~/.cache/lm-visual-mcp/lm-visual-mcp.pid").expanduser()
#: Where the vision proxy writes its PID.
PROXY_PIDFILE = Path("~/.cache/lm-visual-mcp/lm-visual-mcp-proxy.pid").expanduser()


def default_pidfile() -> Path:
    return DEFAULT_PIDFILE


def proxy_pidfile() -> Path:
    return PROXY_PIDFILE


def write_pidfile(pidfile: Path) -> None:
    """Best-effort write ``os.getpid()`` to ``pidfile``."""
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pass


def read_pidfile(pidfile: Path) -> Optional[int]:
    try:
        return int(pidfile.read_text().strip())
    except (OSError, ValueError):
        return None


def unlink_pidfile(pidfile: Path) -> None:
    try:
        pidfile.unlink(missing_ok=True)
    except OSError:
        pass


def daemonize(pidfile: Path) -> None:
    """Detach into a background daemon (double fork + setsid).

    Runs only in the fully-forked grandchild; the intermediate parents exit.
    The bound socket FD (if any) survives fork. Logs after this point go no-
    where (stdio is redirected to /dev/null).
    """
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    os.umask(0o022)
    os.chdir("/")
    sys.stdout.flush()
    sys.stderr.flush()
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    _devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(_devnull, fd)
    if _devnull > 2:
        os.close(_devnull)

    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pass


class _Handler(BaseHTTPRequestHandler):
    """Loopback HTTP handler for the shared daemon."""

    def log_message(self, *args) -> None:  # silence default stderr logging
        pass

    # -- helpers -----------------------------------------------------------
    def _tool_server(self) -> "ToolServer":
        return self.server.tool_server  # type: ignore[attr-defined]

    def _json(self, status: int, payload: dict) -> None:
        # ``default=str`` keeps the connection alive even if some provider value
        # is not natively serializable, rather than dropping the client.
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:
        ts = self._tool_server()
        if self.path == "/health":
            ts.touch()
            # Import-light: tool_names carries no heavy deps, so a cold-start
            # probe answers instantly instead of stalling on the server stack.
            from ..tool_names import _TOOL_NAMES

            self._json(
                200,
                {
                    "ok": True,
                    "version": __version__,
                    "tools": len(_TOOL_NAMES),
                    "pid": os.getpid(),
                },
            )
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        ts = self._tool_server()
        if self.path != "/tool":
            return self._json(404, {"ok": False, "error": "not found"})
        ts.touch()
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, OSError) as exc:
            return self._json(400, {"ok": False, "error": f"bad request: {exc}"})
        if not isinstance(payload, dict):
            return self._json(400, {"ok": False, "error": "expected JSON object"})

        future = asyncio.run_coroutine_threadsafe(ts.run_tool(payload), ts.loop)
        try:
            result = future.result(timeout=ts.execution_timeout)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the client
            logger.exception("tool call failed")
            return self._json(500, {"ok": False, "error": str(exc)})
        self._json(200, result)


class ToolServer:
    """Owns the shared VisionSession and serves it over loopback HTTP."""

    def __init__(
        self,
        cfg: AppConfig,
        host: str,
        port: int,
        idle_timeout_ms: int,
        session_factory=None,
    ) -> None:
        self.cfg = cfg
        self.host = host
        self.port = port
        self.idle_timeout_ms = idle_timeout_ms
        # Test seam: override to inject a VisionSession wired to a fake router.
        self.session_factory = session_factory or self._default_session
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._session: Optional[VisionSession] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._ready = threading.Event()
        self._last_activity = time.monotonic()
        self.execution_timeout = cfg.runtime.timeout + 300.0

    @staticmethod
    def _default_session(cfg: AppConfig) -> VisionSession:
        from ..tools import VisionSession  # deferred: avoids import cycle

        return VisionSession(cfg)

    # -- lifecycle ---------------------------------------------------------
    @property
    def session(self) -> VisionSession:
        """The shared VisionSession; blocks until the loop thread has built it.

        ``/health`` never touches this, so a cold-start probe answers instantly
        while the heavy session import (providers, aiohttp, Pillow) is still
        running in the loop thread. Only ``/tool`` waits on it.
        """
        self._ready.wait()
        assert self._session is not None
        return self._session

    def touch(self) -> None:
        self._last_activity = time.monotonic()

    def bind(self) -> None:
        """Bind the socket. Raises :class:`OSError` if the port is taken."""
        self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._server.tool_server = self  # type: ignore[attr-defined]

    def serve(self) -> None:
        assert self._server is not None
        threading.Thread(target=self._loop_main, name="daemon-loop", daemon=True).start()
        if self.idle_timeout_ms > 0:
            threading.Thread(target=self._reaper, name="daemon-reaper", daemon=True).start()
        # Serve immediately; the session is built concurrently in the loop
        # thread and /tool handlers wait for it via the ``session`` property.
        try:
            self._server.serve_forever()
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            time.sleep(0.05)

    def stop(self) -> None:
        """Stop serving (safe to call from another thread / the reaper)."""
        if self._server is not None:
            self._server.shutdown()

    def _loop_main(self) -> None:
        asyncio.set_event_loop(self.loop)
        self._session = self.session_factory(self.cfg)
        self._ready.set()
        self.loop.run_forever()

    def _reaper(self) -> None:
        interval = max(min(self.idle_timeout_ms / 1000.0 / 2.0, 5.0), 0.5)
        while True:
            time.sleep(interval)
            idle_ms = (time.monotonic() - self._last_activity) * 1000.0
            if idle_ms >= self.idle_timeout_ms:
                logger.info("daemon idle for %dms; reclaiming", self.idle_timeout_ms)
                self.stop()
                return

    # -- tool dispatch -----------------------------------------------------
    async def run_tool(self, payload: dict) -> dict:
        """Route a normalized tool payload to the shared VisionSession.

        ``get_system_prompt`` stays on the daemon side (single place); the
        client only forwards raw tool semantics.
        """
        tool = payload.get("tool")
        user_prompt = payload.get("user_prompt", "")
        image_sources = payload.get("image_sources") or []
        video_sources = payload.get("video_sources") or []
        output_type = payload.get("output_type")

        if video_sources:
            return await self.session.analyze_video(
                tool=tool, video_source=video_sources[0], user_prompt=user_prompt
            )
        return await self.session.analyze_images(
            tool=tool,
            image_sources=image_sources,
            user_prompt=user_prompt,
            output_type=output_type,
        )


def run_daemon(cfg: AppConfig) -> int:
    """Entry for the ``--daemon`` subcommand: bind, daemonize, serve."""
    host, port = cfg.runtime.host, cfg.runtime.port
    ts = ToolServer(cfg, host, port, cfg.runtime.idle_timeout_ms)
    try:
        ts.bind()
    except OSError as exc:
        # Another live daemon already holds the port — exit quietly and let the
        # client connect to the existing one.
        logger.info("port %s:%s already in use (%s); exiting", host, port, exc)
        return 0
    daemonize(default_pidfile())
    ts.serve()
    return 0