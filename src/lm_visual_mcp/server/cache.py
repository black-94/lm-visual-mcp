"""Per-image SHA-256 description cache (memory + disk).

Cache granularity is one entry per image (key = SHA-256 of the image bytes).
Vision calls are batched per request. The cache is two-tier so it survives
restarts:

- a bounded in-memory dict (FIFO eviction) as the fast tier;
- a persisted copy under ``RUNTIME_DIR/descriptions/{sha256}.json`` so the
  same image is described only once even across server restarts.

The disk tier is keyed independently of any per-task workspace, so GC'ing old
``{uuid}`` directories never invalidates a cached description.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from ..paths import DESCRIPTIONS_DIR


class VisionCache:
    def __init__(self, max_entries: int = 4096, directory: Optional[Path] = None) -> None:
        self._max = max_entries
        self._map: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._dir = Path(directory).expanduser() if directory else DESCRIPTIONS_DIR

    def get(self, key: str) -> Optional[str]:
        return self._map.get(key)

    def put(self, key: str, description: str) -> None:
        if key in self._map:
            return
        if len(self._map) >= self._max:
            # FIFO eviction (dict preserves insertion order).
            self._map.pop(next(iter(self._map)))
        self._map[key] = description

    async def aget(self, key: str) -> Optional[str]:
        async with self._lock:
            hit = self._map.get(key)
        if hit is not None:
            return hit
        # Miss in memory: fall through to the disk tier and backfill memory.
        disk = self._read_disk(key)
        if disk is not None:
            async with self._lock:
                self.put(key, disk)
        return disk

    async def aput(
        self,
        key: str,
        description: str,
        provider: Optional[str] = None,
    ) -> None:
        async with self._lock:
            self.put(key, description)
        self._write_disk(key, description, provider)

    # -- disk tier ----------------------------------------------------------
    def _read_disk(self, key: str) -> Optional[str]:
        path = self._disk_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            desc = data.get("description")
            return desc if isinstance(desc, str) else None
        except (OSError, ValueError):
            return None

    def _write_disk(
        self, key: str, description: str, provider: Optional[str] = None,
    ) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "sha256": key,
                "description": description,
                "provider": provider,
                "ts": time.time(),
            }
            self._disk_path(key).write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:  # best-effort: memory tier still works without disk
            pass

    def _disk_path(self, key: str) -> Path:
        return self._dir / f"{key}.json"

    # -- keys ---------------------------------------------------------------
    @staticmethod
    def key_of_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def key_of_file(path) -> str:
        return hashlib.sha256(_read_all(path)).hexdigest()

    def __len__(self) -> int:
        return len(self._map)


def _read_all(path) -> bytes:
    if isinstance(path, Path):
        return path.read_bytes()
    with open(path, "rb") as fh:
        return fh.read()