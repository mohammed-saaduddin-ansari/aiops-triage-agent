from __future__ import annotations

from typing import Any

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tests.conftest import RUNBOOK_DIR, scenario
from triage_agent.agent import TriageAgent, estimate_cost
from triage_agent.config import Settings
from triage_agent.correlation import Correlator
from triage_agent.llm import FakeLLM, LLMError, LLMResponse, ToolCall
from triage_agent.models import AlertGroup, CauseCategory, TokenUsage, TriageOutcome
from triage_agent.scenarios import NOW, fixture_service
from triage_agent.telemetry import setup_tracing

EXPORTER = InMemorySpanExporter()
setup_tracing(EXPORTER)


def group_for(name: str, settings: Settings) -> AlertGroup:
    corr = Correlator(settings.correlation_group_by, 300, clock=lambda: NOW)
    return corr.ingest(scenario(name).alerts).new_groups[0]


def agent_for(name: str, llm: Any, settings: Settings) -> TriageAgent:
    service, *_ = fixture_service(scenario(name), llm, settings, runbook_dir=RUNBOOK_DIR)
    return service.agent


class ScriptedLLM:
    """Plays back a fixed list of responses (tool calls), then repeats the last one."""

    model = "scripted"

    def __init__(self, turns: list[list[ToolCall]], stop_reason: str = "tool_use") -> None:
        self.turns = turns
        self.stop_reason = stop_reason
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def complete(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> LLMResponse:
        self.seen.append(list(messages))
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        content = [
            {"type": "tool_use", "id": f"{c.id}_{self.calls}", "name": c.name, "input": c.input}
            for c in turn
        ]
        calls = [ToolCall(id=f"{c.id}_{self.calls}", name=c.name, input=c.input) for c in turn]
        return LLMResponse(
            assistant_content=content,
            tool_calls=calls,
            text="",
            stop_reason=self.stop_reason if turn else "end_turn",
            usage=TokenUsage(input_tokens=1000, output_tokens=100),
            model=self.model,
        )


async def test_fake_llm_triage_completes_with_verified_evidence(settings: Settings) -> None:
    EXPORTER.clear()
    agent = agent_for("bad-deploy-error-spike", FakeLLM(), settings)
    result = await agent.triage(group_for("bad-deploy-error-spike", settings))
    assert result.outcome is TriageOutcome.COMPLETED
    assert result.verdict.cause_category is CauseCategory.BAD_DEPLOY
    assert result.verdict.evidence
    assert all(e.verified for e in result.verdict.evidence)
    assert all(e.query for e in result.verdict.evidence), "harness fills in queries"
    queries = [c.input["query"] for c in result.tool_calls if c.name == "prometheus_query"]
    assert len(queries) == 1, "runs the check from the top runbook only"
    assert "sum by (version)" in queries[0]

    spans = {s.name for s in EXPORTER.get_finished_spans()}
    assert {"triage.run", "llm.call", "tool.runbook_lookup", "tool.k8s_rollout_history"} <= spans
    llm_span = next(s for s in EXPORTER.get_finished_spans() if s.name == "llm.call")
    assert llm_span.attributes is not None
    assert llm_span.attributes["gen_ai.usage.input_tokens"] > 0


async def test_step_budget_produces_fallback_verdict(settings: Settings) -> None:
    looping = ScriptedLLM([[ToolCall("c", "runbook_lookup", {"alertname": "HighErrorRate"})]])
    settings = settings.model_copy(update={"max_steps": 3})
    agent = agent_for("bad-deploy-error-spike", looping, settings)
    result = await agent.triage(group_for("bad-deploy-error-spike", settings))
    assert result.outcome is TriageOutcome.BUDGET_EXHAUSTED
    assert result.steps == 3
    assert result.verdict.cause_category is CauseCategory.UNKNOWN
    assert result.verdict.confidence == 0.0
    # The final-step note was sent before the last call.
    last_user = looping.seen[-1][-1]
    assert any(b.get("type") == "text" and "final step" in b["text"] for b in last_user["content"])


async def test_token_budget_stops_the_loop(settings: Settings) -> None:
    looping = ScriptedLLM([[ToolCall("c", "runbook_lookup", {"alertname": "x"})]])
    settings = settings.model_copy(update={"token_budget": 2000, "max_steps": 20})
    result = await agent_for("bad-deploy-error-spike", looping, settings).triage(
        group_for("bad-deploy-error-spike", settings)
    )
    assert result.outcome is TriageOutcome.BUDGET_EXHAUSTED
    assert result.steps == 2


async def test_tool_call_budget_is_enforced(settings: Settings) -> None:
    many = [ToolCall(f"c{i}", "runbook_lookup", {"alertname": "x"}) for i in range(10)]
    settings = settings.model_copy(update={"max_tool_calls": 4, "max_steps": 2})
    result = await agent_for("bad-deploy-error-spike", ScriptedLLM([many]), settings).triage(
        group_for("bad-deploy-error-spike", settings)
    )
    assert len(result.tool_calls) == 4


async def test_verdict_citing_unknown_call_is_rejected_then_accepted(settings: Settings) -> None:
    verdict = {
        "summary": "s",
        "suspected_cause": "c",
        "cause_category": "bad_deploy",
        "confidence": 0.6,
        "severity_suggestion": "high",
        "next_steps": [],
    }
    llm = ScriptedLLM(
        [
            [ToolCall("rb", "runbook_lookup", {"alertname": "HighErrorRate"})],
            [
                ToolCall(
                    "v",
                    "submit_verdict",
                    {**verdict, "evidence": [{"tool_call_id": "made_up", "summary": "x"}]},
                )
            ],
            [
                ToolCall(
                    "v",
                    "submit_verdict",
                    {**verdict, "evidence": [{"tool_call_id": "rb_1", "summary": "runbook"}]},
                )
            ],
        ]
    )
    result = await agent_for("bad-deploy-error-spike", llm, settings).triage(
        group_for("bad-deploy-error-spike", settings)
    )
    assert result.outcome is TriageOutcome.COMPLETED
    assert result.steps == 3
    rejection = llm.seen[2][-1]["content"][0]
    assert rejection["is_error"]
    assert "never executed" in rejection["content"]
    assert result.verdict.evidence[0].verified


async def test_model_that_never_calls_submit_verdict_is_nudged_once(settings: Settings) -> None:
    llm = ScriptedLLM([[]])
    result = await agent_for("bad-deploy-error-spike", llm, settings).triage(
        group_for("bad-deploy-error-spike", settings)
    )
    assert result.outcome is TriageOutcome.NO_VERDICT
    assert result.steps == 2
    assert "submit_verdict" in llm.seen[1][-1]["content"]


async def test_refusal_and_provider_errors_fall_back(settings: Settings) -> None:
    refusing = ScriptedLLM(
        [[ToolCall("c", "runbook_lookup", {"alertname": "x"})]], stop_reason="refusal"
    )
    result = await agent_for("bad-deploy-error-spike", refusing, settings).triage(
        group_for("bad-deploy-error-spike", settings)
    )
    assert result.outcome is TriageOutcome.REFUSED

    class Down(ScriptedLLM):
        async def complete(self, **_: Any) -> LLMResponse:
            raise LLMError("unreachable")

    result = await agent_for("bad-deploy-error-spike", Down([[]]), settings).triage(
        group_for("bad-deploy-error-spike", settings)
    )
    assert result.outcome is TriageOutcome.ERROR
    assert result.verdict.cause_category is CauseCategory.UNKNOWN


async def test_secrets_never_reach_the_llm(settings: Settings) -> None:
    llm = FakeLLM()
    sc = scenario("secret-in-logs-credential-rotation")
    agent = agent_for(sc.id, llm, settings)
    await agent.triage(group_for(sc.id, settings))
    transcript = str(llm.requests)
    for secret in sc.secrets:
        assert secret not in transcript


def test_cost_estimate_needs_configured_prices(settings: Settings) -> None:
    usage = TokenUsage(
        input_tokens=1_000_000, output_tokens=100_000, cache_read_input_tokens=500_000
    )
    assert estimate_cost(usage, settings) is None
    priced = settings.model_copy(
        update={
            "price_input_per_mtok": 2.0,
            "price_output_per_mtok": 10.0,
            "price_cache_read_per_mtok": 0.5,
        }
    )
    assert estimate_cost(usage, priced) == pytest.approx(2.0 + 1.0 + 0.25)
