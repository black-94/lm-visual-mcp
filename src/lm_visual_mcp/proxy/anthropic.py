"""Anthropic Messages adapter.

Rewrites ``messages[].content[]`` image blocks into text blocks.
"""

from __future__ import annotations

import json

from ..errors import MediaError
from ..services.media import MediaService
from .media import from_base64_bytes
from .types import Extracted, ImageSlot, ProtocolAdapter


class AnthropicAdapter(ProtocolAdapter):
    path = "anthropic"

    def has_image(self, body: bytes) -> bool:
        return b'"type": "image"' in body or b'"type":"image"' in body

    def extract(self, body: bytes, media: MediaService) -> Extracted:
        doc = json.loads(body)
        slots: list[ImageSlot] = []
        for msg in doc.get("messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for i, block in enumerate(content):
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                source = block.get("source") or {}
                data = source.get("data", "")
                media_type = source.get("media_type", "image/png")
                try:
                    image = from_base64_bytes(data, media_type, media)
                except MediaError:
                    continue
                index = len(slots)

                def _apply(text: str, _content: list = content, _i: int = i, _index: int = index) -> None:
                    _content[_i] = {"type": "text", "text": f"[Image {_index + 1}]\n{text}"}

                slots.append(ImageSlot(image, _apply))
        return Extracted(doc, slots)