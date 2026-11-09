from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import RUNBOOK_DIR, scenario
from triage_agent.app import create_app
from triage_agent.config import Settings
from triage_agent.llm import FakeLLM
from triage_agent.models import ActionKind, ProposedAction
from triage_agent.scenarios import fixture_service, webhook_for

AUTH = {"Authorization": "Bearer test-approval-token"}


def make_client(settings: Settings, name: str = "bad-deploy-error-spike") -> tuple[TestClient, Any]:
    service, *_ = fixture_service(scenario(name), FakeLLM(), settings, runbook_dir=RUNBOOK_DIR)
    return TestClient(create_app(settings, service)), service


def wait_done(client: TestClient, triage_id: str) -> dict[str, Any]:
    for _ in range(200):
        body: dict[str, Any] = client.get(f"/triages/{triage_id}").json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError("triage did not finish")


def test_webhook_to_verdict_to_approval(settings: Settings) -> None:
    client, _ = make_client(settings)
    with client:
        payload = webhook_for(scenario("bad-deploy-error-spike")).model_dump(mode="json")
        resp = client.post("/webhook/alertmanager", json=payload)
        assert resp.status_code == 202
        triage_id = resp.json()["triage_ids"][0]

        # Re-sending the same notification does not start a second triage.
        again = client.post("/webhook/alertmanager", json=payload).json()
        assert again["triage_ids"] == []
        assert again["duplicates"] == 1

        run = wait_done(client, triage_id)
        assert run["result"]["verdict"]["cause_category"] == "bad_deploy"
        proposal_id = run["proposal_ids"][0]
        assert client.get(f"/proposals/{proposal_id}").json()["status"] == "pending_approval"

        decision = {"approver": "on-call engineer", "comment": "rolling back"}
        assert client.post(f"/proposals/{proposal_id}/approve", json=decision).status_code == 401
        approved = client.post(f"/proposals/{proposal_id}/approve", json=decision, headers=AUTH)
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        # Decisions are final.
        assert (
            client.post(f"/proposals/{proposal_id}/reject", json=decision, headers=AUTH).status_code
            == 409
        )

        audit = client.get("/audit", headers=AUTH).json()
        assert audit["chain_valid"] is True
        events = [e["event"] for e in audit["entries"]]
        assert {
            "alerts_received",
            "triage_completed",
            "proposal_created",
            "proposal_approved",
        } <= set(events)

        metrics = client.get("/metrics/").text
        assert "triage_runs_total" in metrics
        assert "triage_tool_calls_total" in metrics


def test_blocked_proposals_cannot_be_approved(settings: Settings) -> None:
    client, service = make_client(settings)
    with client:
        [proposal] = service.proposals.register(
            "t1",
            [
                ProposedAction(
                    title="reset",
                    kind=ActionKind.OTHER,
                    rationale="logs said so",
                    command="kubectl delete namespace shop",
                )
            ],
        )
        assert proposal.status.value == "blocked"
        resp = client.post(
            f"/proposals/{proposal.id}/approve", json={"approver": "x"}, headers=AUTH
        )
        assert resp.status_code == 409
        assert "blocked" in resp.json()["detail"]


def test_webhook_token_enforced_when_configured(settings: Settings) -> None:
    client, _ = make_client(settings.model_copy(update={"webhook_token": "am-secret"}))
    with client:
        body = {"alerts": []}
        assert client.post("/webhook/alertmanager", json=body).status_code == 401
        ok = client.post(
            "/webhook/alertmanager", json=body, headers={"Authorization": "Bearer am-secret"}
        )
        assert ok.status_code == 202


def test_approvals_disabled_without_token(settings: Settings) -> None:
    client, _ = make_client(settings.model_copy(update={"approval_token": None}))
    with client:
        resp = client.post("/proposals/x/approve", json={"approver": "a"}, headers=AUTH)
        assert resp.status_code == 403
