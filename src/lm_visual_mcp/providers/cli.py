"""Shared machinery for CLI-based providers (AGY, Codex, OpenCode)."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..errors import ProviderUnavailableError
from ..models import (
    ProviderFailureReason,
    ProviderResult,
    ProviderStatus,
    ProviderUsage,
    VisionRequest,
)
from ..schema import normalize_result
from ..services.subprocess_runner import (
    SubprocessInvocation,
    SubprocessResult,
    SubprocessRunner,
    classify_cli_failure,
)


class CliProvider:
    """Base class implementing common probe / analyze for CLI providers.

    Subclasses must implement :meth:`build_invocation` (how to run the CLI) and
    :meth:`parse_output` (how to turn the subprocess result into a dict).
    """

    name: str = "cli"
    default_command: str = ""

    def __init__(
        self,
        *,
        command: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        timeout: float = 120.0,
        runner: Optional[SubprocessRunner] = None,
    ) -> None:
        self.command = command or self.default_command
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.runner = runner or SubprocessRunner()

    # -- probe -------------------------------------------------------------
    async def probe(self, request: Optional[VisionRequest] = None) -> ProviderStatus:
        exe = self.runner.resolve_executable(self.command)
        if exe is None:
            return ProviderStatus(
                name=self.name,
                available=False,
                reason=ProviderFailureReason.COMMAND_NOT_FOUND,
                message=f"{self.name} executable not found: {self.command!r}",
            )
        try:
            vision = await self.check_vision_capability(request)
        except ProviderUnavailableError as exc:
            return ProviderStatus(
                name=self.name,
                available=False,
                reason=exc.reason,
                message=exc.message,
            )
        return ProviderStatus(
            name=self.name,
            available=True,
            model=self.model,
            vision_capability=vision,
        )

    async def check_vision_capability(self, request: Optional[VisionRequest]) -> str:
        """Return 'available' | 'unsupported' | 'unknown' for this request."""
        return "unknown"

    # -- analyze -----------------------------------------------------------
    async def analyze(self, request: VisionRequest) -> ProviderResult:
        invocation = self.build_invocation(request)
        result = await self.runner.run(invocation)
        return self.parse_result(result, request)

    def build_invocation(self, request: VisionRequest) -> SubprocessInvocation:
        raise NotImplementedError

    def parse_result(self, result: SubprocessResult, request: VisionRequest) -> ProviderResult:
        try:
            data = self.parse_output(result, request)
        except ProviderUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = classify_cli_failure(result)
            self._log_stderr(result)
            raise ProviderUnavailableError(
                reason or ProviderFailureReason.TEMPORARY_FAILURE,
                f"{self.name} returned unparseable output",
            ) from exc
        normalized = normalize_result(data)
        return ProviderResult(
            provider=self.name,
            result=normalized.to_dict(),
            model=self.model,
            usage=self._usage_from_result(result),
            raw=result.stdout,
        )

    def parse_output(self, result: SubprocessResult, request: VisionRequest) -> dict:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------
    def _usage_from_result(self, result: SubprocessResult) -> ProviderUsage:
        return ProviderUsage(**(result.usage or {}))

    def _log_stderr(self, result: SubprocessResult) -> None:
        import logging

        logging.getLogger("lm_visual_mcp.providers").debug(
            "%s stderr (rc=%s): %s", self.name, result.returncode, result.stderr[:2000]
        )

    @staticmethod
    def _media_instructions(request: VisionRequest, base_dir: Optional[Path] = None) -> str:
        """Build the media mapping block appended to the user prompt for CLI agents.

        ``base_dir`` is the directory the agent will run in (its cwd). When given,
        images are referenced relative to it (e.g. AGY cds into the media dir);
        otherwise they are referenced relative to ``request.workdir``.
        """
        lines = []
        for i, img in enumerate(request.images):
            if img.local_path:
                rel = _relative_to(Path(img.local_path), base_dir or request.workdir)
                lines.append(
                    f"IMAGE {i}: {rel}\n"
                    f"You MUST actually inspect this image file. "
                    f"Do not infer its contents from its filename."
                )
        if request.videos:
            lines.append(
                "VIDEO media is provided. If your backend cannot process video, "
                "report that instead of guessing."
            )
        if not lines:
            return ""
        return "\n\nAttached media to inspect:\n" + "\n\n".join(lines)

    @staticmethod
    def _wrap_json_instruction(prompt: str, *, schema: bool) -> str:
        """Append a JSON-instruction when a schema is actually enforced.

        The "matching exactly this schema" phrasing badly misdirects CLI models
        (notably AGY) when no schema is passed — it sends them reaching for a
        tool to produce the JSON and they error out. So only add it when the
        caller truly passes a schema (e.g. AGY ``--json-schema`` / codex
        ``--output-schema``); otherwise leave the prompt untouched and let the
        provider's ``--output-format json`` framing do the work.
        """
        if not schema:
            return prompt
        return (
            prompt
            + "\n\nYou MUST respond with a single JSON object matching exactly this schema. "
            "Return only the JSON, no prose, no markdown fences."
        )


def _relative_to(path: Path, base: Optional[Path]) -> str:
    try:
        if base is not None:
            return path.relative_to(base).as_posix()
    except ValueError:
        pass
    return path.as_posix()