"""The triage agent loop.

One run = one correlated alert group. The loop is written by hand rather than
using an SDK tool runner because each step has to enforce things a generic
runner does not: step, tool-call and token budgets; parallel tool execution
behind the registry's guard-rails; a span per step; verdict validation with
citation checks; and a deterministic fallback when the model does not finish.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from opentelemetry import trace
from pydantic import ValidationError

from triage_agent import metrics
from triage_agent.config import Settings
from triage_agent.correlation import utcnow
from triage_agent.llm.base import LLMClient, LLMError, LLMResponse, ToolCall
from triage_agent.models import (
    AlertGroup,
    CauseCategory,
    Evidence,
    Severity,
    TokenUsage,
    ToolCallRecord,
    TriageOutcome,
    TriageResult,
    Verdict,
)
from triage_agent.prompts import (
    FINAL_STEP_NOTE,
    NUDGE_NOTE,
    SUBMIT_VERDICT,
    SUBMIT_VERDICT_TOOL,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from triage_agent.security import new_nonce, redact
from triage_agent.tools.base import ExecutedCall, ToolRegistry

tracer = trace.get_tracer(__name__)


def estimate_cost(usage: TokenUsage, settings: Settings) -> float | None:
    """Cost from configured per-MTok prices. Returns None unless prices are set."""
    if settings.price_input_per_mtok is None or settings.price_output_per_mtok is None:
        return None
    cache_read = settings.price_cache_read_per_mtok or 0.0
    cache_write = settings.price_cache_write_per_mtok or 0.0
    total = (
        usage.input_tokens * settings.price_input_per_mtok
        + usage.output_tokens * settings.price_output_per_mtok
        + usage.cache_read_input_tokens * cache_read
        + usage.cache_creation_input_tokens * cache_write
    ) / 1_000_000
    return round(total, 6)


class TriageAgent:
    def __init__(
        self,
        llm: LLMClient,
        registry: ToolRegistry,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.settings = settings
        self.clock = clock

    async def triage(self, group: AlertGroup, triage_id: str | None = None) -> TriageResult:
        triage_id = triage_id or uuid.uuid4().hex[:12]
        started_at = self.clock()
        t0 = time.perf_counter()
        nonce = new_nonce()
        tools = [*self.registry.specs(), SUBMIT_VERDICT_TOOL]
        user_prompt, _ = build_user_prompt(
            group,
            nonce=nonce,
            now=started_at,
            tool_names=self.registry.names,
            max_steps=self.settings.max_steps,
            max_tool_calls=self.settings.max_tool_calls,
        )
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
        usage = TokenUsage()
        records: list[ToolCallRecord] = []
        executed: dict[str, ExecutedCall] = {}
        verdict: Verdict | None = None
        outcome: TriageOutcome | None = None
        steps = 0
        nudged = False

        with tracer.start_as_current_span("triage.run") as run_span:
            run_span.set_attribute("triage.id", triage_id)
            run_span.set_attribute("triage.group_id", group.id)
            run_span.set_attribute("triage.alertname", group.primary.name)
            run_span.set_attribute("triage.alert_count", len(group.alerts))

            while outcome is None:
                if steps >= self.settings.max_steps or usage.total >= self.settings.token_budget:
                    outcome = TriageOutcome.BUDGET_EXHAUSTED
                    break
                steps += 1
                try:
                    response = await self._call_llm(messages, tools, steps)
                except LLMError as exc:
                    run_span.record_exception(exc)
                    outcome = TriageOutcome.ERROR
                    break
                usage.add(response.usage)
                messages.append({"role": "assistant", "content": response.assistant_content})

                if response.stop_reason == "refusal":
                    outcome = TriageOutcome.REFUSED
                    break
                if not response.tool_calls:
                    if nudged:
                        outcome = TriageOutcome.NO_VERDICT
                        break
                    nudged = True
                    messages.append({"role": "user", "content": NUDGE_NOTE})
                    continue

                results, verdict = await self._handle_tool_calls(
                    response.tool_calls, nonce, records, executed
                )
                if verdict is not None:
                    outcome = TriageOutcome.COMPLETED
                    break

                last_step = steps + 1 >= self.settings.max_steps
                near_budget = usage.total >= 0.85 * self.settings.token_budget
                if last_step or near_budget:
                    results.append({"type": "text", "text": FINAL_STEP_NOTE})
                messages.append({"role": "user", "content": results})

            if verdict is None:
                verdict = fallback_verdict(group, records, outcome)
            verdict = self._finalise(verdict, executed)

            run_span.set_attribute("triage.outcome", outcome.value)
            run_span.set_attribute("triage.steps", steps)
            run_span.set_attribute("triage.tool_calls", len(records))
            run_span.set_attribute("triage.cause_category", verdict.cause_category.value)
            run_span.set_attribute("triage.confidence", verdict.confidence)
            run_span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            run_span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)

        metrics.TRIAGE_RUNS.labels(outcome=outcome.value).inc()
        metrics.TRIAGE_DURATION.observe(time.perf_counter() - t0)
        metrics.TRIAGE_STEPS.observe(steps)
        return TriageResult(
            triage_id=triage_id,
            group_id=group.id,
            outcome=outcome,
            verdict=verdict,
            steps=steps,
            tool_calls=records,
            usage=usage,
            model=self.llm.model,
            started_at=started_at,
            finished_at=self.clock(),
            estimated_cost_usd=estimate_cost(usage, self.settings),
        )

    # ------------------------------------------------------------------ steps

    async def _call_llm(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], step: int
    ) -> LLMResponse:
        with tracer.start_as_current_span("llm.call") as span:
            span.set_attribute(
                "gen_ai.system", "anthropic" if "claude" in self.llm.model else "fake"
            )
            span.set_attribute("gen_ai.request.model", self.llm.model)
            span.set_attribute("triage.step", step)
            t0 = time.perf_counter()
            response = await self.llm.complete(system=SYSTEM_PROMPT, messages=messages, tools=tools)
            elapsed = time.perf_counter() - t0
            u = response.usage
            span.set_attribute("gen_ai.response.model", response.model)
            span.set_attribute("gen_ai.response.finish_reason", response.stop_reason)
            span.set_attribute("gen_ai.usage.input_tokens", u.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", u.output_tokens)
            span.set_attribute("gen_ai.usage.cache_read_input_tokens", u.cache_read_input_tokens)
            span.set_attribute(
                "gen_ai.usage.cache_creation_input_tokens", u.cache_creation_input_tokens
            )
            span.set_attribute("gen_ai.usage.estimated", u.estimated)
            span.set_attribute("triage.tool_calls_requested", len(response.tool_calls))
            if response.request_id:
                span.set_attribute("gen_ai.response.id", response.request_id)
            metrics.LLM_CALLS.labels(model=response.model, stop_reason=response.stop_reason).inc()
            metrics.LLM_LATENCY.labels(model=response.model).observe(elapsed)
            for kind, value in (
                ("input", u.input_tokens),
                ("output", u.output_tokens),
                ("cache_read", u.cache_read_input_tokens),
                ("cache_write", u.cache_creation_input_tokens),
            ):
                metrics.LLM_TOKENS.labels(model=response.model, kind=kind).inc(value)
            return response

    async def _handle_tool_calls(
        self,
        calls: list[ToolCall],
        nonce: str,
        records: list[ToolCallRecord],
        executed: dict[str, ExecutedCall],
    ) -> tuple[list[dict[str, Any]], Verdict | None]:
        results: dict[str, dict[str, Any]] = {}
        runnable: list[ToolCall] = []
        verdict_call: ToolCall | None = None

        for call in calls:
            if call.name == SUBMIT_VERDICT:
                if verdict_call is None:
                    verdict_call = call
                else:
                    results[call.id] = _error_result(
                        call.id, "submit_verdict may only be called once"
                    )
                continue
            if len(records) + len(runnable) >= self.settings.max_tool_calls:
                results[call.id] = _error_result(
                    call.id, "tool-call budget exhausted; call submit_verdict with what you have"
                )
                continue
            runnable.append(call)

        outcomes = await asyncio.gather(
            *(
                self.registry.execute(call_id=c.id, name=c.name, args=c.input, nonce=nonce)
                for c in runnable
            )
        )
        for call, done in zip(runnable, outcomes, strict=True):
            records.append(done.record)
            executed[call.id] = done
            results[call.id] = {
                "type": "tool_result",
                "tool_use_id": call.id,
                "content": done.content,
                "is_error": done.is_error,
            }

        verdict: Verdict | None = None
        if verdict_call is not None:
            verdict, problem = _parse_verdict(verdict_call.input, executed)
            if verdict is None:
                results[verdict_call.id] = _error_result(verdict_call.id, problem)
            else:
                results[verdict_call.id] = {
                    "type": "tool_result",
                    "tool_use_id": verdict_call.id,
                    "content": "verdict recorded",
                }
        # Keep the order of the model's calls; the API expects every id answered.
        return [results[c.id] for c in calls], verdict

    def _finalise(self, verdict: Verdict, executed: dict[str, ExecutedCall]) -> Verdict:
        """Mark which evidence the harness can vouch for and fill in queries/links."""
        evidence: list[Evidence] = []
        for item in verdict.evidence:
            done = executed.get(item.tool_call_id)
            summary, _ = redact(item.summary)
            evidence.append(
                item.model_copy(
                    update={
                        "summary": summary,
                        "verified": done is not None and not done.is_error,
                        "query": item.query or (done.query if done else None),
                        "link": item.link or (done.link if done else None),
                    }
                )
            )
        return verdict.model_copy(update={"evidence": evidence})


def _error_result(call_id: str, message: str) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": f"ERROR: {message}",
        "is_error": True,
    }


def _parse_verdict(raw: Any, executed: dict[str, ExecutedCall]) -> tuple[Verdict | None, str]:
    try:
        verdict = Verdict.model_validate(raw)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]
        )
        return None, f"verdict failed validation ({details}); fix and call submit_verdict again"
    unknown = sorted({e.tool_call_id for e in verdict.evidence} - set(executed))
    if unknown:
        return None, (
            f"evidence cites call ids that were never executed: {unknown}; "
            f"valid ids: {sorted(executed)}"
        )
    if not verdict.evidence and verdict.cause_category is not CauseCategory.UNKNOWN:
        return None, "a verdict with a cause category needs at least one evidence item"
    return verdict, ""


def fallback_verdict(
    group: AlertGroup, records: list[ToolCallRecord], outcome: TriageOutcome | None
) -> Verdict:
    """Deterministic verdict when the model did not produce one. Never guesses a cause."""
    label = group.primary.labels.get("severity", "")
    severity = Severity(label) if label in {s.value for s in Severity} else Severity.MEDIUM
    reason = outcome.value if outcome else "unknown"
    return Verdict(
        summary=(
            f"Automated triage for {group.primary.name} ended without a verdict ({reason}). "
            "Evidence gathered so far is attached; a human needs to take over."
        ),
        suspected_cause="not determined",
        cause_category=CauseCategory.UNKNOWN,
        confidence=0.0,
        severity_suggestion=severity,
        evidence=[
            Evidence(
                tool_call_id=r.id,
                summary=f"{r.name}: {json.dumps(r.input, sort_keys=True)[:200]}",
            )
            for r in records
            if r.ok
        ][:5],
        next_steps=["Follow the runbook for this alert manually"],
        caveats=[f"Triage outcome: {reason}"],
    )
