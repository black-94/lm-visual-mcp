"""Per-image SHA-256 description cache.

Cache granularity is one entry per image (key = SHA-256 of the image bytes);
vision calls are batched per request. A simple bounded dict with FIFO eviction -
descriptions are short strings, so memory stays negligible.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Optional


class VisionCache:
    def __init__(self, max_entries: int = 4096) -> None:
        self._max = max_entries
        self._map: dict[str, str] = {}
        self._lock = asyncio.Lock()

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
            return self._map.get(key)

    async def aput(self, key: str, description: str) -> None:
        async with self._lock:
            self.put(key, description)

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
