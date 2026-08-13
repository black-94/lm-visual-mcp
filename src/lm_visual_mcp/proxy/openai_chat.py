"""OpenAI Chat Completions adapter.

Rewrites ``messages[].content[]`` image_url parts into text parts.
"""

from __future__ import annotations

import json

from ..errors import MediaError
from ..services.media import MediaService
from .media import resolve_image
from .types import Extracted, ImageSlot, ProtocolAdapter


class OpenAIChatAdapter(ProtocolAdapter):
    path = "openai/chat"

    def has_image(self, body: bytes) -> bool:
        return b'"image_url"' in body

    def extract(self, body: bytes, media: MediaService) -> Extracted:
        doc = json.loads(body)
        slots: list[ImageSlot] = []
        for msg in doc.get("messages", []):
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "image_url":
                    continue
                url_ref = part.get("image_url")
                if isinstance(url_ref, str):
                    reference = url_ref
                elif isinstance(url_ref, dict):
                    reference = url_ref.get("url", "")
                else:
                    continue
                try:
                    image = resolve_image(reference, media)
                except MediaError:
                    continue  # skip unparseable image refs; keep the rest
                index = len(slots)

                def _apply(text: str, _part: dict = part, _index: int = index) -> None:
                    _part.clear()
                    _part.update({"type": "text", "text": f"[Image {_index + 1}]\n{text}"})

                slots.append(ImageSlot(image, _apply))
        return Extracted(doc, slots)