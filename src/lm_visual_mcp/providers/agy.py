"""AGY provider.

Runs ``agy -p "<prompt>" --output-format json``. AGY headless has no first-class
image flag; images are staged into the workspace and referenced by relative
path in the prompt. Vision capability is probed once per process and cached; if
AGY genuinely produced no output (``no output produced``), image requests raise
UNSUPPORTED_MEDIA and the router falls back to the next provider.

AGY reads workspace images fine via ``--add-dir``. Headless mode auto-denies
``read_file`` / ``command`` because it cannot prompt for permission, so a run
can intermittently produce no output when the model reaches for those tools.
The server launches AGY in a sandbox (``--sandbox``) so that granting those
tools for the workspace is safe: any shell command AGY runs is confined to the
sandbox.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from typing import Optional

from ..errors import ProviderUnavailableError
from ..models import ProviderFailureReason, VisionRequest
from ..services.json_output import extract_json
from ..services.subprocess_runner import SubprocessInvocation, SubprocessResult
from .cli import CliProvider

# 1x1 transparent PNG embedded so capability probing needs no Pillow.
_ONE_PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "YAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)


class AgyProvider(CliProvider):
    name = "agy"
    default_command = "agy"

    def __init__(
        self,
        *,
        command: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout: float = 120.0,
        runner=None,
    ) -> None:
        super().__init__(command=command, model=model, effort=effort, timeout=timeout, runner=runner)
        self._vision_capability: Optional[str] = None

    # -- probe -------------------------------------------------------------
    async def check_vision_capability(self, request: Optional[VisionRequest]) -> str:
        if request is None or (not request.images and not request.videos):
            return "unknown"
        if self._vision_capability is not None:
            return self._vision_capability
        self._vision_capability = await self._probe_vision_once()
        return self._vision_capability

    async def _probe_vision_once(self) -> str:
        """Run AGY on a tiny image once to learn whether it can read images.

        Must mirror the real ``build_invocation`` exactly, including
        ``--add-dir``: AGY reads staged images relative to a workspace that is
        added to its allowed directories. Without ``--add-dir`` the probe would
        (wrongly) report unsupported even though the real path works.
        """
        with tempfile.TemporaryDirectory(prefix="agy-probe-") as td:
            probe_dir = Path(td)
            (probe_dir / "input").mkdir()
            img = probe_dir / "input" / "probe.png"
            img.write_bytes(_ONE_PX_PNG)
            invocation = SubprocessInvocation(
                exe=self.command,
                args=[
                    "-p",
                    "Look at the image input/probe.png and answer with the JSON {\"answer\":\"ok\"}.",
                    "--output-format",
                    "json",
                    "--add-dir",
                    str(probe_dir),
                    "--sandbox",
                ],
                cwd=probe_dir,
                timeout=min(self.timeout, 60.0),
            )
            result = await self.runner.run(invocation)
        if self._looks_unsupported(result):
            return "unsupported"
        if result.returncode == 0 and (result.stdout or result.stderr):
            return "available"
        return "unknown"

    @staticmethod
    def _looks_unsupported(result: SubprocessResult) -> bool:
        """True only when AGY headless produced no output at all.

        AGY's model frequently narrates a tool-permission denial it recovered
        from (e.g. "read_file was auto-denied, so I used view_file") while still
        returning a valid answer. A broad substring check for "permission" +
        "auto-denied" therefore misclassifies successful runs as unsupported and
        needlessly falls back. The one reliable signature of a genuine failure
        is AGY's own "no output produced" line.
        """
        combined = (result.stderr + "\n" + result.stdout).lower()
        return "no output produced" in combined

    # -- analyze -----------------------------------------------------------
    async def analyze(self, request: VisionRequest) -> object:
        # Always attempt a real AGY call. AGY can read workspace images (via
        # --add-dir); it is non-deterministic in headless mode and may
        # intermittently need a tool permission it cannot grant. Such a runtime
        # denial surfaces in parse_output as UNSUPPORTED_MEDIA and caches here
        # so subsequent image requests fail fast. We do NOT short-circuit a
        # probe result here — a single flaky probe run must not permanently
        # disable AGY for the process.
        if (request.images or request.videos) and self._vision_capability == "unsupported":
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "agy headless cannot read images (tool permission auto-denied)",
            )
        return await super().analyze(request)

    def build_invocation(self, request: VisionRequest) -> SubprocessInvocation:
        prompt = self._media_instructions(request)
        user_prompt = (request.user_prompt + "\n\n" + prompt).strip()
        user_prompt = self._wrap_json_instruction(user_prompt)

        args = ["-p", user_prompt, "--output-format", "json"]
        if request.output_schema:
            import json

            args += ["--json-schema", json.dumps(request.output_schema)]
        if self.model:
            args += ["--model", self.model]
        if self.effort:
            args += ["--effort", self.effort]
        if request.workdir is not None:
            args += ["--add-dir", str(request.workdir)]
        # Run in a sandbox so granting read_file/command for the workspace is
        # safe: any shell command AGY runs is confined rather than executed on
        # the host. Sandboxing alone does not auto-approve tools — the grants
        # live in AGY's permission config — but together they make headless
        # image reads reliable (the model may pick read_file or command).
        args += ["--sandbox"]

        return SubprocessInvocation(
            exe=self.command,
            args=args,
            cwd=request.workdir,
            timeout=request.timeout or self.timeout,
        )

    def parse_output(self, result: SubprocessResult, request: VisionRequest) -> dict:
        if (request.images or request.videos) and self._looks_unsupported(result):
            self._vision_capability = "unsupported"
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "agy headless cannot read images (command-tool permission auto-denied)",
            )
        if result.returncode != 0:
            reason = self._classify(result)
            raise ProviderUnavailableError(reason, f"agy exited with rc={result.returncode}")
        data = extract_json(result.stdout)
        if not isinstance(data, dict):
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                "agy output was not a JSON object",
            )
        if data.get("status") == "FAILURE":
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                f"agy reported failure: {data.get('error') or data.get('response') or ''}",
            )
        usage = data.get("usage") or {}
        result.usage = _select(usage, "input_tokens", "output_tokens", "thinking_tokens",
                               "cache_read_tokens", "total_tokens")
        structured = data.get("structured_output")
        if isinstance(structured, dict):
            return structured
        response = data.get("response")
        if isinstance(response, str) and response.strip():
            try:
                parsed = extract_json(response)
                if isinstance(parsed, dict):
                    return parsed
                return {"answer": response.strip()}
            except Exception:  # noqa: BLE001
                return {"answer": response.strip()}
        return {"answer": "", "warnings": ["agy returned no parseable output"]}

    @staticmethod
    def _classify(result: SubprocessResult) -> ProviderFailureReason:
        combined = (result.stderr + "\n" + result.stdout).lower()
        if "authentication" in combined or "not authenticated" in combined:
            return ProviderFailureReason.NOT_AUTHENTICATED
        if "quota" in combined or "rate limit" in combined:
            return ProviderFailureReason.QUOTA_EXHAUSTED
        if "permission" in combined:
            return ProviderFailureReason.PERMISSION_DENIED
        return ProviderFailureReason.TEMPORARY_FAILURE


def _select(mapping: dict, *keys) -> dict:
    return {k: mapping.get(k) for k in keys}