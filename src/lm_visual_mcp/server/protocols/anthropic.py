"""Anthropic Messages adapter.

Rewrites ``messages[].content[]`` image blocks into text blocks. The
replacement text starts with the staged image's absolute path.
"""

from __future__ import annotations

import json

from ...errors import MediaError
from ...media import MediaService
from .media import from_base64_bytes
from .types import Extracted, ImageSlot, ProtocolAdapter, image_text_header, iter_image_blocks


class AnthropicAdapter(ProtocolAdapter):
    path = "anthropic"

    def has_image(self, body: bytes) -> bool:
        return b'"type": "image"' in body or b'"type":"image"' in body

    def extract(self, body: bytes, media: MediaService) -> Extracted:
        doc = json.loads(body)
        slots: list[ImageSlot] = []
        for content, i, block in iter_image_blocks(doc, lambda b: b.get("type") == "image"):
            source = block.get("source") or {}
            data = source.get("data", "")
            media_type = source.get("media_type", "image/png")
            try:
                image = from_base64_bytes(data, media_type, media)
            except MediaError:
                continue
            index = len(slots)

            def _apply(
                text: str,
                _content: list = content,
                _i: int = i,
                _image: object = image,
                _index: int = index,
            ) -> None:
                _content[_i] = {
                    "type": "text",
                    "text": f"{image_text_header(_image, _index)}\n{text}",
                }

            slots.append(ImageSlot(image, _apply))
        return Extracted(doc, slots)
