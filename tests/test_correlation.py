from __future__ import annotations

from datetime import UTC, datetime, timedelta

from triage_agent.correlation import Correlator
from triage_agent.models import Alert, workload_from_pod


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def alert(
    name: str = "HighErrorRate", fp: str = "a", status: str = "firing", **labels: str
) -> Alert:
    return Alert(labels={"alertname": name, **labels}, fingerprint=fp, status=status)


def test_workload_from_pod_names() -> None:
    assert workload_from_pod("cart-7f9c6d5b8d-k2l9p") == "cart"
    assert workload_from_pod("node-exporter-x2x9k") == "node-exporter"
    assert workload_from_pod("postgres-0") == "postgres-0"
    assert workload_from_pod("api-server") == "api-server"


def test_storm_from_one_workload_becomes_one_group() -> None:
    corr = Correlator(["alertname", "namespace", "workload"], 300, Clock())
    alerts = [
        alert("KubePodCrashLooping", fp=f"fp{i}", namespace="ml", pod=f"rec-7c9d8f6b5d-x{i}b2c")
        for i in "245678"
    ]
    result = corr.ingest(alerts)
    assert len(result.new_groups) == 1
    assert len(result.new_groups[0].alerts) == 6


def test_repeat_notifications_are_counted_as_duplicates() -> None:
    corr = Correlator(["alertname", "namespace", "workload"], 300, Clock())
    corr.ingest([alert(fp="a", namespace="shop", app="cart")])
    again = corr.ingest([alert(fp="a", namespace="shop", app="cart")] * 3)
    assert again.new_groups == []
    assert again.duplicates == 3
    assert corr.open_groups()[0].duplicates_suppressed == 3


def test_different_workloads_are_separate_groups() -> None:
    corr = Correlator(["alertname", "namespace", "workload"], 300, Clock())
    result = corr.ingest(
        [
            alert(fp="a", namespace="shop", app="cart"),
            alert(fp="b", namespace="shop", app="checkout"),
        ]
    )
    assert len(result.new_groups) == 2


def test_group_expires_after_window_and_retriages() -> None:
    clock = Clock()
    corr = Correlator(["alertname", "namespace", "workload"], 300, clock)
    corr.ingest([alert(fp="a", namespace="shop", app="cart")])
    clock.now += timedelta(seconds=301)
    result = corr.ingest([alert(fp="a", namespace="shop", app="cart")])
    assert len(result.new_groups) == 1


def test_window_slides_while_alerts_keep_arriving() -> None:
    clock = Clock()
    corr = Correlator(["alertname", "namespace", "workload"], 300, clock)
    corr.ingest([alert(fp="a", namespace="shop", app="cart")])
    for _ in range(3):
        clock.now += timedelta(seconds=200)
        assert corr.ingest([alert(fp="a", namespace="shop", app="cart")]).new_groups == []


def test_resolved_alerts_update_status_and_never_open_groups() -> None:
    corr = Correlator(["alertname", "namespace", "workload"], 300, Clock())
    assert corr.ingest([alert(fp="z", status="resolved", namespace="x", app="y")]).ignored == 1
    corr.ingest([alert(fp="a", namespace="shop", app="cart")])
    result = corr.ingest([alert(fp="a", status="resolved", namespace="shop", app="cart")])
    assert result.resolved == 1
    assert corr.open_groups()[0].firing == []
