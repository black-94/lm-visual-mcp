"""Shared adapter types and serialization (no concrete-import cycle)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from ..models import ImageInput
from ..services.media import MediaService


@dataclass
class ImageSlot:
    """One image found in the request, with a closure that writes its text back."""

    image: ImageInput
    apply: Callable[[str], None]


@dataclass
class Extracted:
    """Parsed request doc + the ordered image slots found in it."""

    doc: dict
    slots: list[ImageSlot]


class ProtocolAdapter:
    """Interface implemented by each protocol adapter."""

    path: str = ""

    def has_image(self, body: bytes) -> bool:
        raise NotImplementedError

    def extract(self, body: bytes, media: MediaService) -> Extracted:
        raise NotImplementedError


def serialize(doc: dict) -> bytes:
    """Serialize a parsed request doc back to bytes for forwarding."""
    return json.dumps(doc).encode("utf-8")