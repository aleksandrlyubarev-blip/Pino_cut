"""Specialised agents of the media production platform.

Each agent owns one responsibility and a model tier, exactly as in the role
table of the specification. Two design rules run through all of them:

1. **Facts are computed, opinions are generated.** Scene numbers, cast lists,
   conflict detection and option ranking are deterministic code. The language
   model is asked for summaries and rationale on top of those facts, and its
   output is validated before it enters state.
2. **No agent decides for a human.** Anything with production consequences ends
   as an :class:`~pinocut.agentplatform.hitl.ApprovalRequest`, never as an
   executed action.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from pinocut.agentplatform.classification import AccessPolicy, Contour, Redactor
from pinocut.agentplatform.hitl import ActionKind, ApprovalGate
from pinocut.agentplatform.mcp import McpError, PartnerMcpClient
from pinocut.agentplatform.models import ModelRouter, ModelTier, TaskKind
from pinocut.agentplatform.retrieval import RetrievalService
from pinocut.agentplatform.screenplay import continuity_flags, parse_scenes
from pinocut.agentplatform.state import QaReport, WorkflowState
from pinocut.utils.logging import StageLogger


class AgentRole(str, Enum):
    COORDINATOR = "coordinator"
    SCRIPT_ANALYST = "script_analyst"
    SCENE_BREAKDOWN = "scene_breakdown"
    SCHEDULE_PLANNER = "schedule_planner"
    ASSET_LIBRARIAN = "asset_librarian"
    COLLABORATION = "collaboration"
    FAN_EXPERIENCE = "fan_experience"
    QUALITY_GOVERNANCE = "quality_governance"


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What a model call returns, with token counts for the cost ledger."""

    data: dict
    input_tokens: int = 0
    output_tokens: int = 0


class LanguageModel(Protocol):
    """Minimal generation interface. Implement over Interactions API / ADK."""

    def generate(
        self, *, node: str, prompt: str, tier: ModelTier, schema: dict | None = None
    ) -> ModelResponse: ...


class StubLanguageModel:
    """Offline stand-in returning canned payloads per node.

    This is a test and demo double, not a model: it exists so the graph, gates
    and audit trail can be exercised without network access or credentials.
    Anything it cannot answer comes back empty, and agents must degrade to their
    deterministic path rather than invent content.
    """

    def __init__(self, responses: dict[str, dict] | None = None):
        self.responses = dict(responses or {})
        self.calls: list[tuple[str, ModelTier]] = []

    def generate(
        self, *, node: str, prompt: str, tier: ModelTier, schema: dict | None = None
    ) -> ModelResponse:
        self.calls.append((node, tier))
        data = self.responses.get(node, {})
        return ModelResponse(
            data=dict(data),
            input_tokens=max(1, len(prompt) // 4),
            output_tokens=max(1, len(json.dumps(data)) // 4),
        )


@dataclass
class AgentDeps:
    """Everything an agent may reach. Anything absent must degrade gracefully."""

    retrieval: RetrievalService
    router: ModelRouter = field(default_factory=ModelRouter)
    llm: LanguageModel | None = None
    mcp: PartnerMcpClient | None = None
    gate: ApprovalGate = field(default_factory=ApprovalGate)
    #: Used by governance to re-check citations independently of retrieval.
    policy: AccessPolicy = field(default_factory=AccessPolicy)


@dataclass(frozen=True, slots=True)
class AgentResult:
    """Outcome of one node in the graph."""

    role: AgentRole
    output: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class Agent(ABC):
    """Base class: a role, a model tier, and a single ``run`` step."""

    role: AgentRole
    task_kind: TaskKind = TaskKind.DRAFTING

    def __init__(self, *, structured_logs: bool = False):
        self.log = StageLogger(f"agent.{self.role.value}", structured=structured_logs)

    @abstractmethod
    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult: ...

    def _generate(
        self,
        deps: AgentDeps,
        state: WorkflowState,
        *,
        node: str,
        prompt: str,
        schema: dict | None = None,
    ) -> dict:
        """Call the model if one is wired, charging the run's usage ledger."""
        if deps.llm is None:
            return {}
        tier = deps.router.tier_for(self.task_kind)
        response = deps.llm.generate(node=node, prompt=prompt, tier=tier, schema=schema)
        entry = state.usage.record(node, tier, response.input_tokens, response.output_tokens)
        if deps.router.budget is not None:
            deps.router.budget.charge(entry.cost_usd)
        return response.data


class CoordinatorAgent(Agent):
    """Routes an incoming request to a workflow. Cheapest tier by design."""

    role = AgentRole.COORDINATOR
    task_kind = TaskKind.ROUTING

    #: Keyword fallback so routing works with no model available.
    INTENTS: dict[str, tuple[str, ...]] = {
        "shoot-day-plan": ("план", "расписан", "съём", "schedule", "call sheet", "shoot"),
        "script-analysis": ("сценар", "script", "beat", "continuity"),
        "asset-lookup": ("ассет", "версия", "dailies", "asset", "version", "footage"),
        "fan-answer": ("фанат", "spoiler", "fan", "faq"),
    }

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        goal = state.goal.lower()
        workflow = self._keyword_intent(goal)
        model_intent = self._generate(
            deps,
            state,
            node="coordinator.route",
            prompt=f"Classify the production request into a workflow id.\nRequest: {state.goal}",
            schema={"type": "object", "properties": {"workflow": {"type": "string"}}},
        ).get("workflow")
        if model_intent in self.INTENTS:
            workflow = model_intent

        if state.context.contour is Contour.FAN and workflow != "fan-answer":
            # A fan-contour session may not enter internal workflows regardless
            # of what the request says.
            return AgentResult(
                self.role,
                {"workflow": "fan-answer", "requested": workflow},
                ("fan contour forced to fan-answer workflow",),
            )
        return AgentResult(self.role, {"workflow": workflow})

    def _keyword_intent(self, goal: str) -> str:
        for workflow, keywords in self.INTENTS.items():
            if any(keyword in goal for keyword in keywords):
                return workflow
        return "script-analysis"


class ScriptAnalystAgent(Agent):
    """Grounds the run: retrieves the script and parses its structure."""

    role = AgentRole.SCRIPT_ANALYST
    task_kind = TaskKind.DEEP_REASONING

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        query = (
            f"script version {state.context.script_version or 'latest'} "
            f"scenes locations cast call sheet {state.goal}"
        )
        context = deps.retrieval.retrieve(query, contour=state.context.contour, top_k=6)
        state.add_citations(context.citations)
        state.add_denied_reads(context.denied)

        warnings: list[str] = []
        if not context.is_grounded:
            warnings.append("no documents retrieved — analysis is not grounded")
            return AgentResult(self.role, {"scenes": []}, tuple(warnings))

        scenes = parse_scenes("\n".join(doc.text for doc in context.documents))
        flags = continuity_flags(scenes)
        if not scenes:
            warnings.append("no sluglines found in retrieved documents")

        summary = self._generate(
            deps,
            state,
            node="script_analyst.summary",
            prompt=(
                "Summarise narrative risks for the parsed scenes. "
                "Use only the provided context.\n\n"
                f"{context.as_prompt_context()}"
            ),
            schema={"type": "object", "properties": {"notes": {"type": "array"}}},
        )

        output = {
            "scenes": [scene.to_dict() for scene in scenes],
            "continuityFlags": flags,
            "notes": summary.get("notes", []),
        }
        state.scene_breakdown = output
        return AgentResult(self.role, output, tuple(warnings))


class SceneBreakdownAgent(Agent):
    """Turns parsed scenes into department-level requirements."""

    role = AgentRole.SCENE_BREAKDOWN
    task_kind = TaskKind.DEEP_REASONING

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        breakdown = state.scene_breakdown or {}
        scenes = breakdown.get("scenes", [])
        if not scenes:
            return AgentResult(self.role, {}, ("no scenes to break down",))

        cast: dict[str, list[int]] = {}
        locations: dict[str, list[int]] = {}
        departments: dict[str, list[int]] = {}
        for scene in scenes:
            for member in scene.get("cast", []):
                cast.setdefault(member, []).append(scene["sceneNo"])
            locations.setdefault(scene.get("location", "UNKNOWN"), []).append(scene["sceneNo"])
            for department in scene.get("departments", []):
                departments.setdefault(department, []).append(scene["sceneNo"])

        output = {
            "castByScene": cast,
            "locationsByScene": locations,
            "departmentsByScene": departments,
            "weatherSensitiveScenes": [
                scene["sceneNo"] for scene in scenes if scene.get("weatherSensitive")
            ],
        }
        breakdown["requirements"] = output
        state.scene_breakdown = breakdown
        return AgentResult(self.role, output)


class SchedulePlannerAgent(Agent):
    """Calls the partner scheduler, then ranks the returned options locally."""

    role = AgentRole.SCHEDULE_PLANNER
    task_kind = TaskKind.DEEP_REASONING

    def __init__(self, *, tool_name: str = "schedule.optimize", structured_logs: bool = False):
        super().__init__(structured_logs=structured_logs)
        self.tool_name = tool_name

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        breakdown = state.scene_breakdown or {}
        warnings: list[str] = []
        options: list[dict] = []

        if deps.mcp is not None:
            key = self.idempotency_key(state)
            try:
                deps.mcp.ensure_initialized()
                result = deps.mcp.call_tool(
                    self.tool_name,
                    {
                        "projectId": state.context.project_id,
                        "scriptVersion": state.context.script_version,
                        "sceneBreakdown": breakdown,
                        "goal": state.goal,
                    },
                    idempotency_key=key,
                )
                options = self._validate_options(result.get("options", []))
                if not options:
                    warnings.append("partner scheduler returned no usable options")
            except McpError as exc:
                warnings.append(f"partner scheduler unavailable ({exc}); using local heuristic")
                self.log.warn(f"schedule.optimize failed: {exc}")
        else:
            warnings.append("no partner MCP configured; using local heuristic")

        if not options:
            options = self._local_options(breakdown)

        state.schedule_options = options
        state.ranked_options = self.rank(options)
        return AgentResult(self.role, {"options": state.ranked_options}, tuple(warnings))

    @staticmethod
    def idempotency_key(state: WorkflowState) -> str:
        """Stable across retries, processes and workers — never time-based.

        ``hash()`` is salted per interpreter, so a redelivery handled by another
        worker would produce a different key and a duplicate schedule.
        """
        goal_digest = hashlib.sha256(state.goal.encode("utf-8")).hexdigest()[:12]
        return (
            f"shootplan-{state.context.project_id}-"
            f"{state.context.script_version or 'na'}-{goal_digest}"
        )

    @staticmethod
    def _validate_options(raw: object) -> list[dict]:
        if not isinstance(raw, list):
            return []
        options: list[dict] = []
        for item in raw:
            if not isinstance(item, dict) or "optionId" not in item:
                continue
            options.append(
                {
                    "optionId": str(item["optionId"]),
                    "score": float(item.get("score", 0.0)),
                    "risks": [str(risk) for risk in item.get("risks", [])],
                    "days": item.get("days", []),
                }
            )
        return options

    @staticmethod
    def _local_options(breakdown: dict) -> list[dict]:
        """Fallback so the crew still gets something usable when the partner is down."""
        requirements = breakdown.get("requirements", {})
        weather_scenes = requirements.get("weatherSensitiveScenes", [])
        locations = requirements.get("locationsByScene", {})
        return [
            {
                "optionId": "local-A",
                "score": 0.6,
                "strategy": "group by location",
                "risks": ["unverified against cast availability"],
                "days": [
                    {"location": location, "scenes": scenes}
                    for location, scenes in sorted(locations.items())
                ],
            },
            {
                "optionId": "local-B",
                "score": 0.55,
                "strategy": "weather-sensitive scenes first",
                "risks": ["unverified against cast availability", "front-loads exterior work"],
                "days": [{"location": "weather-first", "scenes": weather_scenes}],
            },
        ]

    @staticmethod
    def rank(options: list[dict]) -> list[dict]:
        """Higher score wins; ties break toward fewer risks, then option id."""
        return sorted(
            options,
            key=lambda option: (
                -option.get("score", 0.0),
                len(option.get("risks", [])),
                option["optionId"],
            ),
        )


class AssetLibrarianAgent(Agent):
    """Resolves dailies, versions and review packets for the current scenes."""

    role = AgentRole.ASSET_LIBRARIAN
    task_kind = TaskKind.RETRIEVAL_QA

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        scenes = (state.scene_breakdown or {}).get("scenes", [])
        scene_numbers = [scene["sceneNo"] for scene in scenes]
        query = f"dailies versions review comments scenes {scene_numbers} {state.goal}"
        context = deps.retrieval.retrieve(query, contour=state.context.contour, top_k=5)
        state.add_citations(context.citations)
        state.add_denied_reads(context.denied)

        assets = [
            {"docId": doc.doc_id, "source": doc.source, "dataClass": doc.data_class.value}
            for doc in context.documents
        ]
        state.assets = assets
        warnings = () if assets else ("no assets matched the current scenes",)
        return AgentResult(self.role, {"assets": assets}, warnings)


class CollaborationAgent(Agent):
    """Drafts crew-facing tasks and notifications. Drafts only — never sends."""

    role = AgentRole.COLLABORATION
    task_kind = TaskKind.DRAFTING

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        option = state.selected_option()
        if option is None:
            return AgentResult(self.role, {"tasks": []}, ("no approved option — nothing to draft",))
        tasks = [
            {
                "title": f"Prepare {day.get('location', 'day')} — scenes {day.get('scenes', [])}",
                "projectId": state.context.project_id,
                "optionId": option["optionId"],
                "status": "draft",
            }
            for day in option.get("days", [])
        ]
        state.artifacts["tasks_draft"] = json.dumps(tasks, ensure_ascii=False)
        return AgentResult(self.role, {"tasks": tasks})


class FanExperienceAgent(Agent):
    """Fan-facing answers. Public corpus only, redacted, refuses when ungrounded."""

    role = AgentRole.FAN_EXPERIENCE
    task_kind = TaskKind.RETRIEVAL_QA

    REFUSAL = (
        "I can only answer from published material, and I could not find anything "
        "published that covers this."
    )

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        # Force the fan contour regardless of the caller's claim.
        context = deps.retrieval.retrieve(state.goal, contour=Contour.FAN, top_k=4)
        state.add_denied_reads(context.denied)

        if not context.is_grounded:
            return AgentResult(
                self.role,
                {"answer": self.REFUSAL, "grounded": False, "citations": []},
                ("refused: no public source",),
            )

        state.add_citations(context.citations)
        redactor = Redactor()
        generated = self._generate(
            deps,
            state,
            node="fan_experience.answer",
            prompt=(
                "Answer the fan question using only this published material. "
                "If it is not covered, say so.\n\n"
                f"Question: {state.goal}\n\n{context.as_prompt_context(2000)}"
            ),
            schema={"type": "object", "properties": {"answer": {"type": "string"}}},
        )
        answer = generated.get("answer") or context.documents[0].text[:400]
        return AgentResult(
            self.role,
            {
                "answer": redactor.apply(answer),
                "grounded": True,
                "citations": [citation.to_dict() for citation in context.citations],
                "redactions": len(redactor.hits),
            },
        )


class QualityGovernanceAgent(Agent):
    """The gate before any human sees a recommendation.

    Independently re-verifies every citation against the caller's contour rather
    than trusting the retrieval filter that produced it. A leak — above-ceiling
    data that reached the output — is always blocking. Documents the filter
    correctly dropped are reported, not blocking: denying a fan a script is the
    system working.
    """

    role = AgentRole.QUALITY_GOVERNANCE
    task_kind = TaskKind.QUALITY_REVIEW

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        leaks = [
            violation
            for violation in (
                deps.policy.check(state.context.contour, citation.data_class, citation.doc_id)
                for citation in state.citations
            )
            if violation is not None
        ]
        for leak in leaks:
            if leak not in state.violations:
                state.violations.append(leak)

        report = QaReport(grounded=bool(state.citations), access_ok=not state.violations)

        for violation in state.violations:
            report.add_issue("critical", str(violation), kind="access")
        if state.denied_reads:
            report.add_issue(
                "low",
                f"{len(state.denied_reads)} document(s) withheld by contour policy",
                kind="access_filtered",
            )
        if not report.grounded:
            report.add_issue("high", "answer is not grounded in any retrieved source")

        options = state.ranked_options or state.schedule_options
        if state.scene_breakdown is not None and not options:
            report.add_issue("high", "no schedule options were produced")
        for option in options:
            for risk in option.get("risks", []):
                report.add_issue("medium", f"option {option['optionId']}: {risk}", kind="risk")

        for flag in (state.scene_breakdown or {}).get("continuityFlags", []):
            report.add_issue(flag.get("severity", "low"), flag["message"], kind="continuity")

        state.qa_report = report
        warnings = ("blocking issues found",) if report.blocking else ()
        return AgentResult(self.role, report.to_dict(), warnings)


class ScheduleApprovalAgent(Agent):
    """Builds the approval request. Deliberately not a decision maker."""

    role = AgentRole.QUALITY_GOVERNANCE
    task_kind = TaskKind.ROUTING

    action = ActionKind.SCHEDULE_COMMIT

    def run(self, state: WorkflowState, deps: AgentDeps) -> AgentResult:
        options = state.ranked_options or state.schedule_options
        risks = sorted(
            {risk for option in options for risk in option.get("risks", [])}
        )
        request = deps.gate.request(
            self.action,
            project_id=state.context.project_id,
            summary=f"Shoot plan for {state.context.project_id} ({len(options)} options)",
            options=options,
            risks=risks,
            citations=[citation.to_dict() for citation in state.citations],
        )
        state.approval_request = request
        return AgentResult(self.role, {"approvalRequest": request.to_dict()})
