"""Gemini provider tests (google-genai mocked)."""

from __future__ import annotations

import pytest

from vision_mcp.errors import ProviderUnavailableError
from vision_mcp.models import ImageInput, ProviderFailureReason, VisionRequest
from vision_mcp.providers.gemini import GeminiProvider


class FakeMeta:
    prompt_token_count = 10
    candidates_token_count = 5
    total_token_count = 15
    cached_content_token_count = 0


class FakeResponse:
    text = '{"answer":"ok","summary":"x","warnings":[]}'
    usage_metadata = FakeMeta()


class FakeMessages:
    response = FakeResponse()

    class models:
        @staticmethod
        async def generate_content(model, contents, config):
            return FakeMessages.response


class FakeClient:
    aio = FakeMessages()


@pytest.fixture
def provider(tmp_path):
    _png(tmp_path)
    return GeminiProvider(model="gemini-2.0-flash", api_key="k", client=FakeClient())


def _png(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n")
    return p


def _req(n=1, tmp_path=None):
    imgs = [
        ImageInput(source=str(tmp_path / "x.png"), local_path=str(tmp_path / "x.png"),
                   mime_type="image/png")
        for i in range(n)
    ]
    return VisionRequest(system_prompt="s", user_prompt="u", images=imgs)


async def test_gemini_analyze_structured(provider, tmp_path):
    result = await provider.analyze(_req(tmp_path=tmp_path))
    assert result.provider == "gemini"
    assert result.result["answer"] == "ok"
    assert result.usage.input_tokens == 10


async def test_gemini_api_key_resolution_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "envkey")
    p = GeminiProvider(model="m", api_key="envkey")
    status = await p.probe()
    assert status.available is True


async def test_gemini_missing_key_unavailable():
    p = GeminiProvider(model="m", api_key=None)
    status = await p.probe()
    assert status.available is False
    assert status.reason == ProviderFailureReason.API_KEY_MISSING


async def test_gemini_rejects_video():
    from vision_mcp.models import VideoInput
    req = VisionRequest(system_prompt="s", user_prompt="u",
                        videos=[VideoInput(source="/tmp/v.mp4")])
    p = GeminiProvider(model="m", api_key="k", client=FakeClient())
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze(req)
    assert ei.value.reason == ProviderFailureReason.UNSUPPORTED_MEDIA