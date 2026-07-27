"""Gemini / Agent Builder multi-agent platform layer for film and media.

Reference implementation of the platform contour described in
``docs/gemini-platform/``: specialised agents over shared project state, a
governed partner-MCP client, signed event ingress, contour-aware retrieval, and
human-in-the-loop gates on everything with production consequences.

The module has no third-party dependencies and no network calls of its own —
transports, models and retrieval are injected, so the same graph runs against
Google Cloud in production and against fakes in tests.
"""

from __future__ import annotations

from pinocut.agentplatform.agents import (
    Agent,
    AgentDeps,
    AgentResult,
    AgentRole,
    AssetLibrarianAgent,
    CollaborationAgent,
    CoordinatorAgent,
    FanExperienceAgent,
    LanguageModel,
    ModelResponse,
    QualityGovernanceAgent,
    SceneBreakdownAgent,
    ScheduleApprovalAgent,
    SchedulePlannerAgent,
    ScriptAnalystAgent,
    StubLanguageModel,
)
from pinocut.agentplatform.classification import (
    AccessPolicy,
    AccessViolation,
    Contour,
    DataClass,
    Redactor,
)
from pinocut.agentplatform.events import (
    EventEnvelope,
    EventIngress,
    IngressStatus,
    InMemoryDedupeStore,
    sign_body,
    verify_signature,
)
from pinocut.agentplatform.hitl import (
    ActionKind,
    ApprovalDecision,
    ApprovalGate,
    ApprovalRequest,
    ApprovalState,
)
from pinocut.agentplatform.mcp import (
    HttpTransport,
    McpError,
    McpErrorCategory,
    PartnerMcpClient,
    RetryPolicy,
)
from pinocut.agentplatform.models import (
    BudgetGuard,
    BudgetState,
    ModelRouter,
    ModelTier,
    TaskKind,
    UsageLedger,
)
from pinocut.agentplatform.orchestrator import (
    Orchestrator,
    RunStatus,
    WorkflowNode,
    WorkflowRun,
    build_fan_answer_workflow,
    build_shoot_day_plan_workflow,
)
from pinocut.agentplatform.retrieval import (
    Citation,
    Document,
    GroundedContext,
    InMemoryRetrievalService,
    RetrievalService,
)
from pinocut.agentplatform.screenplay import Scene, continuity_flags, parse_scenes
from pinocut.agentplatform.state import ProjectContext, QaReport, WorkflowState

__all__ = [
    "AccessPolicy",
    "AccessViolation",
    "ActionKind",
    "Agent",
    "AgentDeps",
    "AgentResult",
    "AgentRole",
    "ApprovalDecision",
    "ApprovalGate",
    "ApprovalRequest",
    "ApprovalState",
    "AssetLibrarianAgent",
    "BudgetGuard",
    "BudgetState",
    "Citation",
    "CollaborationAgent",
    "Contour",
    "CoordinatorAgent",
    "DataClass",
    "Document",
    "EventEnvelope",
    "EventIngress",
    "FanExperienceAgent",
    "GroundedContext",
    "HttpTransport",
    "InMemoryDedupeStore",
    "InMemoryRetrievalService",
    "IngressStatus",
    "LanguageModel",
    "McpError",
    "McpErrorCategory",
    "ModelResponse",
    "ModelRouter",
    "ModelTier",
    "Orchestrator",
    "PartnerMcpClient",
    "ProjectContext",
    "QaReport",
    "QualityGovernanceAgent",
    "Redactor",
    "RetrievalService",
    "RetryPolicy",
    "RunStatus",
    "Scene",
    "SceneBreakdownAgent",
    "ScheduleApprovalAgent",
    "SchedulePlannerAgent",
    "ScriptAnalystAgent",
    "StubLanguageModel",
    "TaskKind",
    "UsageLedger",
    "WorkflowNode",
    "WorkflowRun",
    "WorkflowState",
    "build_fan_answer_workflow",
    "build_shoot_day_plan_workflow",
    "continuity_flags",
    "parse_scenes",
    "sign_body",
    "verify_signature",
]
