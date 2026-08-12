"""OpenCode provider.

Runs ``opencode run`` with ``--format json`` and parses the JSONL event stream,
extracting the final assistant text result. Images are attached via repeated
``--file`` flags.
"""

from __future__ import annotations

import json
from typing import Optional

from ..errors import ProviderUnavailableError
from ..models import ProviderFailureReason, VisionRequest
from ..services.json_output import extract_json
from ..services.subprocess_runner import SubprocessInvocation, SubprocessResult
from .cli import CliProvider


class OpenCodeProvider(CliProvider):
    name = "opencode"
    default_command = "opencode"

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

    async def check_vision_capability(self, request: Optional[VisionRequest]) -> str:
        if request is None or (not request.images and not request.videos):
            return "unknown"
        return "available"

    def build_invocation(self, request: VisionRequest) -> SubprocessInvocation:
        args = ["run"]
        for img in request.images:
            if img.local_path:
                args += ["--file", img.local_path]
        if request.videos:
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "opencode does not accept video via --file",
            )
        if self.model:
            args += ["--model", self.model]
        if request.workdir is not None:
            args += ["--dir", str(request.workdir)]
        if self.effort:
            args += ["--variant", self.effort]
        args += ["--format", "json"]

        prompt = self._media_instructions(request)
        user_prompt = (request.user_prompt + "\n\n" + prompt).strip()
        user_prompt = self._wrap_json_instruction(user_prompt, schema=False)
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
                f"opencode exited with rc={result.returncode}",
            )
        text = self._extract_assistant_text(result.stdout)
        if not text:
            raise ProviderUnavailableError(
                ProviderFailureReason.TEMPORARY_FAILURE,
                "opencode produced no assistant result",
            )
        try:
            data = extract_json(text)
        except Exception:  # noqa: BLE001
            data = {"answer": text.strip()}
        if not isinstance(data, dict):
            data = {"answer": str(data) if data else text.strip()}
        return data

    @staticmethod
    def _extract_assistant_text(stream: str) -> Optional[str]:
        parts: list[str] = []
        texts: list[str] = []
        for line in stream.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "message.part.updated":
                part = event.get("part") or {}
                if part.get("type") == "text" and part.get("text"):
                    texts.append(part["text"])
            elif etype == "message.updated":
                msg = event.get("message") or {}
                for content in msg.get("content", []) or []:
                    if content.get("type") == "text" and content.get("text"):
                        texts.append(content["text"])
            elif etype == "message.part.created":
                parts.append(line)
        # Prefer completed text parts; fall back to assembled text.
        return "".join(texts) if texts else None

    @staticmethod
    def _classify(result: SubprocessResult) -> ProviderFailureReason:
        combined = (result.stderr + "\n" + result.stdout).lower()
        if "not authenticated" in combined or ("login" in combined and "required" in combined):
            return ProviderFailureReason.NOT_AUTHENTICATED
        if "quota" in combined or "rate limit" in combined:
            return ProviderFailureReason.QUOTA_EXHAUSTED
        if "permission" in combined and "denied" in combined:
            return ProviderFailureReason.PERMISSION_DENIED
        return ProviderFailureReason.TEMPORARY_FAILURE