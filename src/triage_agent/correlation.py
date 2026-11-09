"""Alert-storm correlation and de-duplication.

Runs *before* any LLM call. A storm of 40 alerts from one bad rollout should
produce one triage, not 40. Alerts are grouped by a configurable label key
(default ``alertname, namespace, workload``) inside a sliding time window, and
repeat notifications for the same fingerprint are counted, not re-triaged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from triage_agent.models import Alert, AlertGroup

GroupKey = tuple[tuple[str, str], ...]


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class CorrelationResult:
    new_groups: list[AlertGroup] = field(default_factory=list)
    updated_groups: list[AlertGroup] = field(default_factory=list)
    duplicates: int = 0
    resolved: int = 0
    ignored: int = 0


class Correlator:
    def __init__(
        self,
        group_by: Iterable[str],
        window_seconds: int,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.group_by = tuple(group_by)
        self.window = timedelta(seconds=window_seconds)
        self._clock = clock
        self._groups: dict[GroupKey, AlertGroup] = {}

    def key_for(self, alert: Alert) -> GroupKey:
        labels = alert.derived_labels()
        return tuple((name, labels.get(name, "")) for name in self.group_by)

    def ingest(self, alerts: Iterable[Alert]) -> CorrelationResult:
        now = self._clock()
        self._expire(now)
        result = CorrelationResult()
        touched: dict[str, AlertGroup] = {}
        created: set[str] = set()

        for alert in alerts:
            key = self.key_for(alert)
            group = self._groups.get(key)

            if alert.status != "firing":
                if group is None:
                    result.ignored += 1
                    continue
                for existing in group.alerts:
                    if existing.identity() == alert.identity():
                        existing.status = alert.status
                        existing.endsAt = alert.endsAt
                result.resolved += 1
                touched[group.id] = group
                continue

            if group is None:
                group = AlertGroup(
                    id=uuid.uuid4().hex[:12],
                    key=dict(key),
                    alerts=[alert],
                    first_seen=now,
                    last_seen=now,
                )
                self._groups[key] = group
                created.add(group.id)
                result.new_groups.append(group)
                continue

            group.last_seen = now
            if any(existing.identity() == alert.identity() for existing in group.alerts):
                group.duplicates_suppressed += 1
                result.duplicates += 1
            else:
                group.alerts.append(alert)
            if group.id not in created:
                touched[group.id] = group

        result.updated_groups = list(touched.values())
        return result

    def get(self, group_id: str) -> AlertGroup | None:
        return next((g for g in self._groups.values() if g.id == group_id), None)

    def open_groups(self) -> list[AlertGroup]:
        return list(self._groups.values())

    def _expire(self, now: datetime) -> None:
        stale = [k for k, g in self._groups.items() if now - g.last_seen > self.window]
        for key in stale:
            del self._groups[key]
