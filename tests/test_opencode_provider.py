"""OpenCode provider tests (subprocess mocked)."""

from __future__ import annotations

from lm_visual_mcp.models import VisionRequest
from lm_visual_mcp.providers.opencode import OpenCodeProvider
from lm_visual_mcp.services.subprocess_runner import SubprocessResult


class FakeRunner:
    def __init__(self, result):
        self.result = result

    async def run(self, invocation):
        return self.result

    def resolve_executable(self, command):
        return "/usr/bin/" + command


def _req():
    return VisionRequest(system_prompt="s", user_prompt="u")


def test_opencode_extracts_assistant_text():
    stream = (
        '{"type":"message.part.updated","part":{"type":"text","text":"{\\"answer\\":\\"42\\"}"}}\n'
    )
    res = SubprocessResult("/usr/bin/opencode", [], 0, stream, "")
    p = OpenCodeProvider(command="opencode", runner=FakeRunner(res))
    r = p.parse_output(res, _req())
    assert r["answer"] == "42"


def test_opencode_invocation_flags():
    res = SubprocessResult("/usr/bin/opencode", [], 0, "", "")
    p = OpenCodeProvider(command="opencode", model="google/gemini", effort="high", runner=FakeRunner(res))
    inv = p.build_invocation(_req())
    assert inv.args[0] == "run"
    assert "--format" in inv.args and "json" in inv.args
    assert "--model" in inv.args
    assert "--variant" in inv.args and "high" in inv.args  # reasoning effort


def test_opencode_classify_permission_denied():
    from lm_visual_mcp.models import ProviderFailureReason
    res = SubprocessResult("/usr/bin/opencode", [], 1, "", "error: permission denied")
    assert OpenCodeProvider._classify(res) == ProviderFailureReason.PERMISSION_DENIED


def test_opencode_no_result_raises():
    res = SubprocessResult("/usr/bin/opencode", [], 0, '{"type":"ping"}', "")
    p = OpenCodeProvider(command="opencode", runner=FakeRunner(res))
    try:
        p.parse_output(res, _req())
        assert False, "expected ProviderUnavailableError"
    except Exception:
        pass