"""FastAPI application: Alertmanager webhook, triage results, approvals, audit, metrics."""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from prometheus_client import make_asgi_app

from triage_agent.approvals import ProposalError
from triage_agent.config import Settings
from triage_agent.models import (
    AlertmanagerWebhook,
    ApprovalDecision,
    Proposal,
    ProposalStatus,
)
from triage_agent.service import IngestSummary, TriageRun, TriageService, build_service, run_summary
from triage_agent.telemetry import setup_tracing


def _bearer_ok(header: str | None, expected: str) -> bool:
    if not header or not header.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(header[7:].strip().encode(), expected.encode())


def create_app(settings: Settings | None = None, service: TriageService | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_tracing()
    svc = service or build_service(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await svc.start()
        yield
        await svc.stop()

    app = FastAPI(
        title="aiops-triage-agent",
        version="0.1.0",
        description="Read-only alert triage agent. Proposals require human approval.",
        lifespan=lifespan,
    )
    app.state.service = svc
    app.mount("/metrics", make_asgi_app())

    def require_webhook_token(authorization: Annotated[str | None, Header()] = None) -> None:
        if settings.webhook_token and not _bearer_ok(authorization, settings.webhook_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid webhook token")

    def require_approver(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.approval_token:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "approvals are disabled (no token configured)"
            )
        if not _bearer_ok(authorization, settings.approval_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid approval token")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/webhook/alertmanager",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_webhook_token)],
    )
    async def alertmanager_webhook(payload: AlertmanagerWebhook) -> IngestSummary:
        return svc.ingest(payload)

    @app.get("/triages")
    async def list_triages(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict[str, Any]]:
        runs = list(svc.runs.values())[-limit:]
        return [run_summary(r) for r in reversed(runs)]

    @app.get("/triages/{triage_id}")
    async def get_triage(triage_id: str) -> TriageRun:
        run = svc.runs.get(triage_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "triage not found")
        return run

    @app.get("/proposals")
    async def list_proposals(
        status_filter: Annotated[ProposalStatus | None, Query(alias="status")] = None,
    ) -> list[Proposal]:
        return svc.proposals.list(status_filter)

    @app.get("/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str) -> Proposal:
        proposal = svc.proposals.get(proposal_id)
        if proposal is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "proposal not found")
        return proposal

    async def _decide(proposal_id: str, decision: ApprovalDecision, approve: bool) -> Proposal:
        try:
            return svc.proposals.decide(proposal_id, approve=approve, decision=decision)
        except ProposalError as exc:
            code = (
                status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT
            )
            raise HTTPException(code, str(exc)) from exc

    @app.post("/proposals/{proposal_id}/approve", dependencies=[Depends(require_approver)])
    async def approve(proposal_id: str, decision: ApprovalDecision) -> Proposal:
        return await _decide(proposal_id, decision, approve=True)

    @app.post("/proposals/{proposal_id}/reject", dependencies=[Depends(require_approver)])
    async def reject(proposal_id: str, decision: ApprovalDecision) -> Proposal:
        return await _decide(proposal_id, decision, approve=False)

    @app.get("/audit", dependencies=[Depends(require_approver)])
    async def audit(limit: Annotated[int, Query(ge=1, le=1000)] = 200) -> dict[str, Any]:
        return {"chain_valid": svc.audit.verify(), "entries": svc.audit.entries(limit)}

    return app
