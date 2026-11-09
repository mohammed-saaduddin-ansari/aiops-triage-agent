"""AnthropicLLM request shaping and response parsing, with the SDK client stubbed out."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from triage_agent.config import Settings
from triage_agent.llm.anthropic_client import FALLBACK_BETA, AnthropicLLM


class StubMessages:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text="Checking the rollout."),
                SimpleNamespace(
                    type="tool_use",
                    id="toolu_1",
                    name="k8s_rollout_history",
                    input={"namespace": "shop", "deployment": "cart"},
                ),
            ],
            stop_reason="tool_use",
            model=kwargs["model"],
            usage=SimpleNamespace(
                input_tokens=1200,
                output_tokens=150,
                cache_read_input_tokens=900,
                cache_creation_input_tokens=None,
            ),
        )


def stub_client() -> tuple[Any, StubMessages]:
    messages = StubMessages()
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


async def test_request_shape_and_parsing() -> None:
    client, stub = stub_client()
    llm = AnthropicLLM(Settings(llm_model="claude-opus-5-5", llm_effort="high"), client=client)
    resp = await llm.complete(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=[{"name": "t"}]
    )

    assert stub.kwargs["model"] == "claude-opus-5-5"
    assert stub.kwargs["output_config"] == {"effort": "high"}
    assert stub.kwargs["cache_control"] == {"type": "ephemeral"}
    assert stub.kwargs["fallbacks"] == "default"
    assert stub.kwargs["betas"] == [FALLBACK_BETA]
    assert "thinking" not in stub.kwargs
    assert "tool_choice" not in stub.kwargs

    assert [c.name for c in resp.tool_calls] == ["k8s_rollout_history"]
    assert resp.text == "Checking the rollout."
    assert len(resp.assistant_content) == 3, "thinking blocks are kept for the next turn"
    assert resp.usage.input_tokens == 1200
    assert resp.usage.cache_read_input_tokens == 900
    assert resp.usage.cache_creation_input_tokens == 0


@pytest.mark.parametrize(
    ("model", "setting"), [("claude-haiku-4-5", "default"), ("claude-opus-5-5", "off")]
)
async def test_fallbacks_only_sent_when_supported_and_enabled(model: str, setting: str) -> None:
    client, stub = stub_client()
    llm = AnthropicLLM(Settings(llm_model=model, llm_fallbacks=setting), client=client)  # type: ignore[arg-type]
    await llm.complete(system="s", messages=[], tools=[])
    assert "fallbacks" not in stub.kwargs
    assert "betas" not in stub.kwargs
