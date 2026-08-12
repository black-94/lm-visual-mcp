"""Safe subprocess execution for CLI providers.

Uses argument arrays (never ``shell=True``) and refuses to interpolate raw user
input into commands. All paths are passed as separate argv entries so no
shell-escaping is needed.
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..errors import ProviderError, ProviderUnavailableError
from ..models import ProviderFailureReason


@dataclass
class SubprocessInvocation:
    exe: str
    args: list[str]
    cwd: Optional[Path] = None
    timeout: float = 120.0


@dataclass
class SubprocessResult:
    command: str
    args: list[str]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    usage: dict = field(default_factory=dict)


class SubprocessRunner:
    """Runs CLI providers with a bounded timeout and captures stdout/stderr."""

    async def run(self, invocation: SubprocessInvocation) -> SubprocessResult:
        exe = invocation.exe
        resolved = self.resolve_executable(exe)
        if resolved is None:
            raise ProviderUnavailableError(
                ProviderFailureReason.COMMAND_NOT_FOUND,
                f"executable not found: {exe!r}",
            )

        cmd = [str(resolved), *invocation.args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(invocation.cwd) if invocation.cwd else None,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise ProviderUnavailableError(
                ProviderFailureReason.COMMAND_NOT_FOUND,
                f"failed to launch {exe!r}: {exc}",
            ) from exc

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=invocation.timeout
            )
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            stdout_bytes, stderr_bytes = b"", b""
            timed_out = True

        return SubprocessResult(
            command=str(resolved),
            args=invocation.args,
            returncode=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            timed_out=timed_out,
        )

    @staticmethod
    def resolve_executable(command: str) -> Optional[str]:
        if "/" in command or (Path(command).is_file()):
            # Absolute or explicit path.
            return command if Path(command).is_file() else None
        found = shutil.which(command)
        return found


def classify_cli_failure(result: SubprocessResult) -> Optional[ProviderFailureReason]:
    """Map a CLI run's outcome to a fallback reason (or ``None`` if OK)."""
    if result.timed_out:
        return ProviderFailureReason.TIMEOUT
    if result.returncode == 0:
        return None
    combined = (result.stderr + "\n" + result.stdout).lower()
    if "not authenticated" in combined or "authentication required" in combined:
        return ProviderFailureReason.NOT_AUTHENTICATED
    if "api key" in combined and ("invalid" in combined or "missing" in combined):
        return ProviderFailureReason.API_KEY_MISSING
    if any(k in combined for k in ("quota", "rate limit", "resource exhausted", "429")):
        return ProviderFailureReason.QUOTA_EXHAUSTED
    if "not found" in combined and "command" in combined:
        return ProviderFailureReason.COMMAND_NOT_FOUND
    if "permission denied" in combined or ("permission" in combined and "denied" in combined):
        return ProviderFailureReason.PERMISSION_DENIED
    return ProviderFailureReason.TEMPORARY_FAILURE