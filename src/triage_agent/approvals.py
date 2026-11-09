"""Remediation proposals, human approval, and a tamper-evident audit log.

The agent never executes anything. Each action it suggests becomes a
``Proposal``; the policy gate (``security.evaluate_action``) marks it either
pending approval or blocked. A human approves or rejects through the API, and
every step is appended to a JSONL audit log in which each entry carries the
SHA-256 of the previous one, so edits or deletions are detectable.

Approving a proposal records the decision only. Carrying it out is left to
the responder or to a separate, separately-permissioned system on purpose:
the triage service holds read-only credentials and should stay that way.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from triage_agent import metrics
from triage_agent.models import ApprovalDecision, Proposal, ProposalStatus, ProposedAction
from triage_agent.security import evaluate_action

GENESIS = "0" * 64


class AuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                for line in path.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        self._entries.append(json.loads(line))

    @staticmethod
    def _digest(entry: dict[str, Any]) -> str:
        body = {k: v for k, v in entry.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        with self._lock:
            prev = self._entries[-1]["hash"] if self._entries else GENESIS
            entry: dict[str, Any] = {
                "seq": len(self._entries),
                "ts": datetime.now(UTC).isoformat(),
                "event": event,
                **fields,
                "prev_hash": prev,
            }
            entry["hash"] = self._digest(entry)
            self._entries.append(entry)
            if self.path is not None:
                with self.path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            return entry

    def entries(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self._entries[-limit:])

    def verify(self) -> bool:
        prev = GENESIS
        for entry in self._entries:
            if entry.get("prev_hash") != prev or self._digest(entry) != entry.get("hash"):
                return False
            prev = entry["hash"]
        return True


class ProposalError(Exception):
    pass


class ProposalStore:
    def __init__(self, audit: AuditLog) -> None:
        self.audit = audit
        self._proposals: dict[str, Proposal] = {}

    def register(self, triage_id: str, actions: list[ProposedAction]) -> list[Proposal]:
        created = []
        for action in actions:
            decision = evaluate_action(action)
            proposal = Proposal(
                id=uuid.uuid4().hex[:12],
                triage_id=triage_id,
                action=action,
                status=decision.status,
                policy_reason=decision.reason,
                created_at=datetime.now(UTC),
            )
            self._proposals[proposal.id] = proposal
            metrics.PROPOSALS.labels(status=proposal.status.value).inc()
            self.audit.append(
                "proposal_created",
                proposal_id=proposal.id,
                triage_id=triage_id,
                kind=action.kind.value,
                title=action.title,
                command=action.command,
                status=proposal.status.value,
                policy_reason=decision.reason,
            )
            created.append(proposal)
        return created

    def get(self, proposal_id: str) -> Proposal | None:
        return self._proposals.get(proposal_id)

    def list(self, status: ProposalStatus | None = None) -> list[Proposal]:
        items = sorted(self._proposals.values(), key=lambda p: p.created_at, reverse=True)
        return [p for p in items if status is None or p.status is status]

    def decide(self, proposal_id: str, *, approve: bool, decision: ApprovalDecision) -> Proposal:
        proposal = self._proposals.get(proposal_id)
        if proposal is None:
            raise ProposalError("proposal not found")
        if proposal.status is ProposalStatus.BLOCKED:
            self.audit.append(
                "approval_refused_blocked",
                proposal_id=proposal_id,
                approver=decision.approver,
            )
            raise ProposalError("proposal is blocked by policy and cannot be approved")
        if proposal.status is not ProposalStatus.PENDING_APPROVAL:
            raise ProposalError(f"proposal already {proposal.status.value}")
        proposal.status = ProposalStatus.APPROVED if approve else ProposalStatus.REJECTED
        proposal.decided_by = decision.approver
        proposal.decided_at = datetime.now(UTC)
        proposal.comment = decision.comment
        metrics.APPROVAL_DECISIONS.labels(decision=proposal.status.value).inc()
        self.audit.append(
            f"proposal_{proposal.status.value}",
            proposal_id=proposal_id,
            triage_id=proposal.triage_id,
            approver=decision.approver,
            comment=decision.comment,
            command=proposal.action.command,
        )
        return proposal
