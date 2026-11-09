from __future__ import annotations

from pathlib import Path

import pytest

from triage_agent.config import Settings
from triage_agent.scenarios import Scenario, load_scenario

ROOT = Path(__file__).resolve().parent.parent
SCENARIO_DIR = ROOT / "evals" / "scenarios"
RUNBOOK_DIR = ROOT / "runbooks"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        debounce_seconds=0,
        runbook_dir=RUNBOOK_DIR,
        audit_log_path=tmp_path / "audit.jsonl",
        approval_token="test-approval-token",
    )


def scenario(name: str) -> Scenario:
    return load_scenario(SCENARIO_DIR / f"{name}.yaml")
