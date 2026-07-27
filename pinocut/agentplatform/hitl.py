"""Human-in-the-loop approval gates.

Rule from the specification, encoded rather than documented: publishing,
schedule commitment, fan-facing release, legal-sensitive and budget-affecting
actions never auto-execute. The gate's default is to *stop*; an action is
auto-approvable only when it is explicitly absent from the required set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class ActionKind(str, Enum):
    """High-impact actions the platform can propose."""

    DRAFT_ARTIFACT = "draft_artifact"  # safe: produces a draft, changes nothing
    INTERNAL_NOTIFY = "internal_notify"  # safe: notifies the crew
    SCHEDULE_COMMIT = "schedule_commit"
    CALL_SHEET_PUBLISH = "call_sheet_publish"
    ASSET_PUBLISH = "asset_publish"
    FAN_RELEASE = "fan_release"
    LEGAL_SENSITIVE = "legal_sensitive"
    BUDGET_AFFECTING = "budget_affecting"
    PARTNER_WRITE = "partner_write"  # mutating tools/call on the partner MCP


#: Actions that require a human decision before execution.
HITL_REQUIRED: frozenset[ActionKind] = frozenset(
    {
        ActionKind.SCHEDULE_COMMIT,
        ActionKind.CALL_SHEET_PUBLISH,
        ActionKind.ASSET_PUBLISH,
        ActionKind.FAN_RELEASE,
        ActionKind.LEGAL_SENSITIVE,
        ActionKind.BUDGET_AFFECTING,
        ActionKind.PARTNER_WRITE,
    }
)


class ApprovalState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NOT_REQUIRED = "not_required"


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    """What the human is being asked to approve, with the evidence to judge it."""

    request_id: str
    action: ActionKind
    project_id: str
    summary: str
    options: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    citations: list[dict] = field(default_factory=list)
    requested_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "requestId": self.request_id,
            "action": self.action.value,
            "projectId": self.project_id,
            "summary": self.summary,
            "options": list(self.options),
            "risks": list(self.risks),
            "citations": list(self.citations),
            "requestedAt": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """A human's answer. ``approver_id`` is required — decisions are attributable."""

    request_id: str
    approver_id: str
    approved: bool
    selected_option_id: str | None = None
    note: str | None = None
    decided_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def state(self) -> ApprovalState:
        return ApprovalState.APPROVED if self.approved else ApprovalState.REJECTED


class ApprovalRequired(Exception):
    """Raised when execution reaches a gate with no decision on file."""

    def __init__(self, request: ApprovalRequest):
        super().__init__(f"human approval required for {request.action.value}")
        self.request = request


class ApprovalGate:
    """Holds pending requests and resolves them against submitted decisions."""

    def __init__(self, required: frozenset[ActionKind] = HITL_REQUIRED):
        self._required = required
        self.pending: dict[str, ApprovalRequest] = {}
        self.decisions: dict[str, ApprovalDecision] = {}

    def requires_approval(self, action: ActionKind) -> bool:
        return action in self._required

    def request(
        self,
        action: ActionKind,
        *,
        project_id: str,
        summary: str,
        options: list[dict] | None = None,
        risks: list[str] | None = None,
        citations: list[dict] | None = None,
    ) -> ApprovalRequest:
        request = ApprovalRequest(
            request_id=f"apr_{uuid.uuid4().hex[:12]}",
            action=action,
            project_id=project_id,
            summary=summary,
            options=list(options or []),
            risks=list(risks or []),
            citations=list(citations or []),
        )
        self.pending[request.request_id] = request
        return request

    def submit(self, decision: ApprovalDecision) -> ApprovalRequest:
        """Record a decision. Raises :class:`KeyError` for an unknown request."""
        request = self.pending.pop(decision.request_id)
        self.decisions[decision.request_id] = decision
        return request

    def state_of(self, request_id: str) -> ApprovalState:
        if request_id in self.decisions:
            return self.decisions[request_id].state
        if request_id in self.pending:
            return ApprovalState.PENDING
        return ApprovalState.NOT_REQUIRED
