"""Codex provider tests (subprocess mocked)."""

from __future__ import annotations

import pytest

from lm_visual_mcp.models import ImageInput, VisionRequest
from lm_visual_mcp.providers.codex import CodexProvider
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


def _codex(result, **kw):
    return CodexProvider(command="codex", runner=FakeRunner(result), **kw)


def _req(n=1):
    imgs = [ImageInput(source=f"/tmp/x{i}.png", local_path=f"/tmp/x{i}.png")
            for i in range(n)]
    return VisionRequest(system_prompt="s", user_prompt="u", images=imgs)


def test_codex_parses_json_stdout():
    out = '{"summary":"s","answer":"VISION_TEST_7391","observations":[],"texts":[],"elements":[],"warnings":[]}'
    res = SubprocessResult("/usr/bin/codex", [], 0, out, "")
    p = _codex(res)
    r = p.parse_output(res, _req())
    assert r["answer"] == "VISION_TEST_7391"


def test_codex_invocation_has_images_and_readonly(tmp_path):
    res = SubprocessResult("/usr/bin/codex", [], 0, "{}", "")
    p = _codex(res, model="gpt-x")
    req = _req(n=2)
    req.workdir = tmp_path
    inv = p.build_invocation(req)
    assert inv.args.count("-i") == 2
    assert "-s" in inv.args and "read-only" in inv.args
    assert "--skip-git-repo-check" in inv.args
    assert "-m" in inv.args and "gpt-x" in inv.args
    assert "--output-schema" in inv.args
    # schema file written into workdir
    assert (tmp_path / "schema.json").exists()


def test_codex_failure_rc():
    res = SubprocessResult("/usr/bin/codex", [], 1, "", "boom")
    p = _codex(res)
    with pytest.raises(Exception):
        p.parse_output(res, _req())


def test_codex_rejects_video():
    from lm_visual_mcp.models import VideoInput
    req = VisionRequest(system_prompt="s", user_prompt="u", videos=[VideoInput(source="/tmp/v.mp4")])
    res = SubprocessResult("/usr/bin/codex", [], 0, "{}", "")
    p = _codex(res)
    with pytest.raises(Exception):
        p.build_invocation(req)


def test_codex_effort_reaches_invocation(tmp_path):
    from lm_visual_mcp.models import ProviderFailureReason
    res = SubprocessResult("/usr/bin/codex", [], 0, "{}", "")
    p = _codex(res, model="gpt-x", effort="high")
    req = _req()
    req.workdir = tmp_path
    inv = p.build_invocation(req)
    assert "-c" in inv.args and "model_reasoning_effort=high" in inv.args


def test_codex_classify_permission_denied():
    from lm_visual_mcp.models import ProviderFailureReason
    res = SubprocessResult("/usr/bin/codex", [], 1, "", "permission denied: read-only sandbox")
    assert CodexProvider._classify(res) == ProviderFailureReason.PERMISSION_DENIED