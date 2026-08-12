"""OpenCode provider tests (subprocess mocked)."""

from __future__ import annotations

from vision_mcp.models import VisionRequest
from vision_mcp.providers.opencode import OpenCodeProvider
from vision_mcp.services.subprocess_runner import SubprocessResult


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
    p = OpenCodeProvider(command="opencode", model="google/gemini", runner=FakeRunner(res))
    inv = p.build_invocation(_req())
    assert inv.args[0] == "run"
    assert "--format" in inv.args and "json" in inv.args
    assert "--model" in inv.args


def test_opencode_no_result_raises():
    res = SubprocessResult("/usr/bin/opencode", [], 0, '{"type":"ping"}', "")
    p = OpenCodeProvider(command="opencode", runner=FakeRunner(res))
    try:
        p.parse_output(res, _req())
        assert False, "expected ProviderUnavailableError"
    except Exception:
        pass