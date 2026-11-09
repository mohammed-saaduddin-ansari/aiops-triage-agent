"""Read-only Prometheus tools (instant and range queries over the HTTP API)."""

from __future__ import annotations

import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar, Protocol
from urllib.parse import quote

import httpx

from triage_agent.tools.base import Tool, ToolError, ToolOutput

MAX_SERIES = 20
MAX_RANGE_MINUTES = 360
MIN_STEP_SECONDS = 15


@dataclass
class Series:
    labels: dict[str, str]
    points: list[tuple[float, float]] = field(default_factory=list)  # (unix_ts, value)


class PrometheusBackend(Protocol):
    async def instant(self, query: str, at: datetime) -> list[Series]: ...

    async def range(
        self, query: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[Series]: ...


class HttpPrometheusBackend:
    """Talks to ``/api/v1/query`` and ``/api/v1/query_range``. GET only."""

    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=10.0)

    async def _get(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        resp = await self._client.get(f"{self.base_url}{path}", params=params)
        if resp.status_code == 400:
            body = resp.json()
            raise ToolError(f"Prometheus rejected the query: {body.get('error', 'bad request')}")
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise ToolError(f"Prometheus error: {body.get('error', 'unknown')}")
        data = body["data"]
        if data.get("resultType") == "scalar":
            ts, val = data["result"]
            return [{"metric": {}, "value": [ts, val]}]
        result: list[dict[str, Any]] = data.get("result", [])
        return result

    async def instant(self, query: str, at: datetime) -> list[Series]:
        rows = await self._get("/api/v1/query", {"query": query, "time": at.timestamp()})
        return [
            Series(
                labels=row.get("metric", {}),
                points=[(float(row["value"][0]), float(row["value"][1]))],
            )
            for row in rows
        ]

    async def range(
        self, query: str, start: datetime, end: datetime, step_seconds: int
    ) -> list[Series]:
        rows = await self._get(
            "/api/v1/query_range",
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step_seconds,
            },
        )
        return [
            Series(
                labels=row.get("metric", {}),
                points=[(float(ts), float(v)) for ts, v in row.get("values", [])],
            )
            for row in rows
        ]


def _fmt_labels(labels: dict[str, str]) -> str:
    if not labels:
        return "{}"
    inner = ", ".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _fmt_num(value: float) -> str:
    if value != value:  # NaN
        return "NaN"
    if abs(value) >= 1000 or (abs(value) < 0.01 and value != 0):
        return f"{value:.3g}"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def graph_link(external_url: str | None, query: str, range_minutes: int = 60) -> str | None:
    if not external_url:
        return None
    return (
        f"{external_url.rstrip('/')}/graph?g0.expr={quote(query, safe='')}"
        f"&g0.tab=0&g0.range_input={range_minutes}m"
    )


class PrometheusQueryTool(Tool):
    name = "prometheus_query"
    description = (
        "Run a read-only PromQL instant query and return the current value of each series "
        "(at most 20). Use for 'what is the value now' questions."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "PromQL expression", "maxLength": 1000},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        backend: PrometheusBackend,
        clock: Callable[[], datetime],
        external_url: str | None = None,
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.external_url = external_url

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        query = args["query"].strip()
        series = await self.backend.instant(query, self.clock())
        if not series:
            text = f"query: {query}\nresult: no data (empty vector)"
        else:
            lines = [f"query: {query}", f"series: {len(series)}"]
            for s in series[:MAX_SERIES]:
                value = s.points[-1][1] if s.points else float("nan")
                lines.append(f"{_fmt_labels(s.labels)} => {_fmt_num(value)}")
            if len(series) > MAX_SERIES:
                lines.append(f"... {len(series) - MAX_SERIES} more series omitted")
            text = "\n".join(lines)
        return ToolOutput(text=text, query=query, link=graph_link(self.external_url, query))


class PrometheusRangeQueryTool(Tool):
    name = "prometheus_range_query"
    description = (
        "Run a read-only PromQL range query over the last N minutes. Returns, per series, "
        "first/last/min/max/mean and a down-sampled trend. Use to see when something changed."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "PromQL expression", "maxLength": 1000},
            "minutes": {
                "type": "integer",
                "description": f"Look-back window in minutes (1-{MAX_RANGE_MINUTES}, default 60)",
            },
            "step_seconds": {
                "type": "integer",
                "description": f"Resolution in seconds (>= {MIN_STEP_SECONDS}, default 60)",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(
        self,
        backend: PrometheusBackend,
        clock: Callable[[], datetime],
        external_url: str | None = None,
    ) -> None:
        self.backend = backend
        self.clock = clock
        self.external_url = external_url

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        query = args["query"].strip()
        minutes = max(1, min(int(args.get("minutes", 60)), MAX_RANGE_MINUTES))
        step = max(MIN_STEP_SECONDS, int(args.get("step_seconds", 60)))
        end = self.clock()
        start = end - timedelta(minutes=minutes)
        series = await self.backend.range(query, start, end, step)
        lines = [f"query: {query}", f"window: last {minutes}m, step {step}s"]
        if not series:
            lines.append("result: no data")
        for s in series[:MAX_SERIES]:
            values = [v for _, v in s.points]
            if not values:
                continue
            lines.append(
                f"{_fmt_labels(s.labels)} first={_fmt_num(values[0])} last={_fmt_num(values[-1])} "
                f"min={_fmt_num(min(values))} max={_fmt_num(max(values))} "
                f"mean={_fmt_num(statistics.fmean(values))}"
            )
            sample_every = max(1, len(s.points) // 12)
            trend = ", ".join(
                f"{_relative(ts, end)}:{_fmt_num(v)}" for ts, v in s.points[::sample_every]
            )
            lines.append(f"  trend: {trend}")
        if len(series) > MAX_SERIES:
            lines.append(f"... {len(series) - MAX_SERIES} more series omitted")
        return ToolOutput(
            text="\n".join(lines), query=query, link=graph_link(self.external_url, query, minutes)
        )


def _relative(ts: float, end: datetime) -> str:
    delta_min = round((end.timestamp() - ts) / 60)
    return "now" if delta_min <= 0 else f"-{delta_min}m"
