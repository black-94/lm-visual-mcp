"""Request hooks: the server's pluggable request/response interception layer.

A hook's basic interface is ``process``: it inspects (and may rewrite) a
request, and either lets it *continue* down the pipeline (optionally with a
modified body) or *intercepts* it by returning a response that goes straight
back to the client - the request is never forwarded upstream.

Hooks may also implement ``process_response`` to rewrite the upstream response
(used by the classifier hook to normalize verdict payloads). To see the
response body a hook sets ``ctx.state["read_response_body"] = True`` during
``process``; the server then buffers (and decompresses) the upstream body
instead of streaming it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Protocol, runtime_checkable

Action = Literal["continue", "intercept"]


@dataclass
class HookResponse:
    """A complete response returned to the client when a hook intercepts."""

    status: int
    headers: dict
    body: bytes


@dataclass
class HookResult:
    """Outcome of ``Hook.process``."""

    action: Action = "continue"
    # Replacement request body when ``action == "continue"`` (None = unchanged).
    body: Optional[bytes] = None
    # Response to send back when ``action == "intercept"``.
    response: Optional[HookResponse] = None

    @classmethod
    def passthrough(cls) -> "HookResult":
        return cls(action="continue")

    @classmethod
    def rewrite(cls, body: bytes) -> "HookResult":
        return cls(action="continue", body=body)

    @classmethod
    def intercept(cls, response: HookResponse) -> "HookResult":
        return cls(action="intercept", response=response)


@dataclass
class HookContext:
    """The live request state shared by the pipeline and the forwarder.

    ``state`` is per-request scratch space for hooks (e.g. the protocol path
    chosen by the router, classifier markers set during ``process`` and read
    during ``process_response``).
    """

    method: str
    url: str
    headers: dict
    body: bytes
    state: dict = field(default_factory=dict)


@runtime_checkable
class Hook(Protocol):
    """The basic hook interface."""

    name: str

    async def process(self, ctx: HookContext) -> HookResult:
        """Inspect / rewrite the request; continue or intercept it."""
        ...

    async def process_response(
        self, ctx: HookContext, status: int, headers: dict, body: bytes
    ) -> Optional[tuple[int, dict, bytes]]:
        """Optionally rewrite the upstream response. Returns None to keep it."""
        ...


class HookPipeline:
    """Runs the configured hooks in order."""

    def __init__(self, hooks: list[Hook]) -> None:
        self.hooks = list(hooks)

    async def run(self, ctx: HookContext) -> Optional[HookResponse]:
        """Apply every hook to the request.

        Returns a :class:`HookResponse` when a hook intercepted the request
        (upstream forwarding is skipped); ``None`` when the request should be
        forwarded (``ctx.body`` may have been rewritten along the way).
        """
        for hook in self.hooks:
            result = await hook.process(ctx)
            if result.action == "intercept":
                return result.response
            if result.body is not None:
                ctx.body = result.body
        return None

    async def run_response(
        self, ctx: HookContext, status: int, headers: dict, body: bytes
    ) -> tuple[int, dict, bytes]:
        """Apply every hook's response rewrite, in order."""
        for hook in self.hooks:
            out = await hook.process_response(ctx, status, headers, body)
            if out is not None:
                status, headers, body = out
        return status, headers, body
