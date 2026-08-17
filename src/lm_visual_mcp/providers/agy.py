"""AGY provider (CLI).

Runs ``agy -p "<prompt>" --output-format json``. AGY headless has no first-class
image flag; images are staged into the workspace media dir and referenced by
bare filename in the prompt. Vision capability is discovered from real analyze
results and cached; if AGY genuinely produced no output (``no output
produced``), image requests raise UNSUPPORTED_MEDIA and the router falls back
to the next provider.

AGY reads images from the directory registered with ``--add-dir``. AGY ignores
the subprocess cwd and runs its tools in its own workspace, so the media dir must
be added explicitly with ``--add-dir`` (repeatable) and the server does not cd
into it. Files in an added directory are readable natively - no ``read_file``
grant and no ``command(ls)`` grant are needed, and the operator must never
configure ``command(*)``. The server additionally launches AGY in a sandbox
(``--sandbox``) so any command it runs is confined.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("lm_visual_mcp.vision.providers.agy")

from ..errors import ProviderUnavailableError
from .cli import CliProvider
from .json_output import extract_json
from .ratelimit import RateLimiter
from .runner import SubprocessInvocation, SubprocessResult
from .types import ImageRequest, ProviderFailureReason


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
        vision_cache_ttl: float = 300.0,
        limiter: Optional[RateLimiter] = None,
    ) -> None:
        super().__init__(
            command=command, model=model, effort=effort, timeout=timeout,
            runner=runner, limiter=limiter,
        )
        self._vision_capability: Optional[str] = None
        # Monotonic timestamp of when "unsupported" was last cached. When it ages
        # past ``vision_cache_ttl`` the poison expires and analyze() retries AGY
        # with a real call (so a transient permission issue doesn't permanently
        # exile AGY and push 100% of load onto the fallback providers).
        self._vision_unsupported_at: Optional[float] = None
        self.vision_cache_ttl = vision_cache_ttl

    # -- probe -------------------------------------------------------------
    async def check_vision_capability(self, request: Optional[ImageRequest]) -> str:
        """Return the cached AGY vision capability without running AGY.

        Deliberately does NOT trigger a probe here. analyze() always runs a real
        AGY call regardless of the probe result, so a separate probe would add a
        redundant AGY call on the first image request (2x). Capability is instead
        discovered from the real analyze result and cached in parse_output, so
        every image request is exactly one AGY call and unsupported is still
        fail-fast after the first real failure.
        """
        if request is None or not request.images:
            return "unknown"
        if self._cache_is_fresh():
            return "unsupported"
        return self._vision_capability or "unknown"

    def _cache_is_fresh(self) -> bool:
        """True only while the cached "unsupported" verdict is still within TTL."""
        if self._vision_capability != "unsupported" or self._vision_unsupported_at is None:
            return False
        return (time.monotonic() - self._vision_unsupported_at) < self.vision_cache_ttl

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
    async def _analyze_image(self, request: ImageRequest) -> object:
        # Every image request is exactly one real AGY call. AGY is
        # non-deterministic in headless mode; a runtime failure surfaces in
        # parse_output as UNSUPPORTED_MEDIA and caches here so subsequent image
        # requests fail fast while the router falls back to the next provider.
        if request.images and self._cache_is_fresh():
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "agy headless cannot read images (tool permission auto-denied)",
            )
        return await super()._analyze_image(request)

    @staticmethod
    def _media_dir(request: ImageRequest) -> Path:
        """Directory to register with ``--add-dir`` = the one holding the images.

        AGY ignores the subprocess cwd and always runs its tools in its own
        workspace, so the media dir must be added explicitly. Registering it
        makes the staged images readable by bare filename with no ``read_file``
        or ``command`` grant.
        """
        for img in request.images:
            if img.local_path:
                return Path(img.local_path).parent
        if request.workdir is not None:
            return request.workdir
        return Path.cwd()

    def _resolved_model(self) -> Optional[str]:
        """The model name AGY is actually invoked with.

        AGY has no separate effort parameter - effort is appended to the model
        name (``gemini-3.6-flash`` + effort ``high`` -> ``gemini-3.6-flash-high``).
        The config keeps ``model`` and ``effort`` as separate, convenient knobs;
        this method folds them together for the CLI. A model that already ends
        with the effort suffix is left untouched (no double suffix).
        """
        model = self.model
        if not model:
            return None
        if self.effort and not model.endswith(f"-{self.effort}"):
            return f"{model}-{self.effort}"
        return model

    def build_invocation(self, request: ImageRequest) -> SubprocessInvocation:
        media_dir = self._media_dir(request)
        prompt = self._media_instructions(request, base_dir=media_dir)
        user_prompt = (request.user_prompt + "\n\n" + prompt).strip()
        user_prompt = self._wrap_json_instruction(user_prompt, schema=bool(request.output_schema))

        args = ["-p", user_prompt, "--output-format", "json"]
        if request.output_schema:
            import json

            args += ["--json-schema", json.dumps(request.output_schema)]
        # AGY has no standalone `--effort` flag: reasoning effort is folded into
        # the model name (`gemini-3.6-flash` + effort high -> ...-high). Passing
        # a bare --effort alongside a suffixed model double-specifies it and has
        # been observed to fail with a quota/eligibility error.
        resolved = self._resolved_model()
        if resolved:
            args += ["--model", resolved]
        # Register the media dir so AGY can read the staged images by bare
        # filename. Files in an added dir are readable natively - no read_file
        # grant and no command(ls) grant required, and never command(*).
        args += ["--add-dir", str(media_dir)]
        # Run in a sandbox so any command AGY runs is confined.
        args += ["--sandbox"]

        # No cd into the media dir: AGY ignores the subprocess cwd (it runs its
        # tools in its own workspace) and reads the images via --add-dir, so a
        # forced cwd is redundant. Leave cwd unset (inherit the server's).
        return SubprocessInvocation(
            exe=self.command,
            args=args,
            cwd=None,
            timeout=request.timeout or self.timeout,
        )

    def parse_output(self, result: SubprocessResult, request: ImageRequest) -> dict:
        if request.images and self._looks_unsupported(result):
            self._vision_capability = "unsupported"
            self._vision_unsupported_at = time.monotonic()
            raise ProviderUnavailableError(
                ProviderFailureReason.UNSUPPORTED_MEDIA,
                "agy headless cannot read images (command-tool permission auto-denied)",
            )
        # A real AGY call produced output -> vision works, so clear any stale
        # "unsupported" verdict (records the recovery so fail-fast resumes on
        # the next genuine failure rather than an expired one).
        if self._vision_capability == "unsupported":
            logger.info("agy vision capability recovered; clearing unsupported cache")
            self._vision_capability = None
            self._vision_unsupported_at = None
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
        # rc=0 but no useful response (the pure non-determinism case, distinct from
        # the "no output produced" case handled above). Not an error - it returns
        # an empty answer - but log it so it is not silent.
        logger.warning(
            "agy returned no parseable output (rc=%s, stderr=%r)",
            result.returncode,
            (result.stderr or "")[:300],
        )
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
