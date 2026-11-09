"""Tool abstraction and the registry that enforces the agent's guard-rails.

Every tool call from the model goes through ``ToolRegistry.execute``, which:

* rejects tools not on the allow-list (the model only *sees* allowed tools,
  but a model can still emit an unknown name, so we check again);
* validates arguments against the tool's JSON schema (subset);
* applies a hard timeout;
* redacts secrets, truncates, scans for injection, and wraps the result in
  untrusted-data delimiters before it can reach the model;
* records a span and metrics for the call.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from opentelemetry import trace

from triage_agent import metrics
from triage_agent.models import ToolCallRecord
from triage_agent.security import redact, scan_for_injection, truncate, wrap_untrusted

tracer = trace.get_tracer(__name__)


class ToolError(Exception):
    """An expected failure whose message is safe to show the model."""


@dataclass(frozen=True)
class ToolOutput:
    text: str
    query: str | None = None
    link: str | None = None


class Tool(ABC):
    name: str
    description: str
    input_schema: ClassVar[dict[str, Any]]

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    @abstractmethod
    async def run(self, args: dict[str, Any]) -> ToolOutput: ...


@dataclass
class ExecutedCall:
    content: str  # what the model sees (wrapped, redacted, truncated)
    is_error: bool
    record: ToolCallRecord
    query: str | None = None
    link: str | None = None
    raw_redacted: str = field(default="", repr=False)


_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate_args(schema: dict[str, Any], args: Any) -> None:
    """Validate the small JSON-schema subset our tools use. Raises ToolError."""
    if not isinstance(args, dict):
        raise ToolError("arguments must be a JSON object")
    props: dict[str, Any] = schema.get("properties", {})
    for required in schema.get("required", []):
        if required not in args:
            raise ToolError(f"missing required argument '{required}'")
    if schema.get("additionalProperties") is False:
        extra = sorted(set(args) - set(props))
        if extra:
            raise ToolError(f"unexpected arguments: {extra}")
    for key, value in args.items():
        prop = props.get(key)
        if prop is None:
            continue
        expected = _JSON_TYPES.get(prop.get("type", ""))
        if expected is not None and (
            not isinstance(value, expected)
            or (isinstance(value, bool) and prop["type"] != "boolean")
        ):
            raise ToolError(f"argument '{key}' must be of type {prop['type']}")
        if "enum" in prop and value not in prop["enum"]:
            raise ToolError(f"argument '{key}' must be one of {prop['enum']}")
        if isinstance(value, str) and len(value) > prop.get("maxLength", 2000):
            raise ToolError(f"argument '{key}' is too long")


class ToolRegistry:
    def __init__(
        self,
        tools: Iterable[Tool],
        *,
        allowed: Iterable[str],
        timeout_seconds: float,
        max_chars: int,
    ) -> None:
        allowed_set = set(allowed)
        self._tools = {t.name: t for t in tools if t.name in allowed_set}
        self.timeout_seconds = timeout_seconds
        self.max_chars = max_chars

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        # Sorted for a stable prompt prefix (helps prompt caching).
        return [self._tools[name].spec() for name in self.names]

    async def execute(self, *, call_id: str, name: str, args: Any, nonce: str) -> ExecutedCall:
        started = time.perf_counter()
        with tracer.start_as_current_span(f"tool.{name}") as span:
            span.set_attribute("tool.name", name)
            span.set_attribute("tool.call_id", call_id)
            span.set_attribute("tool.args", json.dumps(args, sort_keys=True, default=str)[:500])
            is_error = False
            output: ToolOutput | None = None
            try:
                tool = self._tools.get(name)
                if tool is None:
                    raise ToolError(
                        f"tool '{name}' is not on the allow-list; available: {self.names}"
                    )
                validate_args(tool.input_schema, args)
                output = await asyncio.wait_for(tool.run(args), timeout=self.timeout_seconds)
                body = output.text
                status = "ok"
            except ToolError as exc:
                is_error, body, status = True, f"ERROR: {exc}", "error"
            except TimeoutError:
                is_error, body, status = (
                    True,
                    f"ERROR: tool timed out after {self.timeout_seconds:.0f}s",
                    "timeout",
                )
            except Exception as exc:  # backend failure; never leak a traceback to the model
                is_error, body, status = True, f"ERROR: {type(exc).__name__}", "error"
                span.record_exception(exc)

            redacted, redactions = redact(body)
            flags = scan_for_injection(redacted)
            clipped, was_truncated = truncate(redacted, self.max_chars)
            content = wrap_untrusted(
                tool=name, call_id=call_id, body=clipped, nonce=nonce, flags=flags
            )
            duration_ms = (time.perf_counter() - started) * 1000

            span.set_attribute("tool.status", status)
            span.set_attribute("tool.truncated", was_truncated)
            span.set_attribute("tool.redactions", redactions)
            span.set_attribute("tool.output_chars", len(clipped))
            if flags:
                span.set_attribute("tool.injection_flags", flags)
            metrics.TOOL_CALLS.labels(tool=name, status=status).inc()
            metrics.TOOL_LATENCY.labels(tool=name).observe(duration_ms / 1000)
            if flags:
                metrics.INJECTION_FLAGS.labels(tool=name).inc()

            record = ToolCallRecord(
                id=call_id,
                name=name,
                input=args if isinstance(args, dict) else {"_raw": str(args)},
                ok=not is_error,
                duration_ms=round(duration_ms, 2),
                truncated=was_truncated,
                redactions=redactions,
                injection_flags=flags,
                output_preview=clipped[:300],
            )
            return ExecutedCall(
                content=content,
                is_error=is_error,
                record=record,
                query=output.query if output else None,
                link=output.link if output else None,
                raw_redacted=clipped,
            )
