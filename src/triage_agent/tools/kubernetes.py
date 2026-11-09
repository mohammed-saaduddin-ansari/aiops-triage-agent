"""Read-only Kubernetes tools built on the official Python client.

The reader only ever calls ``list_*`` and ``read_*`` API methods (enforced by a
unit test), and the service account it runs as is bound to a ClusterRole that
grants ``get``/``list``/``watch`` only (see ``deploy/kubernetes/rbac.yaml``).
Secrets are not in that role, so they cannot be read even by mistake.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, ClassVar, Protocol

from triage_agent.tools.base import Tool, ToolError, ToolOutput

_DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
_SELECTOR = re.compile(r"^[A-Za-z0-9_./=!,()\- ]{0,300}$")
REQUEST_TIMEOUT = 8


class KubernetesReader(Protocol):
    async def list_pods(
        self, namespace: str, label_selector: str | None
    ) -> list[dict[str, Any]]: ...

    async def get_pod(self, namespace: str, name: str) -> dict[str, Any]: ...

    async def list_events(
        self, namespace: str, object_name: str | None
    ) -> list[dict[str, Any]]: ...

    async def rollout_history(self, namespace: str, deployment: str) -> list[dict[str, Any]]: ...


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value else None)


def _container_state(state: Any) -> dict[str, Any] | None:
    if state is None:
        return None
    if state.waiting:
        return {"waiting": state.waiting.reason, "message": (state.waiting.message or "")[:300]}
    if state.terminated:
        t = state.terminated
        return {
            "terminated": t.reason,
            "exit_code": t.exit_code,
            "finished_at": _iso(t.finished_at),
        }
    if state.running:
        return {"running_since": _iso(state.running.started_at)}
    return None


def summarise_pod(pod: Any, *, detailed: bool = False) -> dict[str, Any]:
    status = pod.status
    statuses = status.container_statuses or []
    summary: dict[str, Any] = {
        "name": pod.metadata.name,
        "phase": status.phase,
        "node": pod.spec.node_name,
        "started": _iso(status.start_time),
        "restarts": sum(cs.restart_count or 0 for cs in statuses),
        "ready": all(cs.ready for cs in statuses) if statuses else False,
        "owner": next((o.name for o in (pod.metadata.owner_references or [])), None),
    }
    containers = []
    specs = {c.name: c for c in pod.spec.containers}
    for cs in statuses:
        entry: dict[str, Any] = {
            "name": cs.name,
            "image": cs.image,
            "ready": cs.ready,
            "restarts": cs.restart_count,
            "state": _container_state(cs.state),
        }
        if detailed:
            entry["last_state"] = _container_state(cs.last_state)
            spec = specs.get(cs.name)
            if spec is not None and spec.resources is not None:
                entry["limits"] = spec.resources.limits or {}
                entry["requests"] = spec.resources.requests or {}
        containers.append(entry)
    summary["containers"] = containers
    if detailed:
        summary["conditions"] = [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (status.conditions or [])
            if c.status != "True"
        ]
        summary["labels"] = pod.metadata.labels or {}
    return summary


class KubeClientReader:
    """Implements ``KubernetesReader`` with the official (synchronous) client."""

    def __init__(self, core_api: Any = None, apps_api: Any = None) -> None:
        if core_api is None or apps_api is None:
            from kubernetes import client, config  # imported lazily: optional at runtime

            try:
                config.load_incluster_config()  # type: ignore[no-untyped-call]
            except config.ConfigException:
                config.load_kube_config()  # type: ignore[no-untyped-call]
            core_api = core_api or client.CoreV1Api()
            apps_api = apps_api or client.AppsV1Api()
        self.core = core_api
        self.apps = apps_api

    async def list_pods(self, namespace: str, label_selector: str | None) -> list[dict[str, Any]]:
        resp = await asyncio.to_thread(
            self.core.list_namespaced_pod,
            namespace,
            label_selector=label_selector or "",
            limit=50,
            _request_timeout=REQUEST_TIMEOUT,
        )
        return [summarise_pod(p) for p in resp.items]

    async def get_pod(self, namespace: str, name: str) -> dict[str, Any]:
        pod = await asyncio.to_thread(
            self.core.read_namespaced_pod, name, namespace, _request_timeout=REQUEST_TIMEOUT
        )
        return summarise_pod(pod, detailed=True)

    async def list_events(self, namespace: str, object_name: str | None) -> list[dict[str, Any]]:
        selector = f"involvedObject.name={object_name}" if object_name else ""
        resp = await asyncio.to_thread(
            self.core.list_namespaced_event,
            namespace,
            field_selector=selector,
            limit=100,
            _request_timeout=REQUEST_TIMEOUT,
        )
        events = []
        for ev in resp.items:
            events.append(
                {
                    "type": ev.type,
                    "reason": ev.reason,
                    "object": f"{ev.involved_object.kind}/{ev.involved_object.name}",
                    "message": (ev.message or "")[:300],
                    "count": ev.count,
                    "last_seen": _iso(ev.last_timestamp or ev.event_time),
                }
            )
        events.sort(key=lambda e: e["last_seen"] or "")
        return events[-40:]

    async def rollout_history(self, namespace: str, deployment: str) -> list[dict[str, Any]]:
        dep = await asyncio.to_thread(
            self.apps.read_namespaced_deployment,
            deployment,
            namespace,
            _request_timeout=REQUEST_TIMEOUT,
        )
        match = dep.spec.selector.match_labels or {}
        selector = ",".join(f"{k}={v}" for k, v in sorted(match.items()))
        rs_list = await asyncio.to_thread(
            self.apps.list_namespaced_replica_set,
            namespace,
            label_selector=selector,
            _request_timeout=REQUEST_TIMEOUT,
        )
        history = []
        for rs in rs_list.items:
            owners = rs.metadata.owner_references or []
            if not any(o.kind == "Deployment" and o.name == deployment for o in owners):
                continue
            annotations = rs.metadata.annotations or {}
            history.append(
                {
                    "revision": int(annotations.get("deployment.kubernetes.io/revision", "0")),
                    "replicaset": rs.metadata.name,
                    "created": _iso(rs.metadata.creation_timestamp),
                    "images": [c.image for c in rs.spec.template.spec.containers],
                    "replicas": rs.status.replicas or 0,
                    "ready": rs.status.ready_replicas or 0,
                    "change_cause": annotations.get("kubernetes.io/change-cause"),
                }
            )
        history.sort(key=lambda h: h["revision"])
        return history[-10:]


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, default=str)


class _K8sTool(Tool):
    def __init__(
        self,
        reader: KubernetesReader,
        allowed_namespaces: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.reader = reader
        self.allowed_namespaces = set(allowed_namespaces)
        self.clock = clock

    def _namespace(self, args: dict[str, Any]) -> str:
        ns = str(args["namespace"])
        if not _DNS1123.match(ns):
            raise ToolError("invalid namespace name")
        if self.allowed_namespaces and ns not in self.allowed_namespaces:
            raise ToolError(f"namespace '{ns}' is outside the configured allow-list")
        return ns

    @staticmethod
    def _name(value: Any, what: str) -> str:
        name = str(value)
        if not _DNS1123.match(name):
            raise ToolError(f"invalid {what} name")
        return name


class K8sGetPodsTool(_K8sTool):
    name = "k8s_get_pods"
    description = "List pods in a namespace (optionally by label selector) with phase, readiness and restarts."
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "maxLength": 253},
            "label_selector": {
                "type": "string",
                "description": "e.g. app=checkout",
                "maxLength": 300,
            },
        },
        "required": ["namespace"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        ns = self._namespace(args)
        selector = args.get("label_selector")
        if selector and not _SELECTOR.match(selector):
            raise ToolError("invalid label selector")
        pods = await self.reader.list_pods(ns, selector)
        return ToolOutput(text=_dump(pods), query=f"pods ns={ns} selector={selector or '*'}")


class K8sDescribePodTool(_K8sTool):
    name = "k8s_describe_pod"
    description = (
        "Describe one pod: container states, last termination reason and exit code "
        "(e.g. OOMKilled), resource limits, and failing conditions."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "maxLength": 253},
            "pod": {"type": "string", "maxLength": 253},
        },
        "required": ["namespace", "pod"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        ns = self._namespace(args)
        pod = self._name(args["pod"], "pod")
        return ToolOutput(text=_dump(await self.reader.get_pod(ns, pod)), query=f"pod {ns}/{pod}")


class K8sGetEventsTool(_K8sTool):
    name = "k8s_get_events"
    description = (
        "Recent Kubernetes events in a namespace, optionally for one object name "
        "(pod, deployment, node). Shows reasons such as BackOff, OOMKilling, FailedScheduling."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "maxLength": 253},
            "object_name": {"type": "string", "maxLength": 253},
        },
        "required": ["namespace"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        ns = self._namespace(args)
        obj = args.get("object_name")
        name = self._name(obj, "object") if obj else None
        events = await self.reader.list_events(ns, name)
        return ToolOutput(
            text=_dump(events) if events else "no events",
            query=f"events ns={ns} object={name or '*'}",
        )


class K8sRolloutHistoryTool(_K8sTool):
    name = "k8s_rollout_history"
    description = (
        "Rollout history of a Deployment: revision, creation time, images and change cause. "
        "Use to check whether a deploy happened shortly before the alert."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "namespace": {"type": "string", "maxLength": 253},
            "deployment": {"type": "string", "maxLength": 253},
        },
        "required": ["namespace", "deployment"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        ns = self._namespace(args)
        dep = self._name(args["deployment"], "deployment")
        history = await self.reader.rollout_history(ns, dep)
        now = self.clock() if self.clock else None
        if now is not None:
            for entry in history:
                created = entry.get("created")
                if created:
                    age = now - datetime.fromisoformat(created)
                    entry["age_minutes"] = round(age.total_seconds() / 60)
        return ToolOutput(
            text=_dump(history) if history else "no rollout history found",
            query=f"rollout history {ns}/{dep}",
        )
