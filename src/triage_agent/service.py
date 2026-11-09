"""Orchestration: webhook -> correlation -> queued triage job -> verdict + proposals."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from opentelemetry import trace
from pydantic import BaseModel, Field

from triage_agent import metrics
from triage_agent.agent import TriageAgent
from triage_agent.approvals import AuditLog, ProposalStore
from triage_agent.config import Settings
from triage_agent.correlation import Correlator
from triage_agent.llm import build_llm
from triage_agent.models import AlertGroup, AlertmanagerWebhook, TriageResult
from triage_agent.tools import build_registry
from triage_agent.tools.kubernetes import KubeClientReader, KubernetesReader
from triage_agent.tools.loki import HttpLokiBackend
from triage_agent.tools.prometheus import HttpPrometheusBackend
from triage_agent.tools.runbooks import RunbookIndex

log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

MAX_RUNS_KEPT = 500


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class TriageRun(BaseModel):
    id: str
    group_id: str
    alertname: str
    status: RunStatus
    created_at: datetime
    result: TriageResult | None = None
    proposal_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class IngestSummary(BaseModel):
    received: int
    new_groups: int
    updated_groups: int
    duplicates: int
    resolved: int
    triage_ids: list[str]


class TriageService:
    def __init__(
        self,
        settings: Settings,
        agent: TriageAgent,
        correlator: Correlator,
        proposals: ProposalStore,
        audit: AuditLog,
    ) -> None:
        self.settings = settings
        self.agent = agent
        self.correlator = correlator
        self.proposals = proposals
        self.audit = audit
        self.runs: OrderedDict[str, TriageRun] = OrderedDict()
        self._groups: dict[str, AlertGroup] = {}
        self._queue: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        for i in range(self.settings.max_concurrent_triages):
            self._workers.append(asyncio.create_task(self._worker(), name=f"triage-worker-{i}"))

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def drain(self) -> None:
        """Wait until every queued triage has finished (used by tests and the demo)."""
        await self._queue.join()

    # ------------------------------------------------------------------ ingest
    def ingest(self, payload: AlertmanagerWebhook) -> IngestSummary:
        with tracer.start_as_current_span("webhook.ingest") as span:
            for alert in payload.alerts:
                metrics.ALERTS_RECEIVED.labels(status=alert.status).inc()
            with tracer.start_as_current_span("correlate"):
                result = self.correlator.ingest(payload.alerts)
            span.set_attribute("alerts.received", len(payload.alerts))
            span.set_attribute("alerts.new_groups", len(result.new_groups))
            span.set_attribute("alerts.duplicates", result.duplicates)

            absorbed = (
                len(payload.alerts) - len(result.new_groups) - result.resolved - result.ignored
            )
            metrics.ALERTS_DEDUPLICATED.inc(max(absorbed, 0))
            triage_ids = []
            for group in result.new_groups:
                metrics.ALERT_GROUPS.inc()
                triage_ids.append(self._enqueue(group))
            self.audit.append(
                "alerts_received",
                count=len(payload.alerts),
                receiver=payload.receiver,
                new_groups=[g.id for g in result.new_groups],
                duplicates=result.duplicates,
            )
            return IngestSummary(
                received=len(payload.alerts),
                new_groups=len(result.new_groups),
                updated_groups=len(result.updated_groups),
                duplicates=result.duplicates,
                resolved=result.resolved,
                triage_ids=triage_ids,
            )

    def _enqueue(self, group: AlertGroup) -> str:
        run = TriageRun(
            id=uuid.uuid4().hex[:12],
            group_id=group.id,
            alertname=group.primary.name,
            status=RunStatus.QUEUED,
            created_at=datetime.now(UTC),
        )
        self.runs[run.id] = run
        self._groups[run.id] = group
        while len(self.runs) > MAX_RUNS_KEPT:
            old_id, _ = self.runs.popitem(last=False)
            self._groups.pop(old_id, None)
        due = asyncio.get_running_loop().time() + self.settings.debounce_seconds
        self._queue.put_nowait((run.id, due))
        self.audit.append("triage_queued", triage_id=run.id, group_id=group.id, key=group.key)
        return run.id

    # ------------------------------------------------------------------ worker
    async def _worker(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            run_id, due = await self._queue.get()
            try:
                # Debounce: let the rest of an alert storm join the group first.
                await asyncio.sleep(max(0.0, due - loop.time()))
                await self.execute(run_id)
            except Exception:  # keep the worker alive whatever happens
                log.exception("triage %s crashed", run_id)
            finally:
                self._queue.task_done()

    async def execute(self, run_id: str) -> TriageRun:
        run = self.runs[run_id]
        group = self._groups[run_id]
        run.status = RunStatus.RUNNING
        try:
            result = await self.agent.triage(group, triage_id=run.id)
        except Exception as exc:
            run.status = RunStatus.FAILED
            run.error = type(exc).__name__
            self.audit.append("triage_failed", triage_id=run.id, error=run.error)
            raise
        run.result = result
        proposals = self.proposals.register(run.id, result.verdict.proposed_actions)
        run.proposal_ids = [p.id for p in proposals]
        run.status = RunStatus.DONE
        self.audit.append(
            "triage_completed",
            triage_id=run.id,
            group_id=group.id,
            outcome=result.outcome.value,
            cause_category=result.verdict.cause_category.value,
            confidence=result.verdict.confidence,
            steps=result.steps,
            tool_calls=len(result.tool_calls),
            tokens=result.usage.total,
            model=result.model,
        )
        return run


def build_service(settings: Settings) -> TriageService:
    """Wire real backends from settings."""
    kube: KubernetesReader | None = None
    if settings.kubernetes_mode == "auto":
        try:
            kube = KubeClientReader()
        except Exception as exc:  # no kubeconfig / not in a cluster
            log.warning("Kubernetes tools disabled: %s", exc)
    registry = build_registry(
        settings,
        clock=lambda: datetime.now(UTC),
        prometheus=HttpPrometheusBackend(settings.prometheus_url),
        loki=HttpLokiBackend(settings.loki_url),
        kubernetes=kube,
        runbooks=RunbookIndex.from_dir(settings.runbook_dir),
    )
    audit = AuditLog(settings.audit_log_path)
    return TriageService(
        settings=settings,
        agent=TriageAgent(build_llm(settings), registry, settings),
        correlator=Correlator(settings.correlation_group_by, settings.correlation_window_seconds),
        proposals=ProposalStore(audit),
        audit=audit,
    )


def run_summary(run: TriageRun) -> dict[str, Any]:
    verdict = run.result.verdict if run.result else None
    return {
        "id": run.id,
        "alertname": run.alertname,
        "status": run.status.value,
        "created_at": run.created_at.isoformat(),
        "cause_category": verdict.cause_category.value if verdict else None,
        "confidence": verdict.confidence if verdict else None,
        "proposals": run.proposal_ids,
    }
