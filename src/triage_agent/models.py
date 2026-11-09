"""Domain models: Alertmanager payloads, alert groups, verdicts and proposals."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --------------------------------------------------------------------------- #
# Alertmanager webhook (payload version 4)
# --------------------------------------------------------------------------- #

_POD_HASH_SUFFIX = re.compile(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$")  # deployment pods
_POD_ORDINAL_OR_JOB_SUFFIX = re.compile(r"-[a-z0-9]{5}$")  # daemonset / job pods


def workload_from_pod(pod: str) -> str:
    """Best-effort workload name from a pod name (``api-7d9f8c6b5d-x2x9k`` -> ``api``)."""
    if _POD_HASH_SUFFIX.search(pod):
        return _POD_HASH_SUFFIX.sub("", pod)
    if _POD_ORDINAL_OR_JOB_SUFFIX.search(pod):
        return _POD_ORDINAL_OR_JOB_SUFFIX.sub("", pod)
    return pod


class Alert(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime = Field(default_factory=lambda: datetime.now(UTC))
    endsAt: datetime | None = None
    generatorURL: str = ""
    fingerprint: str = ""

    @field_validator("endsAt", mode="before")
    @classmethod
    def _zero_time_is_none(cls, value: Any) -> Any:
        # Alertmanager sends 0001-01-01T00:00:00Z for "not resolved yet".
        if isinstance(value, str) and value.startswith("0001-01-01"):
            return None
        return value

    @property
    def name(self) -> str:
        return self.labels.get("alertname", "unknown")

    def derived_labels(self) -> dict[str, str]:
        """Labels plus a derived ``workload`` label used for correlation."""
        labels = dict(self.labels)
        if "workload" not in labels:
            for key in ("deployment", "statefulset", "daemonset", "job_name", "service", "app"):
                if labels.get(key):
                    labels["workload"] = labels[key]
                    break
            else:
                if labels.get("pod"):
                    labels["workload"] = workload_from_pod(labels["pod"])
        return labels

    def identity(self) -> str:
        """Fingerprint if Alertmanager supplied one, else a stable label hash."""
        if self.fingerprint:
            return self.fingerprint
        return "|".join(f"{k}={v}" for k, v in sorted(self.labels.items()))


class AlertmanagerWebhook(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: str = "4"
    groupKey: str = ""
    status: str = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)
    commonLabels: dict[str, str] = Field(default_factory=dict)
    commonAnnotations: dict[str, str] = Field(default_factory=dict)
    externalURL: str = ""
    alerts: list[Alert] = Field(default_factory=list)


class AlertGroup(BaseModel):
    """A set of correlated alerts that will be triaged once."""

    id: str
    key: dict[str, str]
    alerts: list[Alert] = Field(default_factory=list)
    first_seen: datetime
    last_seen: datetime
    duplicates_suppressed: int = 0

    @property
    def primary(self) -> Alert:
        return self.alerts[0]

    @property
    def firing(self) -> list[Alert]:
        return [a for a in self.alerts if a.status == "firing"]


# --------------------------------------------------------------------------- #
# Verdict
# --------------------------------------------------------------------------- #


class CauseCategory(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    CONFIG_ERROR = "config_error"
    RESOURCE_EXHAUSTION = "resource_exhaustion"
    DEPENDENCY_FAILURE = "dependency_failure"
    CAPACITY_SATURATION = "capacity_saturation"
    NODE_FAILURE = "node_failure"
    NETWORK = "network"
    CERTIFICATE_EXPIRY = "certificate_expiry"
    DISK_PRESSURE = "disk_pressure"
    FALSE_POSITIVE = "false_positive"
    UNKNOWN = "unknown"


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ActionKind(StrEnum):
    INVESTIGATE = "investigate"
    ROLLBACK = "rollback"
    RESTART = "restart"
    SCALE = "scale"
    CONFIG_CHANGE = "config_change"
    SILENCE = "silence"
    ESCALATE = "escalate"
    OTHER = "other"


# Kinds that would change the system if a human carried them out.
MUTATING_KINDS = frozenset(
    {
        ActionKind.ROLLBACK,
        ActionKind.RESTART,
        ActionKind.SCALE,
        ActionKind.CONFIG_CHANGE,
        ActionKind.SILENCE,
        ActionKind.OTHER,
    }
)


class Evidence(BaseModel):
    tool_call_id: str = Field(description="id of the tool call that produced this evidence")
    summary: str
    query: str | None = None
    link: str | None = None
    verified: bool = Field(
        default=False,
        description="Set by the harness: true if tool_call_id matches a call it executed.",
    )


class ProposedAction(BaseModel):
    title: str
    kind: ActionKind
    rationale: str
    command: str | None = Field(
        default=None, description="Suggested command for a human to run. Never executed."
    )
    risk: Severity = Severity.MEDIUM


class Verdict(BaseModel):
    summary: str
    suspected_cause: str
    cause_category: CauseCategory
    confidence: float = Field(ge=0.0, le=1.0)
    severity_suggestion: Severity
    evidence: list[Evidence] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )

    def add(self, other: TokenUsage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.estimated = self.estimated or other.estimated


class ToolCallRecord(BaseModel):
    id: str
    name: str
    input: dict[str, Any]
    ok: bool
    duration_ms: float
    truncated: bool = False
    redactions: int = 0
    injection_flags: list[str] = Field(default_factory=list)
    output_preview: str = ""


class TriageOutcome(StrEnum):
    COMPLETED = "completed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    NO_VERDICT = "no_verdict"
    REFUSED = "refused"
    ERROR = "error"


class TriageResult(BaseModel):
    triage_id: str
    group_id: str
    outcome: TriageOutcome
    verdict: Verdict
    steps: int
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str
    started_at: datetime
    finished_at: datetime
    estimated_cost_usd: float | None = None


# --------------------------------------------------------------------------- #
# Proposals and approvals
# --------------------------------------------------------------------------- #


class ProposalStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"  # refused by policy; cannot be approved


class Proposal(BaseModel):
    id: str
    triage_id: str
    action: ProposedAction
    status: ProposalStatus
    policy_reason: str
    created_at: datetime
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class ApprovalDecision(BaseModel):
    approver: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=2000)
