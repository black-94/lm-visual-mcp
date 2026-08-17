"""CLI provider run-through tests (AGY, Codex).

Covers the "X runs through end-to-end" acceptance: an image-bearing request is
built into exactly one CLI invocation, the CLI output is parsed into the unified
result, and failure/fallback classification works. The subprocess is stubbed
with a :class:`FakeRunner` - the unit under test is the provider's
build_invocation + parse_output pipeline, not the OS process it spawns.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from lm_visual_mcp.errors import ProviderUnavailableError
from lm_visual_mcp.providers.agy import AgyProvider
from lm_visual_mcp.providers.codex import CodexProvider
from lm_visual_mcp.providers.runner import SubprocessResult
from lm_visual_mcp.providers.types import ImageInput, ImageRequest, ProviderFailureReason

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeRunner:
    """Records invocations, hands back canned SubprocessResults."""

    def __init__(self, results: list, *, resolve: str = "/usr/bin/fake-provider") -> None:
        self.results = list(results)
        self.runs: list = []
        self.resolve = resolve

    async def run(self, invocation):
        self.runs.append(invocation)
        return self.results.pop(0)

    def resolve_executable(self, command: str):
        return self.resolve


def make_request(tmp_path: Path, *, images: int = 1) -> ImageRequest:
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    imgs = []
    for i in range(images):
        p = media / f"img{i}.png"
        p.write_bytes(_PNG)
        imgs.append(ImageInput(source=str(p), local_path=str(p), mime_type="image/png"))
    # workdir is set: codex writes its --output-schema here, agy stages media here.
    return ImageRequest(
        system_prompt="sys",
        user_prompt="describe what you see",
        images=imgs,
        workdir=tmp_path,
    )


# -- AGY ---------------------------------------------------------------------


async def test_agy_success_runs_one_invocation(tmp_path):
    """One image -> exactly one AGY call, parsed into the unified result."""
    out = json.dumps({"response": '{"summary":"s","answer":"the answer"}',
                      "usage": {"input_tokens": 7, "output_tokens": 3}})
    runner = FakeRunner([SubprocessResult("agy", ["-p"], 0, out, "")])
    p = AgyProvider(runner=runner)

    result = await p.analyze_image(make_request(tmp_path))

    assert result.provider == "agy"
    assert result.result["answer"] == "the answer"
    # Exactly one AGY subprocess for the request (multi-image is one call too).
    assert len(runner.runs) == 1

    inv = runner.runs[0]
    args = inv.args
    assert args[0] == "-p"
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "json"
    # AGY reads staged media from the dir registered with --add-dir; sandbox on.
    media_dir = tmp_path / "media"
    assert "--add-dir" in args
    assert args[args.index("--add-dir") + 1] == str(media_dir)
    assert "--sandbox" in args
    # The prompt must tell the agent to inspect the actual image file.
    prompt = args[args.index("-p") + 1]
    assert "IMAGE 0:" in prompt and "MUST actually inspect" in prompt


async def test_agy_folds_effort_into_model_flag(tmp_path):
    """Effort is appended to the model name; no standalone --effort is emitted.

    AGY has no separate effort parameter, so `model=gemini-3.6-flash` +
    `effort=high` must invoke `--model gemini-3.6-flash-high` (never a bare
    `--effort` flag).
    """
    runner = FakeRunner([SubprocessResult(
        "agy", ["-p"], 0, json.dumps({"response": '{"answer":"a"}', "usage": {}}), "",
    )])
    p = AgyProvider(runner=runner, model="gemini-3.6-flash", effort="high")
    await p.analyze_image(make_request(tmp_path))
    args = runner.runs[0].args
    assert "--model" in args
    assert args[args.index("--model") + 1] == "gemini-3.6-flash-high"
    assert "--effort" not in args


async def test_agy_model_already_suffixed_not_double_appended(tmp_path):
    """A model that already carries the effort suffix is left untouched."""
    runner = FakeRunner([SubprocessResult(
        "agy", ["-p"], 0, json.dumps({"response": '{"answer":"a"}', "usage": {}}), "",
    )])
    p = AgyProvider(runner=runner, model="gemini-3.6-flash-medium", effort="medium")
    await p.analyze_image(make_request(tmp_path))
    args = runner.runs[0].args
    assert args[args.index("--model") + 1] == "gemini-3.6-flash-medium"
    assert args.count(args[args.index("--model") + 1]) == 1  # no -medium-medium
    assert "--effort" not in args


async def test_agy_multi_image_is_a_single_call(tmp_path):
    """Two images still cost exactly one AGY invocation (no per-image loop)."""
    runner = FakeRunner([SubprocessResult("agy", ["-p"], 0,
                                          json.dumps({"response": '{"answer":"m"}',
                                                      "usage": {}}),
                                          "")])
    p = AgyProvider(runner=runner)

    await p.analyze_image(make_request(tmp_path, images=2))

    assert len(runner.runs) == 1
    assert len(runner.runs[0].args) >= 1  # the -p prompt carries both images' block
    prompt = runner.runs[0].args[runner.runs[0].args.index("-p") + 1]
    assert "IMAGE 0:" in prompt and "IMAGE 1:" in prompt


async def test_agy_unsupported_fails_fast_after_first_run(tmp_path):
    """'no output produced' marks agy image-unsupported; later calls skip the CLI."""
    runner = FakeRunner(
        [SubprocessResult("agy", ["-p"], 0, "no output produced", "")]
    )
    p = AgyProvider(runner=runner)

    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason == ProviderFailureReason.UNSUPPORTED_MEDIA

    # A second image request fails fast without spawning AGY again (TTL fresh).
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path / "other"))
    assert ei.value.reason == ProviderFailureReason.UNSUPPORTED_MEDIA
    assert len(runner.runs) == 1  # cached verdict; no second subprocess


async def test_agy_quota_classified(tmp_path):
    runner = FakeRunner([SubprocessResult("agy", ["-p"], 1,
                                          "", "rate limit exceeded")])
    p = AgyProvider(runner=runner)
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason == ProviderFailureReason.QUOTA_EXHAUSTED


# -- Codex -------------------------------------------------------------------


async def test_codex_success_runs_one_invocation(tmp_path):
    """Codex passes images natively via -i and parses the strict JSON result."""
    out = json.dumps({"summary": "s", "answer": "the answer"})
    runner = FakeRunner([SubprocessResult("codex", ["exec"], 0, out, "")])
    p = CodexProvider(runner=runner)

    result = await p.analyze_image(make_request(tmp_path))

    assert result.provider == "codex"
    assert result.result["answer"] == "the answer"
    assert len(runner.runs) == 1

    args = runner.runs[0].args
    assert args[0] == "exec"
    img = tmp_path / "media" / "img0.png"
    assert "-i" in args and str(img) in args
    assert "-s" in args and args[args.index("-s") + 1] == "read-only"
    assert "--skip-git-repo-check" in args
    # The strict codex schema is materialized next to the workdir.
    schema = tmp_path / "schema.json"
    assert schema.exists()
    assert "--output-schema" in args
    assert args[args.index("--output-schema") + 1] == str(schema)
    assert "MUST actually inspect" in args[-1]


async def test_codex_quota_classified(tmp_path):
    runner = FakeRunner([SubprocessResult("codex", ["exec"], 1,
                                          "", "rate limit 429")])
    p = CodexProvider(runner=runner)
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason == ProviderFailureReason.QUOTA_EXHAUSTED


async def test_codex_unparseable_output_is_temporary_failure(tmp_path):
    runner = FakeRunner([SubprocessResult("codex", ["exec"], 0, "definitely not json", "")])
    p = CodexProvider(runner=runner)
    with pytest.raises(ProviderUnavailableError) as ei:
        await p.analyze_image(make_request(tmp_path))
    assert ei.value.reason in (ProviderFailureReason.TEMPORARY_FAILURE,)