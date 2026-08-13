"""Protocol adapters: registry mapping user-facing protocol paths to adapters.

The protocol path (``openai/chat``, ``openai/responses``, ``anthropic``) is
explicit — never inferred from the target URL or the request body.
"""

from __future__ import annotations

from .anthropic import AnthropicAdapter
from .openai_chat import OpenAIChatAdapter
from .openai_responses import OpenAIResponsesAdapter
from .types import ProtocolAdapter


def build_registry() -> dict[str, ProtocolAdapter]:
    """Return the {protocol path: adapter} map."""
    return {a.path: a for a in (OpenAIChatAdapter(), OpenAIResponsesAdapter(), AnthropicAdapter())}