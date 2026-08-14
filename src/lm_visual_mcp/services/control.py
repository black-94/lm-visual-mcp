"""Shared single-instance lm-vision-server (server side).

The default CLI entry is a *client* that proxies MCP tool calls over HTTP to a
single shared server. This module is that server: a plain HTTP loopback service
(no stdio, no MCP transport) that owns the one global ``VisionSession`` and
serializes every request through its concurrency semaphore.

Endpoints
    GET  /health  -> ``{"ok": true, "version": ..., "tools": N, "pid": ...}``
    POST /tool    -> ``{"tool","image_sources","video_sources","user_prompt",
                       "output_type"}`` in, MCP envelope JSON out.

The server binds the port *before* detaching so a port conflict (another live
server) is detected and exits cleanly without writing a pidfile. Once started it
stays running (always-on) until stopped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from .. import __version__
from ..config import AppConfig

logger = logging.getLogger("lm_visual_mcp.control")

#: Where the lm-vision-server writes its PID (used for diagnosis / cleanup).
DEFAULT_PIDFILE = Path("~/.cache/lm-visual-mcp/lm-visual-mcp-server.pid").expanduser()
#: Where the lm-proxy writes its PID.
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


def detach(pidfile: Path) -> None:
    """Detach into a background server process.

    Runs only in the process that will serve. On POSIX this is the fully-forked
    grandchild (double fork + setsid) so the intermediate parents exit and the
    bound socket FD survives. On Windows there is no ``fork``: the client
    already spawned this process detached (``subprocess.Popen`` with
    ``start_new_session`` + DEVNULL stdio), so we only write the pidfile and
    redirect logs. Logs after this point go to a file next to the pidfile.
    """
    if platform.system() == "Windows":
        _detach_windows(pidfile)
        return

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

    # Redirect logging to a file so server errors are not silently lost.
    _setup_server_logging(pidfile.parent)


class _Handler(BaseHTTPRequestHandler):
    """Loopback HTTP handler for the shared lm-vision-server."""

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
        session_factory=None,
    ) -> None:
        self.cfg = cfg
        self.host = host
        self.port = port
        # Test seam: override to inject a VisionSession wired to a fake router.
        self.session_factory = session_factory or self._default_session
        self.loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._session: Optional[VisionSession] = None
        self._server: Optional[ThreadingHTTPServer] = None
        self._ready = threading.Event()
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

    def bind(self) -> None:
        """Bind the socket. Raises :class:`OSError` if the port is taken."""
        self._server = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._server.tool_server = self  # type: ignore[attr-defined]

    def serve(self) -> None:
        assert self._server is not None
        threading.Thread(target=self._loop_main, name="server-loop", daemon=True).start()
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

    # -- tool dispatch -----------------------------------------------------
    async def run_tool(self, payload: dict) -> dict:
        """Route a normalized tool payload to the shared VisionSession.

        ``get_system_prompt`` stays on the server side (single place); the
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


def run_server(cfg: AppConfig) -> int:
    """Entry for the ``--server`` subcommand: bind, detach, serve."""
    host, port = cfg.runtime.host, cfg.runtime.port
    ts = ToolServer(cfg, host, port)
    try:
        ts.bind()
    except OSError as exc:
        # Another live server already holds the port — exit quietly and let the
        # client connect to the existing one.
        logger.info("port %s:%s already in use (%s); exiting", host, port, exc)
        return 0
    detach(default_pidfile())
    ts.serve()
    return 0


def _setup_server_logging(log_dir: Path) -> None:
    """Set up file-based logging for the lm-vision-server process."""
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "lm-vision-server.log"
        handler = logging.FileHandler(str(log_file), encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        root = logging.getLogger("lm_visual_mcp")
        root.handlers = [handler]
        root.setLevel(logging.INFO)
    except OSError:
        pass  # best-effort; don't crash the server if log setup fails


def _detach_windows(pidfile: Path) -> None:
    """Windows detach: no double-fork, the client already backgrounded us.

    The detached spawn (``start_new_session`` + DEVNULL stdio) happens in
    ``start_server``/``start_proxy`` on the client side; here we only persist
    the pidfile and route logs to a file. If the server was started directly
    from a console, it stays attached to that console — acceptable fallback,
    since a true background detach on Windows requires the client-spawn path.
    """
    try:
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pass
    _setup_server_logging(pidfile.parent)