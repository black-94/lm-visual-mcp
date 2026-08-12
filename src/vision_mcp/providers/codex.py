"""Codex provider.

Runs ``codex exec`` in read-only sandbox with ``--output-schema`` to force a
strict structured result. Images are passed natively via repeated ``-i`` flags.
"""

from __future__ import annotations

from typing import Optional

from ..errors import ProviderUnavailableError
from ..models import ProviderFailureReason, VisionRequest
from ..schema import build_codex_schema
from ..services.json_output import extract_json
from ..services.subprocess_runner import SubprocessInvocation, SubprocessResult
from .cli import CliProvider


class CodexProvider(CliProvider):
    name = "codex"
    default_command = "codex"

    def __init__(
        self,
        *,
        command: Optional[str] = None,
        model: Optional[str] = None,
        timeout: float = 120.0,
        runner=None,
        sandbox: str = "read-only",
    ) -> None:
        super().__init__(command=command, model=model, timeout=timeout, runner=runner)
        self.sandbox = sandbox

    async def check_vision_capability(self, request: Optional[VisionRequest]) -> str:
        # Codex supports images natively via -i.
        if request is None or (not request.images and not request.videos):
            return "unknown"
        return "available"

    def build_invocation(self, request: VisionRequest) -> SubprocessInvocation:
        args = ["exec"]
        for img in request.images:
            if img.local_path:
                args += ["-i", img.local_path]
        if request.videos:
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "codex does not accept video input via -i",
            )
        if self.model:
            args += ["-m", self.model]
        if request.workdir is not None:
            args += ["-C", str(request.workdir)]
        args += ["-s", self.sandbox, "--skip-git-repo-check"]
        if request.workdir is not None:
            schema_path = request.workdir / "schema.json"
            schema_path.write_text(_dump(build_codex_schema()), encoding="utf-8")
            args += ["--output-schema", str(schema_path)]

        prompt = self._media_instructions(request)
        user_prompt = (request.user_prompt + "\n\n" + prompt).strip()
        user_prompt = self._wrap_json_instruction(user_prompt)
        args.append(user_prompt)

        return SubprocessInvocation(
            exe=self.command,
            args=args,
            cwd=request.workdir,
            timeout=request.timeout or self.timeout,
        )

    def parse_output(self, result: SubprocessResult, request: VisionRequest) -> dict:
        if result.returncode != 0:
            raise ProviderUnavailableError(
                self._classify(result),
                f"codex exited with rc={result.returncode}",
            )
        data = extract_json(result.stdout)
        if not isinstance(data, dict):
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                "codex output was not a JSON object",
            )
        return data

    @staticmethod
    def _classify(result: SubprocessResult) -> ProviderFailureReason:
        combined = (result.stderr + "\n" + result.stdout).lower()
        if "not authenticated" in combined or "authentication required" in combined:
            return ProviderFailureReason.NOT_AUTHENTICATED
        if "quota" in combined or "rate limit" in combined or "429" in combined:
            return ProviderFailureReason.QUOTA_EXHAUSTED
        if "invalid_json_schema" in combined or "invalid_request_error" in combined:
            return ProviderFailureReason.INVALID_MODEL
        return ProviderFailureReason.TEMPORARY_FAILURE


def _dump(data: dict) -> str:
    import json

    return json.dumps(data, indent=2)