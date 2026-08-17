"""OpenAI Responses API adapter.

Rewrites ``input[].content[]`` input_image parts into input_text parts. The
replacement text starts with the staged image's absolute path.
"""

from __future__ import annotations

import json

from ...errors import MediaError
from ...media import MediaService
from .media import resolve_image
from .types import Extracted, ImageSlot, ProtocolAdapter, image_text_header, iter_image_blocks


class OpenAIResponsesAdapter(ProtocolAdapter):
    path = "openai/responses"

    def has_image(self, body: bytes) -> bool:
        return b'"input_image"' in body

    def extract(self, body: bytes, media: MediaService) -> Extracted:
        doc = json.loads(body)
        slots: list[ImageSlot] = []
        for content, i, part in iter_image_blocks(doc, lambda b: b.get("type") == "input_image"):
            reference = part.get("image_url", "")
            try:
                image = resolve_image(reference, media)
            except MediaError:
                continue
            index = len(slots)

            def _apply(
                text: str,
                _part: dict = part,
                _image: object = image,
                _index: int = index,
            ) -> None:
                _part.clear()
                _part.update(
                    {"type": "input_text", "text": f"{image_text_header(_image, _index)}\n{text}"}
                )

            slots.append(ImageSlot(image, _apply))
        return Extracted(doc, slots)
