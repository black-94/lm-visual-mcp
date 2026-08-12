"""AGY provider tests (subprocess mocked)."""

from __future__ import annotations

import pytest

from lm_visual_mcp.errors import ProviderUnavailableError
from lm_visual_mcp.models import ImageInput, ProviderFailureReason, VisionRequest
from lm_visual_mcp.providers.agy import AgyProvider
from lm_visual_mcp.services.subprocess_runner import SubprocessResult


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


def test_agy_adds_media_dir_and_references_image(tmp_path):
    # AGY ignores the shell cwd and runs tools in its own workspace, so the
    # media dir must be registered with --add-dir. Once registered, files there
    # are readable natively (no read_file / command grant needed).
    img = tmp_path / "media-0.png"
    img.write_bytes(b"\x89PNG")
    req = VisionRequest(
        system_prompt="s", user_prompt="u",
        images=[ImageInput(source=str(img), local_path=str(img), mime_type="image/png")],
        workdir=tmp_path,
    )
    ok = SubprocessResult("/usr/bin/agy", [], 0, "{}", "")
    p = AgyProvider(command="agy", runner=FakeRunner(ok))
    inv = p.build_invocation(req)
    assert "--add-dir" in inv.args and str(tmp_path) in inv.args
    assert any("media-0.png" in a for a in inv.args)  # bare filename in prompt
    assert "--sandbox" in inv.args
    # No cd into the media dir: AGY ignores the shell cwd, reading via --add-dir.
    assert inv.cwd is None


def test_agy_model_and_effort_reach_invocation():
    ok = SubprocessResult("/usr/bin/agy", [], 0, "{}", "")
    p = AgyProvider(command="agy", model="gemini-3.6-flash", effort="high", runner=FakeRunner(ok))
    inv = p.build_invocation(_req())
    assert "--model" in inv.args and "gemini-3.6-flash" in inv.args
    assert "--effort" in inv.args and "high" in inv.args


def test_agy_unsupported_media_raises():
    ok = SubprocessResult("/usr/bin/agy", [], 0, "{}", "")
    p = AgyProvider(command="agy", runner=FakeRunner(ok))
    p._vision_capability = "unsupported"
    with pytest.raises(ProviderUnavailableError) as ei:
        import asyncio
        asyncio.run(p.analyze(_req()))
    assert ei.value.reason == ProviderFailureReason.UNSUPPORTED_MEDIA


def test_agy_check_vision_capability_does_not_invoke_agy():
    # Regression: the request path must not run a separate probe (that would
    # double the AGY call count on the first image request). check_vision_capability
    # only returns the cached value or "unknown".
    denied = SubprocessResult(
        "/usr/bin/agy", [], 0, "",
        'jetski: no output produced — a tool required the "read_file" permission '
        'that headless mode cannot prompt for, so it was auto-denied.',
    )
    p = _agy(denied)
    import asyncio
    assert asyncio.run(p.check_vision_capability(_req())) == "unknown"
    assert p.runner.calls == []  # no AGY invocation happened


def test_agy_analyze_detects_unsupported_and_caches():
    # Capability is discovered from the real analyze result: a genuine
    # "no output produced" failure caches "unsupported" and raises so later
    # image requests fail fast without another AGY call.
    denied = SubprocessResult(
        "/usr/bin/agy", [], 0, "",
        'jetski: no output produced — a tool required the "read_file" permission '
        'that headless mode cannot prompt for, so it was auto-denied.',
    )
    p = _agy(denied)
    import asyncio
    with pytest.raises(ProviderUnavailableError) as ei:
        asyncio.run(p.analyze(_req()))
    assert ei.value.reason == ProviderFailureReason.UNSUPPORTED_MEDIA
    assert p._vision_capability == "unsupported"


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


def test_agy_empty_response_is_not_silent(caplog):
    # rc=0 but an empty response must not be an unlogged silent failure: it
    # returns the empty-answer envelope AND emits a warning log line.
    empty = SubprocessResult(
        "/usr/bin/agy", [], 0,
        '{"status":"SUCCESS","response":"","usage":{}}', "",
    )
    p = _agy(empty)
    with caplog.at_level("WARNING", logger="lm_visual_mcp.providers.agy"):
        out = p.parse_output(empty, _req())
    assert out["answer"] == ""
    assert out["warnings"] == ["agy returned no parseable output"]
    assert any("no parseable output" in r.message for r in caplog.records)


def test_agy_classify_permission_denied():
    # A non-vision rc!=0 failure that mentions permission is PERMISSION_DENIED,
    # not UNSUPPORTED_MEDIA.
    res = SubprocessResult("/usr/bin/agy", [], 1, "", "permission denied")
    assert AgyProvider._classify(res) == ProviderFailureReason.PERMISSION_DENIED