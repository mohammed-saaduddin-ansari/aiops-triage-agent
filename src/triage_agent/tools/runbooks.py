"""Runbook lookup over markdown files with YAML front matter.

Deliberately simple retrieval: an exact alert-name match wins, otherwise a
TF-IDF-style keyword overlap. A small, curated runbook set does not need a
vector database, and simple retrieval is easy to debug when it picks the wrong page.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import yaml

from triage_agent.tools.base import Tool, ToolError, ToolOutput

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "with",
    ]
)


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


@dataclass
class Runbook:
    path: Path
    title: str
    alerts: list[str]
    tags: list[str]
    body: str
    terms: Counter[str] = field(default_factory=Counter)


def _parse(path: Path) -> Runbook:
    text = path.read_text(encoding="utf-8")
    meta: dict[str, Any] = {}
    body = text
    if text.startswith("---\n"):
        _, front, body = text.split("---\n", 2)
        meta = yaml.safe_load(front) or {}
    title = str(meta.get("title") or path.stem.replace("-", " "))
    alerts = [str(a) for a in meta.get("alerts", [])]
    tags = [str(t) for t in meta.get("tags", [])]
    rb = Runbook(path=path, title=title, alerts=alerts, tags=tags, body=body.strip())
    rb.terms = Counter(_tokens(" ".join([title, *alerts, *tags, body])))
    return rb


class RunbookIndex:
    def __init__(self, runbooks: list[Runbook]) -> None:
        self.runbooks = runbooks
        doc_freq: Counter[str] = Counter()
        for rb in runbooks:
            doc_freq.update(set(rb.terms))
        n = max(len(runbooks), 1)
        self._idf = {term: math.log(1 + n / df) for term, df in doc_freq.items()}

    @classmethod
    def from_dir(cls, directory: Path) -> RunbookIndex:
        if not directory.is_dir():
            return cls([])
        return cls(
            [_parse(p) for p in sorted(directory.glob("*.md")) if p.name.lower() != "readme.md"]
        )

    def search(
        self, alertname: str | None, query: str | None, k: int = 2
    ) -> list[tuple[float, Runbook]]:
        q_terms = _tokens(" ".join(filter(None, [alertname, query])))
        scored: list[tuple[float, Runbook]] = []
        for rb in self.runbooks:
            score = 0.0
            if alertname and alertname.lower() in (a.lower() for a in rb.alerts):
                score += 100.0
            for term in q_terms:
                if term in rb.terms:
                    score += self._idf.get(term, 0.0) * (1 + math.log(rb.terms[term]))
            if score > 0:
                scored.append((score, rb))
        scored.sort(key=lambda pair: (-pair[0], pair[1].title))
        return scored[:k]


class RunbookLookupTool(Tool):
    name = "runbook_lookup"
    description = (
        "Find the team runbook for an alert. Pass the alert name and/or keywords. "
        "Returns the best-matching runbook sections (likely causes, checks, safe actions)."
    )
    input_schema: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "alertname": {"type": "string", "maxLength": 200},
            "query": {"type": "string", "description": "free-text keywords", "maxLength": 500},
        },
        "additionalProperties": False,
    }

    def __init__(self, index: RunbookIndex, max_chars_per_doc: int = 2500) -> None:
        self.index = index
        self.max_chars_per_doc = max_chars_per_doc

    async def run(self, args: dict[str, Any]) -> ToolOutput:
        alertname = args.get("alertname")
        query = args.get("query")
        if not alertname and not query:
            raise ToolError("provide 'alertname' or 'query'")
        hits = self.index.search(alertname, query)
        if not hits:
            return ToolOutput(text="no runbook matched", query=f"runbook {alertname or query}")
        parts = []
        for score, rb in hits:
            body = rb.body[: self.max_chars_per_doc]
            parts.append(
                f"## {rb.title} (file: runbooks/{rb.path.name}, score {score:.1f})\n{body}"
            )
        return ToolOutput(
            text="\n\n".join(parts),
            query=f"runbook {alertname or query}",
            link=f"runbooks/{hits[0][1].path.name}",
        )
