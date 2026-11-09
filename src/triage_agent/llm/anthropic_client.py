"""Claude via the official ``anthropic`` Python SDK.

Notes on the request shape:

* ``output_config.effort`` controls reasoning depth and spend. Current Opus
  models think adaptively by default, so ``thinking`` is not sent.
* Top-level ``cache_control`` asks the API to cache the longest stable prefix.
  The tool list and system prompt never change between runs, and each agent
  step only appends messages, so later steps re-read earlier ones from cache.
* The assistant turn is appended back verbatim (including thinking blocks).
* ``fallbacks="default"`` (server-side refusal fallback) is sent only for
  models known to support it; see ``FALLBACK_MODELS``.
* Forced ``tool_choice`` is not used: the system prompt tells the model to
  finish by calling ``submit_verdict`` and the loop nudges once if it does not.
"""

from __future__ import annotations

import time
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from triage_agent.config import Settings
from triage_agent.llm.base import LLMError, LLMResponse, ToolCall
from triage_agent.models import TokenUsage

FALLBACK_MODELS = frozenset(
    {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
)
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLM:
    def __init__(self, settings: Settings, client: AsyncAnthropic | None = None) -> None:
        self.model = settings.llm_model
        self.effort = settings.llm_effort
        self.max_tokens = settings.llm_max_tokens
        self.use_fallbacks = settings.llm_fallbacks == "default" and self.model in FALLBACK_MODELS
        # Credentials are resolved by the SDK (ANTHROPIC_API_KEY etc.); never passed in code.
        self._client = client or AsyncAnthropic(timeout=settings.llm_timeout_seconds, max_retries=2)

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": self.effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self.use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        started = time.perf_counter()
        try:
            response = await self._client.beta.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("authentication failed; check ANTHROPIC_API_KEY") from exc
        except anthropic.BadRequestError as exc:
            raise LLMError(f"request rejected: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("rate limited after SDK retries") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"API error {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("could not reach the Anthropic API") from exc

        tool_calls: list[ToolCall] = []
        texts: list[str] = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(ToolCall(id=block.id, name=block.name, input=block.input))
            elif block.type == "text":
                texts.append(block.text)

        u = response.usage
        usage = TokenUsage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_input_tokens=u.cache_read_input_tokens or 0,
            cache_creation_input_tokens=u.cache_creation_input_tokens or 0,
        )
        return LLMResponse(
            assistant_content=list(response.content),
            tool_calls=tool_calls,
            text="\n".join(texts),
            stop_reason=response.stop_reason or "unknown",
            usage=usage,
            model=response.model,
            request_id=getattr(response, "_request_id", None),
            extra={"latency_s": time.perf_counter() - started},
        )
