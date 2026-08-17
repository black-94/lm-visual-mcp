"""Centralized runtime paths.

All state that lm-visual-mcp persists across requests/restarts lives under a
single root (~/.cache/lm-visual-mcp). Keeping the constants here - rather than
scattered string literals - makes GC, cleanup and auditing straightforward.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path

#: Single runtime root for every per-task workspace, disk cache and pidfile.
RUNTIME_DIR = Path("~/.cache/lm-visual-mcp").expanduser()

#: Where the server writes its PID (used for diagnosis / cleanup).
PIDFILE = RUNTIME_DIR / "server.pid"

#: Persisted per-image description cache (key = sha256 of image bytes).
DESCRIPTIONS_DIR = RUNTIME_DIR / "descriptions"

#: Default retention for GC'able runtime artifacts.
_DEFAULT_RETENTION_SECONDS = 7 * 24 * 3600  # 7 days

#: Matches the per-task workspace directory names (bare versioned UUIDs).
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def gc_runtime(retention_seconds: float = _DEFAULT_RETENTION_SECONDS) -> dict:
    """Reclaim stale runtime artifacts.

    Workspaces (``RUNTIME_DIR/<uuid>``) and persisted description entries
    (``RUNTIME_DIR/descriptions/*.json``) older than ``retention_seconds`` are
    removed. Only names matching a version-4 UUID are treated as workspaces, so
    logs/pidfiles are never touched. Each workspace is an independent copy, so
    deleting any one never breaks the others or the description cache.
    """
    now = time.time()
    removed = {"workspaces": 0, "descriptions": 0}

    if RUNTIME_DIR.is_dir():
        for child in RUNTIME_DIR.iterdir():
            if child.is_dir() and _UUID_RE.match(child.name):
                if now - _mtime_ok(child, now) > retention_seconds:
                    shutil.rmtree(child, ignore_errors=True)
                    removed["workspaces"] += 1

    if DESCRIPTIONS_DIR.is_dir():
        for entry in DESCRIPTIONS_DIR.glob("*.json"):
            if now - _mtime_ok(entry, now) > retention_seconds:
                try:
                    entry.unlink(missing_ok=True)
                    removed["descriptions"] += 1
                except OSError:
                    pass

    return removed


def _mtime_ok(path: Path, now: float) -> float:
    """Return the file mtime, or ``now`` on stat failure (keeps young things)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return now