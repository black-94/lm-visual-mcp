"""AGY provider tests (subprocess mocked)."""

from __future__ import annotations

import pytest

from vision_mcp.errors import ProviderUnavailableError
from vision_mcp.models import ImageInput, ProviderFailureReason, VisionRequest
from vision_mcp.providers.agy import AgyProvider
from vision_mcp.services.subprocess_runner import SubprocessResult


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def run(self, invocation):
        self.calls.append(invocation.args)
        return self.result

    def resolve_executable(self, command):
        return "/usr/bin/" + command


def _agy(result, **kw):
    return AgyProvider(command="agy", runner=FakeRunner(result), **kw)


def _req(images=True):
    imgs = [ImageInput(source="/tmp/x.png", local_path="/tmp/x.png", mime_type="image/png")] if images else []
    return VisionRequest(system_prompt="s", user_prompt="u", images=imgs)


def test_agy_text_parse_structured_top():
    ok = SubprocessResult("/usr/bin/agy", [], 0,
                          '{"status":"SUCCESS","response":"","structured_output":{"answer":"hi"}}', "")
    p = _agy(ok)
    # no images -> no vision probe
    r = p.parse_output(SubprocessResult("/usr/bin/agy", [], 0,
                        '{"status":"SUCCESS","response":"","structured_output":{"answer":"hi"}}', ""), _req(images=False))
    assert r["answer"] == "hi"


def test_agy_inputs_include_model_and_schema():
    ok = SubprocessResult("/usr/bin/agy", [], 0, "{}", "")
    p = AgyProvider(command="agy", model="gemini-x", runner=FakeRunner(ok))
    p._vision_capability = "unknown"
    inv = p.build_invocation(_req())
    assert "--model" in inv.args and "gemini-x" in inv.args
    assert "--output-format" in inv.args


def test_agy_unsupported_media_raises():
    ok = SubprocessResult("/usr/bin/agy", [], 0, "{}", "")
    p = AgyProvider(command="agy", runner=FakeRunner(ok))
    p._vision_capability = "unsupported"
    with pytest.raises(ProviderUnavailableError) as ei:
        import asyncio
        asyncio.run(p.analyze(_req()))
    assert ei.value.reason == ProviderFailureReason.UNSUPPORTED_MEDIA


def test_agy_probe_detects_unsupported():
    # AGY's genuine headless failure line always includes "no output produced".
    denied = SubprocessResult(
        "/usr/bin/agy", [], 0, "",
        'jetski: no output produced — a tool required the "read_file" permission '
        'that headless mode cannot prompt for, so it was auto-denied.',
    )
    p = _agy(denied)
    import asyncio
    cap = asyncio.run(p.check_vision_capability(_req()))
    assert cap == "unsupported"


def test_agy_success_with_permission_narration_is_not_unsupported():
    # AGY often recovers from a denied read_file by falling back to view_file and
    # still returns a valid answer, while its response narrates the denial. That
    # must NOT be classified as unsupported (regression for the false positive).
    ok = SubprocessResult(
        "/usr/bin/agy", [], 0,
        '{"status":"SUCCESS","response":"read_file was auto-denied, so I used '
        'view_file to read the image. INPUT shows VISION_TEST_7391."}', "",
    )
    p = _agy(ok)
    assert p._looks_unsupported(ok) is False
    out = p.parse_output(ok, _req())
    assert out["answer"] == "read_file was auto-denied, so I used view_file to read the image. INPUT shows VISION_TEST_7391."


def test_agy_failure_rc():
    bad = SubprocessResult("/usr/bin/agy", [], 1, "", "boom")
    p = _agy(bad)
    with pytest.raises(ProviderUnavailableError):
        p.parse_output(bad, _req(images=False))