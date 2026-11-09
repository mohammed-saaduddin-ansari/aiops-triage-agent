"""Prometheus metrics for the triage service itself (exposed on ``/metrics``)."""

from __future__ import annotations

from prometheus_client import Counter, Histogram

ALERTS_RECEIVED = Counter(
    "triage_alerts_received_total", "Alerts received from Alertmanager", ["status"]
)
ALERT_GROUPS = Counter("triage_alert_groups_total", "Correlated alert groups created")
ALERTS_DEDUPLICATED = Counter(
    "triage_alerts_deduplicated_total", "Alerts absorbed into an existing group or suppressed"
)
TRIAGE_RUNS = Counter("triage_runs_total", "Completed triage runs", ["outcome"])
TRIAGE_DURATION = Histogram(
    "triage_duration_seconds",
    "Wall-clock time per triage run",
    buckets=(1, 2.5, 5, 10, 20, 30, 60, 120, 300),
)
TRIAGE_STEPS = Histogram(
    "triage_steps", "LLM steps per triage run", buckets=(1, 2, 3, 4, 5, 6, 8, 10, 15, 20)
)
TOOL_CALLS = Counter("triage_tool_calls_total", "Tool calls made by the agent", ["tool", "status"])
TOOL_LATENCY = Histogram(
    "triage_tool_call_duration_seconds",
    "Tool call latency",
    ["tool"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)
INJECTION_FLAGS = Counter(
    "triage_injection_flags_total",
    "Tool outputs that matched prompt-injection heuristics",
    ["tool"],
)
LLM_CALLS = Counter("triage_llm_calls_total", "LLM requests", ["model", "stop_reason"])
LLM_TOKENS = Counter("triage_llm_tokens_total", "LLM tokens", ["model", "kind"])
LLM_LATENCY = Histogram(
    "triage_llm_call_duration_seconds",
    "LLM request latency",
    ["model"],
    buckets=(0.1, 0.5, 1, 2.5, 5, 10, 20, 40, 80, 160),
)
PROPOSALS = Counter("triage_proposals_total", "Remediation proposals by status", ["status"])
APPROVAL_DECISIONS = Counter(
    "triage_approval_decisions_total", "Human approval decisions", ["decision"]
)
