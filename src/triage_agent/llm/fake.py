"""A deterministic stand-in for the LLM, used by tests, CI evals and the offline demo.

It is honest about what it is: a scripted investigator plus keyword rules.
It follows the same protocol as the real model (tool calls in, tool results
out, finish with ``submit_verdict``), and it only sees what the real model
would see: redacted, truncated, delimited tool output. That makes it useful
for testing the *harness* (correlation, budgets, tool plumbing, redaction,
citation checks, safety gates) without network access or cost.

It is not a measure of model quality. Offline eval scores say the plumbing
works; only a run against a real model says anything about reasoning.

Investigation script:
  step 1  runbook_lookup for the alert
  step 2  in parallel: the alert's own PromQL (from generatorURL) as a range
          query, PromQL checks listed in the runbook, k8s events, rollout
          history, and an error-filtered Loki search
  step 3  describe the alerting pod, if there is one
  step 4  classify with ordered keyword rules and call submit_verdict
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from triage_agent.llm.base import LLMResponse, ToolCall
from triage_agent.models import ActionKind, CauseCategory, Severity, TokenUsage
from triage_agent.prompts import SUBMIT_VERDICT

_ALERT_GROUP = re.compile(r"<alert_group[^>]*>\n(.*?)\n</alert_group>", re.S)
_TOOL_OUTPUT_BODY = re.compile(r"<tool_output[^>]*>(.*?)</tool_output[^>]*>", re.S)
_PROMQL_BLOCK = re.compile(r"```promql\n(.*?)\n```", re.S)
_ERRORISH = re.compile(r"(?i)\b(error|exception|panic|fatal|5\d\d|failed|traceback)\b")


@dataclass(frozen=True)
class Rule:
    category: CauseCategory
    pattern: re.Pattern[str]
    cause: str
    confidence: float
    default_severity: Severity


RULES: list[Rule] = [
    Rule(
        CauseCategory.CERTIFICATE_EXPIRY,
        re.compile(
            r"(?i)x509|certificate (has )?expired|failed to renew certificate|certificate expires in"
        ),
        "TLS certificate is expiring or expired and renewal is failing",
        0.8,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.NODE_FAILURE,
        re.compile(r"(?i)NodeNotReady|Kubelet stopped posting node status|node is not ready"),
        "Node is NotReady; workloads on it are failing or being evicted",
        0.8,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.DISK_PRESSURE,
        re.compile(r"(?i)DiskPressure|no space left on device|EvictionThresholdMet"),
        "Volume or node filesystem is full",
        0.8,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.RESOURCE_EXHAUSTION,
        re.compile(r"(?i)OOMKill(ed|ing)|out of memory|memory cgroup"),
        "Container is exceeding its memory limit and being OOM-killed",
        0.8,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.NETWORK,
        re.compile(r"(?i)no such host|NXDOMAIN|SERVFAIL|temporary failure in name resolution"),
        "DNS resolution is failing for a dependency",
        0.75,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.CONFIG_ERROR,
        re.compile(
            r"(?i)KeyError|environment variable \S+ (is )?(not set|missing)|missing required config|"
            r"invalid configuration|CreateContainerConfigError|401 Unauthorized|invalid api key"
        ),
        "Workload is running with missing or invalid configuration or credentials",
        0.75,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.DEPENDENCY_FAILURE,
        re.compile(
            r"(?i)connection refused|ECONNREFUSED|upstream connect error|could not connect to server|"
            r"too many connections"
        ),
        "A downstream dependency is refusing or failing connections",
        0.7,
        Severity.HIGH,
    ),
    Rule(
        CauseCategory.CAPACITY_SATURATION,
        re.compile(
            r"(?i)throttl|queue (is )?full|too many requests|rate limit exceeded|"
            r"reached (its )?max(imum)? replicas|worker pool exhausted"
        ),
        "Demand is exceeding provisioned capacity (CPU throttling / queueing)",
        0.7,
        Severity.MEDIUM,
    ),
]

ACTIONS: dict[CauseCategory, list[dict[str, Any]]] = {
    CauseCategory.BAD_DEPLOY: [
        {
            "title": "Roll back {workload} to the previous revision",
            "kind": ActionKind.ROLLBACK,
            "rationale": "Errors began shortly after the latest rollout; rollback is the fastest reversible fix.",
            "command": "kubectl rollout undo deployment/{workload} -n {namespace}",
            "risk": Severity.MEDIUM,
        }
    ],
    CauseCategory.CONFIG_ERROR: [
        {
            "title": "Restore the missing or rotated configuration for {workload}",
            "kind": ActionKind.CONFIG_CHANGE,
            "rationale": "Logs show the workload cannot load required configuration or credentials.",
            "command": None,
            "risk": Severity.MEDIUM,
        }
    ],
    CauseCategory.RESOURCE_EXHAUSTION: [
        {
            "title": "Raise the memory limit for {workload} temporarily and open a leak investigation",
            "kind": ActionKind.CONFIG_CHANGE,
            "rationale": "Containers are OOM-killed; a higher limit buys time while the leak is found.",
            "command": "kubectl set resources deployment/{workload} -n {namespace} --limits=memory=<new-limit>",
            "risk": Severity.MEDIUM,
        }
    ],
    CauseCategory.DEPENDENCY_FAILURE: [
        {
            "title": "Escalate to the owners of the failing dependency",
            "kind": ActionKind.ESCALATE,
            "rationale": "The fault is downstream; restarting this service will not help.",
            "command": None,
            "risk": Severity.LOW,
        }
    ],
    CauseCategory.CAPACITY_SATURATION: [
        {
            "title": "Scale out {workload}",
            "kind": ActionKind.SCALE,
            "rationale": "Throttling and queueing indicate demand above provisioned capacity.",
            "command": "kubectl scale deployment/{workload} -n {namespace} --replicas=<current+2>",
            "risk": Severity.MEDIUM,
        }
    ],
    CauseCategory.NODE_FAILURE: [
        {
            "title": "Cordon the unhealthy node",
            "kind": ActionKind.CONFIG_CHANGE,
            "rationale": "Stops new pods landing on a NotReady node while it is investigated.",
            "command": "kubectl cordon {node}",
            "risk": Severity.MEDIUM,
        }
    ],
    CauseCategory.CERTIFICATE_EXPIRY: [
        {
            "title": "Investigate and fix certificate renewal",
            "kind": ActionKind.INVESTIGATE,
            "rationale": "Renewal is failing; the certificate will expire if nothing changes.",
            "command": None,
            "risk": Severity.LOW,
        }
    ],
    CauseCategory.DISK_PRESSURE: [
        {
            "title": "Expand the volume or free space",
            "kind": ActionKind.CONFIG_CHANGE,
            "rationale": "Writes are failing because the filesystem is full.",
            "command": None,
            "risk": Severity.MEDIUM,
        }
    ],
    CauseCategory.NETWORK: [
        {
            "title": "Check CoreDNS health and the dependency's DNS record",
            "kind": ActionKind.INVESTIGATE,
            "rationale": "Lookups for the dependency are failing.",
            "command": "kubectl -n kube-system logs deployment/coredns --since=30m",
            "risk": Severity.LOW,
        }
    ],
    CauseCategory.FALSE_POSITIVE: [
        {
            "title": "Silence for 2h and tune the alert threshold or 'for' duration",
            "kind": ActionKind.SILENCE,
            "rationale": "No corroborating errors; the alert appears to flap around its threshold.",
            "command": "amtool silence add alertname={alertname} --duration=2h --comment='flapping; tune threshold'",
            "risk": Severity.LOW,
        }
    ],
    CauseCategory.UNKNOWN: [
        {
            "title": "Escalate to the service owner",
            "kind": ActionKind.ESCALATE,
            "rationale": "Automated triage could not identify a cause.",
            "command": None,
            "risk": Severity.LOW,
        }
    ],
}


class FakeLLM:
    """Deterministic scripted investigator implementing ``LLMClient``."""

    def __init__(self, model: str = "fake-triage-llm") -> None:
        self.model = model
        self.requests: list[dict[str, Any]] = []  # every request, for redaction checks

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> LLMResponse:
        self.requests.append(
            {"system": system, "messages": json.loads(json.dumps(messages, default=str))}
        )
        available = {t["name"] for t in tools}
        step = sum(1 for m in messages if m["role"] == "assistant")
        ctx = _context(messages)
        calls = self._plan(step, ctx, available, messages)
        if not calls:
            calls = [(SUBMIT_VERDICT, _classify(ctx, messages))]
        tool_calls = [
            ToolCall(id=f"call_{step}_{i}", name=name, input=args)
            for i, (name, args) in enumerate(calls)
        ]
        content: list[Any] = [{"type": "text", "text": f"Step {step + 1}: investigating."}]
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tool_calls
        ]
        prompt_chars = len(system) + len(json.dumps(messages, default=str)) + len(json.dumps(tools))
        usage = TokenUsage(
            input_tokens=prompt_chars // 4,
            output_tokens=len(json.dumps(content)) // 4,
            estimated=True,
        )
        return LLMResponse(
            assistant_content=content,
            tool_calls=tool_calls,
            text=content[0]["text"],
            stop_reason="tool_use",
            usage=usage,
            model=self.model,
        )

    # ------------------------------------------------------------------ plan
    def _plan(
        self,
        step: int,
        ctx: dict[str, str],
        available: set[str],
        messages: list[dict[str, Any]],
    ) -> list[tuple[str, dict[str, Any]]]:
        ns, workload, pod, node = (
            ctx.get("namespace"),
            ctx.get("workload"),
            ctx.get("pod"),
            ctx.get("node"),
        )
        plan: list[tuple[str, dict[str, Any]]] = []
        if step == 0:
            if "runbook_lookup" in available:
                plan.append(
                    (
                        "runbook_lookup",
                        {"alertname": ctx["alertname"], "query": ctx.get("summary", "")},
                    )
                )
            if plan:
                return plan
            step = 1
        if step == 1:
            expr = ctx.get("expr")
            if expr and "prometheus_range_query" in available:
                plan.append(("prometheus_range_query", {"query": expr, "minutes": 60}))
            if "prometheus_query" in available:
                for query in _runbook_queries(messages, ctx)[:2]:
                    plan.append(("prometheus_query", {"query": query}))
            if "k8s_get_events" in available:
                if ns:
                    plan.append(("k8s_get_events", {"namespace": ns}))
                if node:
                    plan.append(("k8s_get_events", {"namespace": "default", "object_name": node}))
            if ns and workload and "k8s_rollout_history" in available:
                plan.append(("k8s_rollout_history", {"namespace": ns, "deployment": workload}))
            if ns and "loki_search" in available:
                selector = f'namespace="{ns}"' + (f', app="{workload}"' if workload else "")
                plan.append(
                    (
                        "loki_search",
                        {
                            "query": "{"
                            + selector
                            + '} |~ "(?i)error|fatal|panic|exception|refused|'
                            'timeout|denied|expired|unauthorized|full|throttl"',
                            "minutes": 30,
                            "limit": 50,
                        },
                    )
                )
            if plan:
                return plan
            step = 2
        if step == 2 and ns and pod and "k8s_describe_pod" in available:
            return [("k8s_describe_pod", {"namespace": ns, "pod": pod})]
        return []


# ---------------------------------------------------------------------- helpers


def _context(messages: list[dict[str, Any]]) -> dict[str, str]:
    first = messages[0]["content"]
    text = first if isinstance(first, str) else json.dumps(first)
    match = _ALERT_GROUP.search(text)
    group = json.loads(match.group(1)) if match else {"alerts": []}
    alert = group["alerts"][0] if group["alerts"] else {"labels": {}, "annotations": {}}
    labels: dict[str, str] = alert.get("labels", {})
    ctx = {
        "alertname": labels.get("alertname", "unknown"),
        "summary": alert.get("annotations", {}).get("summary", ""),
        "severity": labels.get("severity", ""),
    }
    for key in ("namespace", "pod", "node", "instance"):
        if labels.get(key):
            ctx[key] = labels[key]
    workload = group.get("correlation_key", {}).get("workload") or labels.get("deployment")
    if workload:
        ctx["workload"] = workload
    gen = alert.get("generator_url") or ""
    exprs = parse_qs(urlparse(gen).query).get("g0.expr")
    if exprs:
        ctx["expr"] = exprs[0]
    return ctx


def _tool_results(messages: list[dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any], str]]:
    """Map call id -> (tool name, input, body text without the delimiter/warning)."""
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    results: dict[str, tuple[str, dict[str, Any], str]] = {}
    for message in messages:
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                calls[block["id"]] = (block["name"], block.get("input", {}))
            elif block.get("type") == "tool_result" and block["tool_use_id"] in calls:
                raw = block.get("content", "")
                raw = raw if isinstance(raw, str) else json.dumps(raw)
                body_match = _TOOL_OUTPUT_BODY.search(raw)
                body = body_match.group(1) if body_match else raw
                body = "\n".join(
                    line
                    for line in body.splitlines()
                    if not line.startswith("WARNING: this output matched")
                )
                name, args = calls[block["tool_use_id"]]
                results[block["tool_use_id"]] = (name, args, body)
    return results


def _runbook_queries(messages: list[dict[str, Any]], ctx: dict[str, str]) -> list[str]:
    queries: list[str] = []
    for name, _args, body in _tool_results(messages).values():
        if name != "runbook_lookup":
            continue
        for block in _PROMQL_BLOCK.findall(body):
            query = block.strip()
            for key in ("namespace", "workload", "pod", "node", "instance"):
                query = query.replace("{{" + key + "}}", ctx.get(key, ""))
            if "{{" not in query:
                queries.append(query)
    return queries


def _content_lines(body: str) -> list[str]:
    skip = ("query:", "window:", "## ")
    return [line for line in body.splitlines() if line.strip() and not line.startswith(skip)]


def _recent_rollout(
    results: dict[str, tuple[str, dict[str, Any], str]],
) -> tuple[str, dict[str, Any]] | None:
    for call_id, (name, _args, body) in results.items():
        if name != "k8s_rollout_history":
            continue
        try:
            history = json.loads(body)
        except json.JSONDecodeError:
            continue
        if len(history) >= 2 and history[-1].get("age_minutes", 10_000) <= 60:
            return call_id, history[-1]
    return None


def _classify(ctx: dict[str, str], messages: list[dict[str, Any]]) -> dict[str, Any]:
    results = _tool_results(messages)
    evidence_sources = {k: v for k, v in results.items() if v[0] != "runbook_lookup"}
    injection_seen = any(
        "matched prompt-injection heuristics" in json.dumps(m["content"], default=str)
        for m in messages
    )

    category = CauseCategory.UNKNOWN
    cause = "Evidence does not point to a single cause"
    confidence = 0.2
    severity = Severity.MEDIUM
    evidence: list[dict[str, Any]] = []

    rollout = _recent_rollout(evidence_sources)
    error_lines = [
        (cid, line)
        for cid, (name, _a, body) in evidence_sources.items()
        if name in ("loki_search", "k8s_get_events", "k8s_describe_pod")
        for line in _content_lines(body)
        if _ERRORISH.search(line)
    ]

    def matches(pattern: re.Pattern[str]) -> list[tuple[str, str]]:
        found = []
        for cid, (_name, _a, body) in evidence_sources.items():
            for line in _content_lines(body):
                if pattern.search(line):
                    found.append((cid, line.strip()))
                    break
        return found

    chosen: Rule | None = None
    hits: list[tuple[str, str]] = []
    for rule in RULES[:5]:  # infrastructure-level causes outrank a coincident deploy
        hits = matches(rule.pattern)
        if hits:
            chosen = rule
            break
    if chosen is None and rollout is not None and error_lines:
        rollout_id, latest = rollout
        category = CauseCategory.BAD_DEPLOY
        cause = (
            f"Revision {latest.get('revision')} ({', '.join(latest.get('images', []))}) rolled out "
            f"{latest.get('age_minutes')} minutes before the alert, followed by errors"
        )
        confidence = 0.75
        severity = Severity.HIGH
        evidence.append(
            {
                "tool_call_id": rollout_id,
                "summary": f"Latest rollout: revision {latest.get('revision')}, "
                f"{latest.get('age_minutes')} min old, images {latest.get('images')}",
            }
        )
        seen = {rollout_id}
        for cid, line in error_lines:
            if cid not in seen:
                evidence.append(
                    {"tool_call_id": cid, "summary": f"Errors after rollout: {line.strip()[:200]}"}
                )
                seen.add(cid)
            if len(evidence) >= 3:
                break
    if chosen is None and category is CauseCategory.UNKNOWN:
        for rule in RULES[5:]:
            hits = matches(rule.pattern)
            if hits:
                chosen = rule
                break
    if chosen is not None:
        category, cause, confidence, severity = (
            chosen.category,
            chosen.cause,
            chosen.confidence,
            chosen.default_severity,
        )
        if len(hits) >= 2:
            confidence = min(0.9, confidence + 0.1)
        evidence = [{"tool_call_id": cid, "summary": line[:200]} for cid, line in hits[:3]]
    if category is CauseCategory.UNKNOWN and not error_lines and evidence_sources:
        category = CauseCategory.FALSE_POSITIVE
        cause = "Alert fired without corroborating errors in logs, events or pod state"
        confidence = 0.45
        severity = Severity.LOW
        evidence = [
            {
                "tool_call_id": cid,
                "summary": f"No corroborating signal: {_content_lines(body)[0][:160] if _content_lines(body) else 'empty'}",
            }
            for cid, (_n, _a, body) in list(evidence_sources.items())[:2]
        ]

    label_severity = ctx.get("severity", "")
    if (
        label_severity in {s.value for s in Severity}
        and category is not CauseCategory.FALSE_POSITIVE
    ):
        severity = Severity(label_severity)

    fmt = {
        "workload": ctx.get("workload", "<workload>"),
        "namespace": ctx.get("namespace", "<namespace>"),
        "node": ctx.get("node", "<node>"),
        "alertname": ctx.get("alertname", "<alertname>"),
    }
    actions = []
    for template in ACTIONS[category]:
        action = {k: (v.format(**fmt) if isinstance(v, str) else v) for k, v in template.items()}
        if action.get("command") is None:
            action.pop("command")
        actions.append(action)

    caveats = ["Produced by the deterministic FakeLLM (keyword rules), not a language model."]
    if injection_seen:
        caveats.append(
            "A tool output contained text resembling instructions; it was treated as data and ignored."
        )
    return {
        "summary": f"{ctx['alertname']}: {cause}.",
        "suspected_cause": cause,
        "cause_category": category.value,
        "confidence": confidence,
        "severity_suggestion": severity.value,
        "evidence": evidence,
        "next_steps": [
            "Confirm the suspected cause against the cited evidence",
            "Follow the matching runbook section",
        ],
        "proposed_actions": actions,
        "caveats": caveats,
    }
