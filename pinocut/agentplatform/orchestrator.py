"""Graph orchestration with audit trail and resumable approval gates.

A run is a sequence of nodes over shared state, not a chain of prompts. Three
things stop a run: a blocking governance verdict, an approval gate, or a node
raising. All three leave the run resumable and fully described in the audit
trail, which is what makes the platform reviewable after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from pinocut.agentplatform.agents import (
    Agent,
    AgentDeps,
    AssetLibrarianAgent,
    CollaborationAgent,
    FanExperienceAgent,
    QualityGovernanceAgent,
    SceneBreakdownAgent,
    ScheduleApprovalAgent,
    SchedulePlannerAgent,
    ScriptAnalystAgent,
)
from pinocut.agentplatform.classification import Contour
from pinocut.agentplatform.hitl import ApprovalDecision, ApprovalState
from pinocut.agentplatform.state import WorkflowState
from pinocut.utils.logging import StageLogger


class RunStatus(str, Enum):
    COMPLETED = "completed"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"  # governance refused to proceed
    FAILED = "failed"  # a node raised
    REJECTED = "rejected"  # a human declined


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One executed node. Shipped to Cloud Logging in production."""

    node: str
    role: str
    ok: bool
    warnings: tuple[str, ...] = ()
    error: str | None = None
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "node": self.node,
            "role": self.role,
            "ok": self.ok,
            "warnings": list(self.warnings),
            "error": self.error,
            "at": self.at,
        }


@dataclass
class WorkflowNode:
    """A node and whether reaching it suspends the run for a human."""

    name: str
    agent: Agent
    halts_for_approval: bool = False


@dataclass
class WorkflowRun:
    """Result of a run. Carries enough state to resume after an approval."""

    workflow: str
    state: WorkflowState
    status: RunStatus
    audit: list[AuditEntry] = field(default_factory=list)
    next_index: int = 0
    reason: str | None = None

    @property
    def is_final(self) -> bool:
        return self.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.REJECTED)

    def to_dict(self) -> dict:
        return {
            "workflow": self.workflow,
            "status": self.status.value,
            "reason": self.reason,
            "audit": [entry.to_dict() for entry in self.audit],
            "state": self.state.to_dict(),
        }


class Orchestrator:
    """Runs a node list over one :class:`WorkflowState`."""

    def __init__(
        self,
        nodes: list[WorkflowNode],
        deps: AgentDeps,
        *,
        workflow: str = "workflow",
        required_contour: Contour | None = None,
        structured_logs: bool = False,
    ):
        self.nodes = list(nodes)
        self.deps = deps
        self.workflow = workflow
        self.required_contour = required_contour
        self.log = StageLogger("orchestrator", structured=structured_logs)

    def run(self, state: WorkflowState) -> WorkflowRun:
        run = WorkflowRun(workflow=self.workflow, state=state, status=RunStatus.COMPLETED)

        # Contour is checked before the first node: a fan session must not be able
        # to start an internal workflow and produce a half-grounded plan.
        if self.required_contour is not None and state.context.contour is not self.required_contour:
            run.status = RunStatus.BLOCKED
            run.reason = (
                f"contour {state.context.contour.value} may not run workflow {self.workflow}"
            )
            run.audit.append(
                AuditEntry(node="contour_check", role="policy", ok=False, error=run.reason)
            )
            return run

        return self._execute(run, start=0)

    def resume(self, run: WorkflowRun, decision: ApprovalDecision) -> WorkflowRun:
        """Continue a suspended run with a human decision.

        Rejection is terminal: the platform does not retry a plan a producer
        turned down, it waits for a new request.
        """
        if run.status is not RunStatus.AWAITING_APPROVAL:
            raise ValueError(f"run is {run.status.value}, not awaiting approval")

        request = self.deps.gate.submit(decision)
        run.audit.append(
            AuditEntry(
                node="approval",
                role="human",
                ok=decision.approved,
                warnings=() if decision.approved else ("approval rejected",),
            )
        )
        if not decision.approved:
            run.status = RunStatus.REJECTED
            run.reason = decision.note or "rejected by approver"
            return run

        options = run.state.ranked_options or run.state.schedule_options
        selected = decision.selected_option_id or (options[0]["optionId"] if options else None)
        if selected is not None and all(option["optionId"] != selected for option in options):
            raise ValueError(f"unknown option {selected} for request {request.request_id}")
        run.state.approved_option_id = selected
        run.status = RunStatus.COMPLETED
        return self._execute(run, start=run.next_index)

    def _execute(self, run: WorkflowRun, *, start: int) -> WorkflowRun:
        for index in range(start, len(self.nodes)):
            node = self.nodes[index]
            try:
                result = node.agent.run(run.state, self.deps)
            except Exception as exc:  # a node failure must not lose the audit trail
                self.log.exception(f"node {node.name} failed: {exc}")
                run.audit.append(
                    AuditEntry(node=node.name, role=node.agent.role.value, ok=False, error=str(exc))
                )
                run.status = RunStatus.FAILED
                run.reason = f"{node.name}: {exc}"
                run.next_index = index
                return run

            run.audit.append(
                AuditEntry(
                    node=node.name,
                    role=result.role.value,
                    ok=True,
                    warnings=result.warnings,
                )
            )
            for warning in result.warnings:
                run.state.add_warning(warning)

            if run.state.qa_report is not None and run.state.qa_report.blocking:
                run.status = RunStatus.BLOCKED
                run.reason = "governance blocked the run"
                run.next_index = index + 1
                return run

            if node.halts_for_approval:
                request = run.state.approval_request
                already_decided = (
                    request is not None
                    and self.deps.gate.state_of(request.request_id) is ApprovalState.APPROVED
                )
                if not already_decided:
                    run.status = RunStatus.AWAITING_APPROVAL
                    run.reason = "human approval required"
                    run.next_index = index + 1
                    return run

        run.next_index = len(self.nodes)
        run.status = RunStatus.COMPLETED
        return run


def build_shoot_day_plan_workflow(
    deps: AgentDeps, *, structured_logs: bool = False
) -> Orchestrator:
    """Script -> breakdown -> partner scheduler -> governance -> human approval -> tasks."""
    return Orchestrator(
        [
            WorkflowNode("script_analysis", ScriptAnalystAgent(structured_logs=structured_logs)),
            WorkflowNode("scene_breakdown", SceneBreakdownAgent(structured_logs=structured_logs)),
            WorkflowNode("schedule_plan", SchedulePlannerAgent(structured_logs=structured_logs)),
            WorkflowNode("asset_lookup", AssetLibrarianAgent(structured_logs=structured_logs)),
            WorkflowNode("governance", QualityGovernanceAgent(structured_logs=structured_logs)),
            WorkflowNode(
                "approval",
                ScheduleApprovalAgent(structured_logs=structured_logs),
                halts_for_approval=True,
            ),
            WorkflowNode("collaboration", CollaborationAgent(structured_logs=structured_logs)),
        ],
        deps,
        workflow="shoot-day-plan",
        required_contour=Contour.INTERNAL,
        structured_logs=structured_logs,
    )


def build_fan_answer_workflow(deps: AgentDeps, *, structured_logs: bool = False) -> Orchestrator:
    """Public corpus only, then the same governance check as internal runs."""
    return Orchestrator(
        [
            WorkflowNode("fan_answer", FanExperienceAgent(structured_logs=structured_logs)),
            WorkflowNode("governance", QualityGovernanceAgent(structured_logs=structured_logs)),
        ],
        deps,
        workflow="fan-answer",
        structured_logs=structured_logs,
    )
