"""Resolve an image reference (data URL or http(s) URL) into an :class:`ImageInput`.

Both OpenAI (``image_url`` / ``input_image``) and Anthropic (base64 ``source``)
end up as a local file so the vision providers can read them. Files are named
by content hash and persist in the server's media cache directory, keeping the
absolute paths written into rewritten prompts valid for later use.
"""

from __future__ import annotations

import base64
import hashlib

from ...errors import MediaError
from ...media import MediaService, tempfile_mkdtemp
from ...providers.types import ImageInput

_MIME_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


def resolve_image(reference: str, media: MediaService) -> ImageInput:
    """Turn ``reference`` into a local :class:`ImageInput` for the vision stack."""
    if not reference or not reference.strip():
        raise MediaError("image reference is empty")
    if reference.startswith("data:"):
        return _from_data_url(reference, media)
    if reference.startswith(("http://", "https://")):
        resolved = media.resolve_image(reference)
        return ImageInput(
            source=reference,
            local_path=str(resolved.local_path),
            mime_type=resolved.mime_type,
            url=resolved.url,
        )
    raise MediaError("image reference must be a data: URL or an http(s) URL")


def from_base64_bytes(data: str, media_type: str, media: MediaService) -> ImageInput:
    """Create an :class:`ImageInput` from raw base64 (Anthropic source.data)."""
    try:
        raw = base64.b64decode(data)
    except Exception as exc:  # noqa: BLE001
        raise MediaError("image base64 is invalid") from exc
    return _write_temp(raw, media_type or "image/png", media)


# -- helpers ----------------------------------------------------------------
def _from_data_url(data_url: str, media: MediaService) -> ImageInput:
    header, comma, b64 = data_url.partition(",")
    if not comma:
        raise MediaError("invalid data: URL for image")
    media_type = header[len("data:"):].split(";")[0] or "image/png"
    try:
        raw = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        raise MediaError("image data: URL base64 is invalid") from exc
    return _write_temp(raw, media_type, media)


def _write_temp(raw: bytes, media_type: str, media: MediaService) -> ImageInput:
    workdir = media.workdir
    if workdir is None:
        from pathlib import Path

        workdir = Path(tempfile_mkdtemp())
        media.workdir = workdir
    workdir.mkdir(parents=True, exist_ok=True)
    ext = _MIME_EXT.get(media_type.lower(), ".png")
    name = hashlib.sha1(raw).hexdigest()[:16] + ext
    path = workdir / name
    if not path.exists():
        path.write_bytes(raw)
    return ImageInput(source=media_type, local_path=str(path), mime_type=media_type)
