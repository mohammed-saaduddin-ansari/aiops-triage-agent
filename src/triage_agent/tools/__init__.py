"""Read-only tools available to the triage agent."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from triage_agent.config import Settings
from triage_agent.tools.base import Tool, ToolError, ToolOutput, ToolRegistry
from triage_agent.tools.kubernetes import (
    K8sDescribePodTool,
    K8sGetEventsTool,
    K8sGetPodsTool,
    K8sRolloutHistoryTool,
    KubernetesReader,
)
from triage_agent.tools.loki import LokiBackend, LokiSearchTool
from triage_agent.tools.prometheus import (
    PrometheusBackend,
    PrometheusQueryTool,
    PrometheusRangeQueryTool,
)
from triage_agent.tools.runbooks import RunbookIndex, RunbookLookupTool

__all__ = [
    "Tool",
    "ToolError",
    "ToolOutput",
    "ToolRegistry",
    "build_registry",
]


def build_registry(
    settings: Settings,
    *,
    clock: Callable[[], datetime],
    prometheus: PrometheusBackend | None,
    loki: LokiBackend | None,
    kubernetes: KubernetesReader | None,
    runbooks: RunbookIndex,
) -> ToolRegistry:
    """Assemble the tool set. A backend of ``None`` removes its tools entirely."""
    tools: list[Tool] = [RunbookLookupTool(runbooks)]
    if prometheus is not None:
        ext = settings.prometheus_external_url
        tools += [
            PrometheusQueryTool(prometheus, clock, ext),
            PrometheusRangeQueryTool(prometheus, clock, ext),
        ]
    if loki is not None:
        tools.append(LokiSearchTool(loki, clock))
    if kubernetes is not None:
        ns = settings.kubernetes_namespaces
        tools += [
            K8sGetPodsTool(kubernetes, ns, clock),
            K8sDescribePodTool(kubernetes, ns, clock),
            K8sGetEventsTool(kubernetes, ns, clock),
            K8sRolloutHistoryTool(kubernetes, ns, clock),
        ]
    return ToolRegistry(
        tools,
        allowed=settings.allowed_tools,
        timeout_seconds=settings.tool_timeout_seconds,
        max_chars=settings.tool_result_max_chars,
    )
