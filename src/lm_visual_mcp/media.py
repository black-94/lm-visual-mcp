"""Media resolution (image only) and per-task workspaces.

Sources may be local paths or ``http(s)://`` URLs. ``file://`` is rejected to
avoid bypassing path validation. Remote downloads are bounded by time and
size, and validated by MIME type.
"""

from __future__ import annotations

import hashlib
import mimetypes
import shutil
import tempfile
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .errors import ConfigError, MediaError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
IMAGE_MIMES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
    "image/tiff",
}

_HTTP_HEADERS = {"User-Agent": "lm-visual-mcp/0.2"}


@dataclass
class ResolvedMedia:
    """An image source resolved to a validated local file."""

    source: str
    local_path: Path
    mime_type: str
    url: Optional[str] = None


class MediaService:
    def __init__(
        self,
        *,
        max_image_mb: float = 20.0,
        download_timeout: float = 30.0,
        max_download_mb: float = 32.0,
        workdir: Optional[Path] = None,
    ) -> None:
        self.max_image_mb = max_image_mb
        self.download_timeout = download_timeout
        self.max_download_mb = max_download_mb
        self.workdir = workdir

    # -- entry points ------------------------------------------------------
    def resolve_image(self, source: str) -> ResolvedMedia:
        if not source or not source.strip():
            raise MediaError("image source is empty")
        if source.lower().startswith("file://"):
            raise MediaError("file:// sources are not allowed; use a local path or http(s) URL")
        if source.lower().startswith(("http://", "https://")):
            local = self._download(source)
            mime = _guess_mime(local.suffix)
            self._validate_size(local.stat().st_size)
            return ResolvedMedia(source, local, mime, url=source)
        # Local path.
        path = Path(source).expanduser()
        if not path.exists():
            raise MediaError(f"image not found: {source}")
        if not path.is_file():
            raise MediaError(f"image path is not a file: {source}")
        mime = _guess_mime(path.suffix.lower())
        self._validate_mime(mime)
        self._validate_size(path.stat().st_size)
        return ResolvedMedia(source, path, mime)

    # -- download -----------------------------------------------------------
    def _download(self, url: str) -> Path:
        if self.workdir is None:
            raise MediaError("cannot download media without a configured workdir")
        target_dir = self.workdir
        target_dir.mkdir(parents=True, exist_ok=True)
        suffix = _suffix_from_url(url) or ".png"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        target = target_dir / f"download-{digest}{suffix}"
        max_bytes = int(self.max_download_mb * 1024 * 1024)
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)

        try:
            with urllib.request.urlopen(  # noqa: S310 - http(s) sources are user-authorized
                req, timeout=self.download_timeout
            ) as resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype and ctype not in IMAGE_MIMES:
                    # Allow only if we can't guess; otherwise it's likely not media.
                    if ctype != "application/octet-stream":
                        raise MediaError(f"unexpected content-type {ctype!r} for image source {url}")
                bytes_written = 0
                with open(target, "wb") as out:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        bytes_written += len(chunk)
                        if bytes_written > max_bytes:
                            raise MediaError(f"image too large (>{self.max_download_mb} MB): {url}")
                        out.write(chunk)
        except MediaError:
            target.unlink(missing_ok=True)
            raise
        except Exception as exc:  # noqa: BLE001
            target.unlink(missing_ok=True)
            raise MediaError(f"failed to download image: {exc}") from exc
        return target

    # -- validation ----------------------------------------------------------
    def _validate_mime(self, mime: str) -> None:
        if mime not in IMAGE_MIMES:
            raise MediaError(f"unsupported image type {mime!r}")

    def _validate_size(self, size_bytes: int) -> None:
        if size_bytes > self.max_image_mb * 1024 * 1024:
            raise MediaError(f"image exceeds {self.max_image_mb} MB limit")


def _guess_mime(suffix: str) -> str:
    if suffix in IMAGE_EXTENSIONS:
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
    guessed, _ = mimetypes.guess_type(f"x{suffix}")
    return guessed or "application/octet-stream"


def _suffix_from_url(url: str) -> str:
    from urllib.parse import urlparse

    path = urlparse(url).path
    return Path(path).suffix.lower()


def tempfile_mkdtemp() -> str:
    return tempfile.mkdtemp(prefix="lm-visual-mcp-dl-")


# -- workspaces ---------------------------------------------------------------


@dataclass
class Workspace:
    """A single task's working directory."""

    root: Path
    input_dir: Path
    output_dir: Path
    schema_path: Path
    _temporary: bool = False
    _created: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._created = True

    def stage_media(self, source: str) -> Path:
        """Copy/link a media source into the input dir under a safe name."""
        src = Path(source)
        target = self.input_dir / f"media-{uuid.uuid4().hex[:12]}{src.suffix}"
        if not src.exists():
            raise FileNotFoundError(f"media source not found: {source}")
        shutil.copy2(src, target)
        return target

    def cleanup(self) -> None:
        """Remove the workspace. Only removes directories this manager created."""
        if self._temporary and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


class WorkspaceManager:
    """Creates and reaps per-task workspaces.

    ``base`` is the configured ``vision.workdir`` (may be ``None``).
    """

    def __init__(self, base: Optional[Path] = None) -> None:
        self.base = base.expanduser().resolve() if base else None

    def create(self) -> Workspace:
        if self.base is None:
            root = Path(tempfile.mkdtemp(prefix="lm-visual-mcp-"))
            return Workspace(
                root=root,
                input_dir=root / "input",
                output_dir=root / "output",
                schema_path=root / "schema.json",
                _temporary=True,
            )
        if not self.base.is_dir():
            raise ConfigError(f"workdir is not a directory: {self.base}")
        task = self.base / ".lm-visual-mcp" / str(uuid.uuid4())
        return Workspace(
            root=task,
            input_dir=task / "input",
            output_dir=task / "output",
            schema_path=task / "schema.json",
            _temporary=True,
        )

    def write_schema(self, workspace: Workspace, schema: dict) -> Path:
        import json

        workspace.schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        return workspace.schema_path
