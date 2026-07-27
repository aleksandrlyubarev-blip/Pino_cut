"""Workflow state carried across agents in a run.

Kept as plain dataclasses so the state is serialisable into a session store
(Firestore for the MVP, Spanner when consistency and scale demand it) and
inspectable in the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from pinocut.agentplatform.classification import AccessViolation, Contour
from pinocut.agentplatform.hitl import ApprovalRequest
from pinocut.agentplatform.models import UsageLedger
from pinocut.agentplatform.retrieval import Citation


@dataclass(frozen=True, slots=True)
class ProjectContext:
    """Who is asking, about which project, in which contour."""

    project_id: str
    user_id: str
    contour: Contour = Contour.INTERNAL
    tenant_id: str = "default"
    script_version: str | None = None

    def to_dict(self) -> dict:
        return {
            "projectId": self.project_id,
            "userId": self.user_id,
            "contour": self.contour.value,
            "tenantId": self.tenant_id,
            "scriptVersion": self.script_version,
        }


@dataclass
class QaReport:
    """Governance verdict. ``blocking`` decides whether the run may continue."""

    grounded: bool = False
    access_ok: bool = True
    issues: list[dict] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return not self.access_ok or any(
            issue.get("severity") == "critical" for issue in self.issues
        )

    def add_issue(self, severity: str, message: str, **extra: object) -> None:
        self.issues.append({"severity": severity, "message": message, **extra})

    def to_dict(self) -> dict:
        return {
            "grounded": self.grounded,
            "accessOk": self.access_ok,
            "blocking": self.blocking,
            "issues": list(self.issues),
        }


@dataclass
class WorkflowState:
    """Mutable state threaded through the agent graph."""

    context: ProjectContext
    goal: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    citations: list[Citation] = field(default_factory=list)
    #: Documents the access filter dropped. Expected traffic — reported, not fatal.
    denied_reads: list[AccessViolation] = field(default_factory=list)
    #: Above-ceiling data that reached the output. An incident — always blocking.
    violations: list[AccessViolation] = field(default_factory=list)
    scene_breakdown: dict | None = None
    schedule_options: list[dict] = field(default_factory=list)
    ranked_options: list[dict] = field(default_factory=list)
    assets: list[dict] = field(default_factory=list)
    qa_report: QaReport | None = None
    approval_request: ApprovalRequest | None = None
    approved_option_id: str | None = None
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    usage: UsageLedger = field(default_factory=UsageLedger)

    def add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def add_citations(self, citations: list[Citation]) -> None:
        """Append, keeping one citation per document across all agents."""
        seen = {citation.doc_id for citation in self.citations}
        for citation in citations:
            if citation.doc_id not in seen:
                self.citations.append(citation)
                seen.add(citation.doc_id)

    def add_denied_reads(self, denied: list[AccessViolation]) -> None:
        for violation in denied:
            if violation not in self.denied_reads:
                self.denied_reads.append(violation)

    def selected_option(self) -> dict | None:
        for option in self.ranked_options or self.schedule_options:
            if option.get("optionId") == self.approved_option_id:
                return option
        return None

    def to_dict(self) -> dict:
        return {
            "context": self.context.to_dict(),
            "goal": self.goal,
            "startedAt": self.started_at,
            "citations": [citation.to_dict() for citation in self.citations],
            "deniedReads": [str(denied) for denied in self.denied_reads],
            "violations": [str(violation) for violation in self.violations],
            "sceneBreakdown": self.scene_breakdown,
            "scheduleOptions": list(self.ranked_options or self.schedule_options),
            "approvedOptionId": self.approved_option_id,
            "qaReport": self.qa_report.to_dict() if self.qa_report else None,
            "approvalRequest": (
                self.approval_request.to_dict() if self.approval_request else None
            ),
            "artifacts": dict(self.artifacts),
            "warnings": list(self.warnings),
            "usage": self.usage.summary(),
        }
