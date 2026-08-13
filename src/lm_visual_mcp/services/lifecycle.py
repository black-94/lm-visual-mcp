"""Lifecycle management for the daemon and proxy singletons.

Both singletons are normally auto-started by the MCP client (``_serve``), but :
functions also let them be managed independently:

    lm-visual-mcp start|stop|restart [--service daemon|proxy]

A service is identified two ways: its pidfile (daemon/proxy both write one, only
the process that wins the port bind) and, as a fallback for stale pidfiles, the
PID listening on its port. ``stop`` prefers the pidfile + process-cmdline check
to avoid killing an unrelated process that reused the PID.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Optional

from ..config import AppConfig
from .control import default_pidfile, proxy_pidfile, read_pidfile, unlink_pidfile
from .proxy import probe_primary, probe_proxy, start_primary, start_proxy

logger = logging.getLogger("lm_visual_mcp.lifecycle")

SERVICES = ("daemon", "proxy")


def service_targets(cfg: AppConfig, name: str) -> tuple[str, int, object]:
    """Return ``(name, port, pidfile)`` for a service."""
    if name == "daemon":
        return "daemon", cfg.runtime.port, default_pidfile()
    return "proxy", cfg.proxy.port, proxy_pidfile()


def start_service(cfg: AppConfig, name: str, config_path: Optional[str]) -> dict:
    """Ensure ``name`` is running (probe-then-launch). Returns a status dict."""
    if name == "daemon":
        host, port = cfg.runtime.host, cfg.runtime.port
        if probe_primary(host, port):
            return {"service": name, "status": "already-running", "port": port}
        ok = start_primary(cfg, config_path, host, port)
    else:
        host, port = cfg.proxy.host, cfg.proxy.port
        if probe_proxy(host, port):
            return {"service": name, "status": "already-running", "port": port}
        ok = start_proxy(cfg, config_path)
    return {"service": name, "status": "started" if ok else "failed", "port": port}


def stop_service(name: str, port: int, pidfile) -> dict:
    """Stop ``name`` via its pidfile, falling back to the port-PID. Idempotent."""
    pid = read_pidfile(pidfile)
    if pid is not None and _is_our_process(pid, name):
        result = _kill(pid, name, port)
    else:
        found = _pid_on_port(port)
        result = (
            _kill(found, name, port)
            if found is not None
            else {"service": name, "status": "not-running", "port": port}
        )
    unlink_pidfile(pidfile)
    return result


def _kill(pid: int, name: str, port: int) -> dict:
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return {"service": name, "status": "not-running", "port": port}
    return {"service": name, "status": "stopped", "port": port}


def _is_our_process(pid: int, name: str) -> bool:
    """True if ``pid``'s cmdline mentions ``lm_visual_mcp <name>``."""
    try:
        out = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return f"lm_visual_mcp {name}" in out


def _pid_on_port(port: int) -> Optional[int]:
    """Return the PID listening on ``port`` via lsof, or None."""
    try:
        out = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None