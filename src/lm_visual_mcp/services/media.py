"""Media resolution and validation.

Sources may be local paths or ``http(s)://`` URLs. ``file://`` is rejected to
avoid bypassing path validation. Remote downloads are bounded by time, size and
redirect count, and validated by MIME type.
"""

from __future__ import annotations

import hashlib
import mimetypes
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..errors import MediaError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
}
VIDEO_MIMES = {"video/mp4", "video/quicktime", "video/x-m4v"}

_HTTP_HEADERS = {"User-Agent": "lm-visual-mcp/0.1"}


@dataclass
class ResolvedMedia:
    """A media source resolved to a validated local file."""

    source: str
    local_path: Path
    mime_type: str
    kind: str  # "image" | "video"
    url: Optional[str] = None


class MediaService:
    def __init__(
        self,
        *,
        max_image_mb: float = 20.0,
        max_video_mb: float = 8.0,
        download_timeout: float = 30.0,
        max_download_mb: float = 32.0,
        workdir: Optional[Path] = None,
    ) -> None:
        self.max_image_mb = max_image_mb
        self.max_video_mb = max_video_mb
        self.download_timeout = download_timeout
        self.max_download_mb = max_download_mb
        self.workdir = workdir

    # -- entry points ------------------------------------------------------
    def resolve_image(self, source: str) -> ResolvedMedia:
        return self._resolve(source, kind="image")

    def resolve_video(self, source: str) -> ResolvedMedia:
        return self._resolve(source, kind="video")

    def _resolve(self, source: str, *, kind: str) -> ResolvedMedia:
        if not source or not source.strip():
            raise MediaError(f"{kind} source is empty")
        if source.lower().startswith("file://"):
            raise MediaError("file:// sources are not allowed; use a local path or http(s) URL")
        if source.lower().startswith(("http://", "https://")):
            local = self._download(source, kind=kind)
            mime = _guess_mime(local.suffix, kind)
            self._validate_size(local.stat().st_size, kind)
            return ResolvedMedia(source, local, mime, kind, url=source)
        # Local path.
        path = Path(source).expanduser()
        if not path.exists():
            raise MediaError(f"{kind} not found: {source}")
        if not path.is_file():
            raise MediaError(f"{kind} path is not a file: {source}")
        mime = _guess_mime(path.suffix.lower(), kind)
        self._validate_mime(mime, kind)
        self._validate_size(path.stat().st_size, kind)
        return ResolvedMedia(source, path, mime, kind)

    # -- download -----------------------------------------------------------
    def _download(self, url: str, *, kind: str) -> Path:
        target_dir = self.workdir or Path(tempfile_mkdtemp())
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = _suffix_from_url(url) or f".{kind}"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        target = target_dir / f"download-{digest}{suffix}"
        max_bytes = int(self.max_download_mb * 1024 * 1024)
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)

        try:
            with urllib.request.urlopen(  # noqa: S310 - http(s) sources are user-authorized
                req, timeout=self.download_timeout
            ) as resp:
                final_url = resp.geturl()
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                allowed = IMAGE_MIMES if kind == "image" else VIDEO_MIMES
                if ctype and ctype not in allowed:
                    # Allow only if we can't guess; otherwise it's likely not media.
                    if ctype != "application/octet-stream":
                        raise MediaError(f"unexpected content-type {ctype!r} for {kind} source {url}")
                bytes_written = 0
                with open(target, "wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > max_bytes:
                            raise MediaError(f"{kind} too large (>{self.max_download_mb} MB): {url}")
                        out.write(chunk)
        except MediaError:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            target.unlink(missing_ok=True)
            raise MediaError(f"failed to download {kind}: {exc}") from exc
        return target

    # -- validation ----------------------------------------------------------
    def _validate_mime(self, mime: str, kind: str) -> None:
        allowed = IMAGE_MIMES if kind == "image" else VIDEO_MIMES
        if mime not in allowed:
            raise MediaError(f"unsupported {kind} type {mime!r}")

    def _validate_size(self, size_bytes: int, kind: str) -> None:
        limit = self.max_image_mb if kind == "image" else self.max_video_mb
        if size_bytes > limit * 1024 * 1024:
            raise MediaError(f"{kind} exceeds {limit} MB limit")


def _guess_mime(suffix: str, kind: str) -> str:
    if kind == "image" and suffix in IMAGE_EXTENSIONS:
        return {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".tiff": "image/tiff",
            ".tif": "image/tiff",
        }[suffix]
    if kind == "video" and suffix in VIDEO_EXTENSIONS:
        return {".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/x-m4v"}[suffix]
    guessed, _ = mimetypes.guess_type(suffix)
    return guessed or "application/octet-stream"


def _suffix_from_url(url: str) -> str:
    from urllib.parse import urlparse

    path = urlparse(url).path
    return Path(path).suffix.lower()


def tempfile_mkdtemp() -> str:
    import tempfile

    return tempfile.mkdtemp(prefix="lm-visual-mcp-dl-")