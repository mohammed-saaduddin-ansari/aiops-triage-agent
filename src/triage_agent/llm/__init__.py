"""LLM providers."""

from __future__ import annotations

from triage_agent.config import Settings
from triage_agent.llm.base import LLMClient, LLMError, LLMResponse, ToolCall
from triage_agent.llm.fake import FakeLLM

__all__ = ["FakeLLM", "LLMClient", "LLMError", "LLMResponse", "ToolCall", "build_llm"]


def build_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        from triage_agent.llm.anthropic_client import AnthropicLLM

        return AnthropicLLM(settings)
    return FakeLLM()
