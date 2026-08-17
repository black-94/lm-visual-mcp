"""Shared protocol-adapter types and serialization (no concrete-import cycle)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from ...media import MediaService
from ...providers.types import ImageInput


@dataclass
class ImageSlot:
    """One image found in the request, with a closure that writes its text back.

    The rewritten text always records the image's absolute local path first, so
    the text model can reference the file later (e.g. hand it back to a vision
    tool) without guessing where it lives.
    """

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

    def model_of(self, body: bytes) -> str | None:
        """Return the target model name from a request body, or None.

        Implementations read the protocol-specific ``model`` field. Used by the
        hooks to apply model allowlists (empty list = all models).
        """
        raise NotImplementedError


def image_text_header(image: ImageInput, index: int) -> str:
    """The header line written above an image's description text.

    The absolute path is part of the contract: every rewritten image block
    starts with ``[Image N: <absolute-path>]`` so the file remains findable.
    """
    path = image.local_path or image.url or image.source
    return f"[Image {index + 1}: {path}]"


def serialize(doc: dict) -> bytes:
    """Serialize a parsed request doc back to bytes for forwarding."""
    return json.dumps(doc).encode("utf-8")


def iter_image_blocks(node, is_image):
    """Yield ``(container, index, block)`` for every image block anywhere in a request doc.

    Claude Code and other SDKs nest images inside ``tool_result.content`` and
    other content-block lists, not just top-level message content. ``has_image()``
    already spans the whole body with a byte search, so extraction must span
    the whole document too - otherwise we detect an image we cannot rewrite and fall
    back to raw passthrough, which a text-only upstream then rejects. This
    depth-first walk finds every content block satisfying ``is_image`` regardless
    of nesting depth.
    """
    if isinstance(node, dict):
        for value in node.values():
            yield from iter_image_blocks(value, is_image)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, dict) and is_image(item):
                yield (node, i, item)
            else:
                yield from iter_image_blocks(item, is_image)
