"""Media resolution / validation tests."""

from __future__ import annotations

import pytest

from lm_visual_mcp.errors import MediaError
from lm_visual_mcp.services import MediaService


@pytest.fixture
def svc(tmp_path) -> MediaService:
    return MediaService(workdir=tmp_path, max_image_mb=1, max_video_mb=1)


def _png(tmp_path, name="img.png", size=100) -> str:
    p = tmp_path / name
    p.write_bytes(b"\x00" * size)
    return str(p)


def test_local_image(tmp_path, svc) -> None:
    p = _png(tmp_path)
    r = svc.resolve_image(p)
    assert r.kind == "image"
    assert r.mime_type == "image/png"


def test_local_image_missing(tmp_path, svc) -> None:
    with pytest.raises(MediaError, match="not found"):
        svc.resolve_image(str(tmp_path / "nope.png"))


def test_rejects_file_uri(svc) -> None:
    with pytest.raises(MediaError, match="file://"):
        svc.resolve_image("file:///etc/passwd")


def test_unsupported_extension(tmp_path, svc) -> None:
    p = tmp_path / "foo.txt"
    p.write_text("hi", encoding="utf-8")
    with pytest.raises(MediaError, match="unsupported image type"):
        svc.resolve_image(str(p))


def test_size_limit(tmp_path, svc) -> None:
    p = _png(tmp_path, size=2 * 1024 * 1024)  # > 1 MB
    with pytest.raises(MediaError, match="limit"):
        svc.resolve_image(p)


def test_video_extensions(tmp_path, svc) -> None:
    v = tmp_path / "clip.mp4"
    v.write_bytes(b"\x00" * 50)
    r = svc.resolve_video(str(v))
    assert r.kind == "video"
    assert r.mime_type == "video/mp4"


def test_empty_source(svc) -> None:
    with pytest.raises(MediaError, match="empty"):
        svc.resolve_image("   ")


def test_http_download_missing(tmp_path, svc) -> None:
    # Local-server-less: a URL that will fail to connect raises a clear error.
    with pytest.raises(MediaError, match="failed to download"):
        svc.resolve_image("http://127.0.0.1:1/none.png")