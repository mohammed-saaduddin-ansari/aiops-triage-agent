"""Provider-neutral LLM interface used by the agent loop.

Messages use the Anthropic Messages API shape (it is the provider we ship),
but the agent only depends on this small protocol, so the FakeLLM and any
future provider plug in without touching the loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from triage_agent.models import TokenUsage


class LLMError(Exception):
    """The provider failed in a way the agent cannot recover from in this run."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass
class LLMResponse:
    # Appended verbatim as the assistant turn (keeps thinking blocks intact).
    assistant_content: list[Any]
    tool_calls: list[ToolCall]
    text: str
    stop_reason: str
    usage: TokenUsage
    model: str
    request_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class LLMClient(Protocol):
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse: ...
