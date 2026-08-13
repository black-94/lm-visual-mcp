"""OpenAI Responses API adapter.

Rewrites ``input[].content[]`` input_image parts into input_text parts.
"""

from __future__ import annotations

import json

from ..errors import MediaError
from ..services.media import MediaService
from .media import resolve_image
from .types import Extracted, ImageSlot, ProtocolAdapter


class OpenAIResponsesAdapter(ProtocolAdapter):
    path = "openai/responses"

    def has_image(self, body: bytes) -> bool:
        return b'"input_image"' in body

    def extract(self, body: bytes, media: MediaService) -> Extracted:
        doc = json.loads(body)
        slots: list[ImageSlot] = []
        for item in doc.get("input", []):
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict) or part.get("type") != "input_image":
                    continue
                reference = part.get("image_url", "")
                try:
                    image = resolve_image(reference, media)
                except MediaError:
                    continue
                index = len(slots)

                def _apply(text: str, _part: dict = part, _index: int = index) -> None:
                    _part.clear()
                    _part.update({"type": "input_text", "text": f"[Image {_index + 1}]\n{text}"})

                slots.append(ImageSlot(image, _apply))
        return Extracted(doc, slots)