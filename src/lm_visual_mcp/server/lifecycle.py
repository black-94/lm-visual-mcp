"""Singleton server lifecycle: probe, detached spawn, stop.

The MCP client probes the server's ``/health``; if absent it spawns the server
once as a detached background process and waits for it to come up. Port
binding arbitrates concurrency: only one spawned server can bind the port, the
others exit quietly and every caller connects to the winner. There is no
runtime keep-alive - if the server dies mid-session, the user restarts the MCP
session or runs ``lm-visual-mcp start``.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from ..config import AppConfig

logger = logging.getLogger("lm_visual_mcp.server.lifecycle")

#: Where the server writes its PID (used for diagnosis / cleanup).
PIDFILE = Path("~/.cache/lm-visual-mcp/server.pid").expanduser()


def server_pidfile() -> Path:
    return PIDFILE


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


def probe_server(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True if a healthy vision server answers on host:port."""
    req = urllib.request.Request(f"http://{host}:{port}/health", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return isinstance(data, dict) and data.get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _spawn_detached(cmd: list[str]) -> bool:
    """Launch ``cmd`` as a detached background process.

    On POSIX, ``start_new_session`` detaches it into its own session so it
    survives the spawning client's exit. On Windows, the same flag plus
    ``CREATE_NO_WINDOW`` keeps the child console-less. stdout/stderr go to a
    per-run log file so pre-logging crashes are not silently lost.
    """
    log_path = os.path.join(tempfile.gettempdir(), "lm-visual-mcp-server.log")
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


def start_server(
    cfg: AppConfig,
    config_path: Optional[str],
    *,
    max_wait: float = 6.0,
) -> bool:
    """Spawn the server (``lm-visual-mcp server``) and wait until it answers."""
    host, port = cfg.server.host, cfg.server.port
    cmd = [sys.executable, "-m", "lm_visual_mcp", "server"]
    if config_path:
        cmd += ["--config", config_path]
    if not _spawn_detached(cmd):
        return False

    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        if probe_server(host, port, 0.5):
            return True
        time.sleep(0.1)
    return probe_server(host, port, 0.5)


def stop_server(cfg: AppConfig) -> dict:
    """Stop the singleton server (SIGTERM by pidfile; verify by health probe)."""
    host, port = cfg.server.host, cfg.server.port
    pidfile = server_pidfile()
    pid = read_pidfile(pidfile)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as exc:
            logger.warning("failed to signal server pid %s: %s", pid, exc)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not probe_server(host, port, 0.5):
            unlink_pidfile(pidfile)
            return {"service": "server", "status": "stopped", "port": port}
        time.sleep(0.1)
    return {"service": "server", "status": "still-running", "port": port}


def ensure_server(cfg: AppConfig, config_path: Optional[str], *, start: bool = True) -> bool:
    """Probe the singleton server; optionally spawn it when absent.

    Returns True when a healthy server is reachable afterwards.
    """
    host, port = cfg.server.host, cfg.server.port
    if probe_server(host, port):
        return True
    if not start:
        return False
    logger.info("no vision server on %s:%s; starting one", host, port)
    if not start_server(cfg, config_path):
        return probe_server(host, port, 0.5)
    return True
