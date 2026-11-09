"""Labelled scenarios: an alert payload plus canned backend responses.

Used by the eval runner, the offline demo and integration tests. Fixtures are
served through the same backend protocols as the real Prometheus, Loki and
Kubernetes clients, so the real tool code (formatting, validation, limits)
runs unchanged. All times in a scenario are offsets from a fixed ``NOW`` so
results are reproducible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from triage_agent.agent import TriageAgent
from triage_agent.approvals import AuditLog, ProposalStore
from triage_agent.config import Settings
from triage_agent.correlation import Correlator
from triage_agent.llm.base import LLMClient
from triage_agent.models import Alert, AlertmanagerWebhook, CauseCategory
from triage_agent.service import TriageService
from triage_agent.tools import build_registry
from triage_agent.tools.base import ToolError
from triage_agent.tools.loki import LogLine
from triage_agent.tools.prometheus import Series
from triage_agent.tools.runbooks import RunbookIndex

NOW = datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)


def at(offset_seconds: float) -> datetime:
    return NOW + timedelta(seconds=offset_seconds)


@dataclass
class Expected:
    cause_category: CauseCategory
    evidence_keywords: list[str] = field(default_factory=list)
    groups: int = 1
    forbidden_action_patterns: list[str] = field(default_factory=list)


@dataclass
class Scenario:
    id: str
    title: str
    description: str
    alerts: list[Alert]
    expected: Expected
    fixtures: dict[str, Any]
    secrets: list[str]
    path: Path


def _alert(raw: dict[str, Any]) -> Alert:
    data = dict(raw)
    offset = data.pop("starts_offset_seconds", -180)
    data.setdefault("startsAt", at(offset).isoformat())
    expr = data.pop("expr", None)
    if expr and not data.get("generatorURL"):
        data["generatorURL"] = (
            f"http://prometheus:9090/graph?g0.expr={quote(expr, safe='')}&g0.tab=1"
        )
    return Alert.model_validate(data)


def load_scenario(path: Path) -> Scenario:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    alerts: list[Alert] = []
    for entry in raw["alerts"]:
        repeat = int(entry.pop("repeat", 1)) if isinstance(entry, dict) else 1
        for i in range(repeat):
            item = dict(entry)
            if repeat > 1 and "{i}" in str(item.get("labels", {})):
                item["labels"] = {k: v.replace("{i}", str(i)) for k, v in item["labels"].items()}
                item["fingerprint"] = str(item.get("fingerprint", "fp")).replace("{i}", str(i))
            alerts.append(_alert(item))
    exp = raw["expected"]
    return Scenario(
        id=raw["id"],
        title=raw.get("title", raw["id"]),
        description=raw.get("description", ""),
        alerts=alerts,
        expected=Expected(
            cause_category=CauseCategory(exp["cause_category"]),
            evidence_keywords=list(exp.get("evidence_keywords", [])),
            groups=int(exp.get("groups", 1)),
            forbidden_action_patterns=list(exp.get("forbidden_action_patterns", [])),
        ),
        fixtures=raw.get("fixtures", {}),
        secrets=list(raw.get("secrets", [])),
        path=path,
    )


def load_scenarios(directory: Path) -> list[Scenario]:
    return [load_scenario(p) for p in sorted(directory.glob("*.yaml"))]


# --------------------------------------------------------------------------- #
# Fixture backends
# --------------------------------------------------------------------------- #


def _first_match(entries: list[dict[str, Any]], text: str) -> dict[str, Any] | None:
    for entry in entries:
        if re.search(entry["match"], text):
            return entry
    return None


class FixturePrometheus:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.queries: list[str] = []

    def _series(self, query: str) -> list[Series]:
        self.queries.append(query)
        entry = _first_match(self.entries, query)
        if entry is None:
            return []
        return [
            Series(
                labels=dict(s.get("labels", {})),
                points=[(at(off).timestamp(), float(v)) for off, v in s["values"]],
            )
            for s in entry["series"]
        ]

    async def instant(self, query: str, at_time: datetime) -> list[Series]:
        series = self._series(query)
        return [Series(labels=s.labels, points=s.points[-1:]) for s in series]

    async def range(
        self, query: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[Series]:
        return [
            Series(
                labels=s.labels,
                points=[p for p in s.points if start.timestamp() <= p[0] <= end.timestamp()],
            )
            for s in self._series(query)
        ]


class FixtureLoki:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = entries
        self.queries: list[str] = []

    async def query_range(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[LogLine]:
        self.queries.append(query)
        entry = _first_match(self.entries, query)
        if entry is None:
            return []
        lines = []
        for item in entry["lines"]:
            ts = at(item.get("offset", -60))
            if start <= ts <= end:
                lines.append(LogLine(ts=ts, labels={"pod": item.get("pod", "")}, line=item["line"]))
        lines.sort(key=lambda entry: entry.ts)
        return lines[-limit:]


class FixtureKubernetes:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.calls: list[str] = []

    async def list_pods(self, namespace: str, label_selector: str | None) -> list[dict[str, Any]]:
        self.calls.append(f"list_pods {namespace}")
        pods: list[dict[str, Any]] = self.data.get("pods", {}).get(namespace, [])
        return pods

    async def get_pod(self, namespace: str, name: str) -> dict[str, Any]:
        self.calls.append(f"get_pod {namespace}/{name}")
        details = self.data.get("pod_details", {}).get(f"{namespace}/{name}")
        if details is None:
            raise ToolError(f"pod {namespace}/{name} not found")
        result: dict[str, Any] = details
        return result

    async def list_events(self, namespace: str, object_name: str | None) -> list[dict[str, Any]]:
        self.calls.append(f"list_events {namespace} {object_name or '*'}")
        events = []
        for ev in self.data.get("events", {}).get(namespace, []):
            if object_name and object_name not in ev.get("object", ""):
                continue
            item = {k: v for k, v in ev.items() if k != "offset"}
            item["last_seen"] = at(ev.get("offset", -60)).isoformat()
            events.append(item)
        return events

    async def rollout_history(self, namespace: str, deployment: str) -> list[dict[str, Any]]:
        self.calls.append(f"rollout_history {namespace}/{deployment}")
        history = []
        for rev in self.data.get("rollouts", {}).get(f"{namespace}/{deployment}", []):
            item = {k: v for k, v in rev.items() if k != "offset_minutes"}
            item["created"] = at(rev.get("offset_minutes", -1440) * 60).isoformat()
            history.append(item)
        return history


# --------------------------------------------------------------------------- #
# Wiring helpers
# --------------------------------------------------------------------------- #


def fixture_service(
    scenario: Scenario,
    llm: LLMClient,
    settings: Settings | None = None,
    runbook_dir: Path | None = None,
) -> tuple[TriageService, FixturePrometheus, FixtureLoki, FixtureKubernetes]:
    """A full TriageService whose data sources are the scenario's fixtures."""
    settings = settings or Settings(debounce_seconds=0, llm_provider="fake")
    prom = FixturePrometheus(scenario.fixtures.get("prometheus", []))
    loki = FixtureLoki(scenario.fixtures.get("loki", []))
    kube = FixtureKubernetes(scenario.fixtures.get("kubernetes", {}))
    registry = build_registry(
        settings,
        clock=lambda: NOW,
        prometheus=prom,
        loki=loki,
        kubernetes=kube,
        runbooks=RunbookIndex.from_dir(runbook_dir or settings.runbook_dir),
    )
    audit = AuditLog(None)
    service = TriageService(
        settings=settings,
        agent=TriageAgent(llm, registry, settings, clock=lambda: NOW),
        correlator=Correlator(
            settings.correlation_group_by, settings.correlation_window_seconds, clock=lambda: NOW
        ),
        proposals=ProposalStore(audit),
        audit=audit,
    )
    return service, prom, loki, kube


def webhook_for(scenario: Scenario) -> AlertmanagerWebhook:
    return AlertmanagerWebhook(receiver="triage-agent", status="firing", alerts=scenario.alerts)
