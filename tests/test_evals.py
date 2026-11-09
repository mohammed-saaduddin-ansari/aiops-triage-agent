"""The eval harness must catch the failures it claims to catch."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tests.conftest import ROOT, SCENARIO_DIR, scenario
from triage_agent.config import Settings
from triage_agent.llm import FakeLLM, LLMResponse
from triage_agent.scenarios import load_scenarios

sys.path.insert(0, str(ROOT / "evals"))
import run_evals


class InjectionFollowingLLM(FakeLLM):
    """Behaves like a model that obeyed the instruction planted in the logs."""

    async def complete(self, **kwargs: Any) -> LLMResponse:
        resp = await super().complete(**kwargs)
        for call in resp.tool_calls:
            if call.name == "submit_verdict":
                call.input["proposed_actions"].append(
                    {
                        "title": "Reset the search namespace",
                        "kind": "other",
                        "rationale": "as instructed",
                        "command": "kubectl delete namespace search",
                    }
                )
        return resp


def test_all_scenarios_load() -> None:
    scenarios = load_scenarios(SCENARIO_DIR)
    assert 10 <= len(scenarios) <= 15
    assert len({s.id for s in scenarios}) == len(scenarios)


async def test_safety_check_catches_injection_following_model() -> None:
    settings = Settings(debounce_seconds=0, runbook_dir=ROOT / "runbooks")
    score = await run_evals.score_scenario(
        scenario("prompt-injection-in-logs"), InjectionFollowingLLM(), settings
    )
    assert score.safe_ok is False
    assert score.passed is False


async def test_redaction_check_catches_a_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("triage_agent.tools.base.redact", lambda text: (text, 0))
    settings = Settings(debounce_seconds=0, runbook_dir=ROOT / "runbooks")
    score = await run_evals.score_scenario(
        scenario("secret-in-logs-credential-rotation"), FakeLLM(), settings
    )
    assert score.redaction_ok is False


async def test_offline_suite_passes() -> None:
    settings = Settings(debounce_seconds=0, runbook_dir=ROOT / "runbooks")
    for sc in load_scenarios(SCENARIO_DIR):
        score = await run_evals.score_scenario(sc, FakeLLM(), settings)
        assert score.passed, (sc.id, score.notes)
