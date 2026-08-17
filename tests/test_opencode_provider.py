"""OpenCode API provider tests (mocked HTTP)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lm_visual_mcp.errors import ProviderUnavailableError
from lm_visual_mcp.providers.opencode import OpenCodeProvider
from lm_visual_mcp.providers.types import ImageRequest, ProviderFailureReason

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, *, json=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def make_request(tmp_path: Path) -> ImageRequest:
    img = tmp_path / "x.png"
    img.write_bytes(_PNG)
    return ImageRequest(
        system_prompt="sys", user_prompt="describe",
        images=[type("I", (), {"local_path": str(img), "url": None, "mime_type": "image/png"})()],
    )


def ok_body(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


async def test_success_parses_json(tmp_path):
    session = FakeSession([FakeResponse(200, ok_body('{"summary":"s","answer":"a"}'))])
    p = OpenCodeProvider(api_key="k", session=session)
    result = await p.analyze_image(make_request(tmp_path))
    assert result.provider == "opencode"
    assert result.result["answer"] == "a"
    req = session.requests[0]
    assert req["url"].endswith("/chat/completions")
    assert req["headers"]["Authorization"] == "Bearer k"
    # The image is inlined as a data URL.
    content = req["json"]["messages"][1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_quota_error_classified(tmp_path):
    session = FakeSession([FakeResponse(429, b'{"error":"rate"}')])
    p = OpenCodeProvider(api_key="k", session=session)
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason == ProviderFailureReason.QUOTA_EXHAUSTED


async def test_auth_error_classified(tmp_path):
    session = FakeSession([FakeResponse(401, b"unauthorized")])
    p = OpenCodeProvider(api_key="bad", session=session)
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason == ProviderFailureReason.NOT_AUTHENTICATED


async def test_missing_key_unavailable():
    p = OpenCodeProvider(api_key=None)
    status = await p.probe_image()
    assert status.available is False
    assert status.reason == ProviderFailureReason.API_KEY_MISSING


async def test_rate_limited_by_own_limiter(tmp_path):
    clock = {"now": 0.0}
    from lm_visual_mcp.providers.ratelimit import RateLimiter

    limiter = RateLimiter(rpm=1, clock=lambda: clock["now"])
    session = FakeSession([FakeResponse(200, ok_body('{"answer":"a"}'))])
    p = OpenCodeProvider(api_key="k", session=session, limiter=limiter)
    await p.analyze_image(make_request(tmp_path))
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason == ProviderFailureReason.RATE_LIMITED
