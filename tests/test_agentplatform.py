"""Tests for the Gemini/Agent Builder platform layer."""

from __future__ import annotations

import json

import pytest

from pinocut.agentplatform import (
    AccessPolicy,
    ActionKind,
    AgentDeps,
    ApprovalDecision,
    ApprovalGate,
    BudgetGuard,
    BudgetState,
    Contour,
    CoordinatorAgent,
    DataClass,
    Document,
    EventIngress,
    FanExperienceAgent,
    IngressStatus,
    InMemoryRetrievalService,
    McpError,
    McpErrorCategory,
    ModelRouter,
    ModelTier,
    PartnerMcpClient,
    ProjectContext,
    Redactor,
    RetryPolicy,
    RunStatus,
    StubLanguageModel,
    TaskKind,
    UsageLedger,
    WorkflowState,
    build_fan_answer_workflow,
    build_shoot_day_plan_workflow,
    continuity_flags,
    parse_scenes,
    sign_body,
)

SCRIPT = """
INT. STUDIO A - DAY

MAYA enters, carrying the storyboard.

MAYA
We shoot the rig sequence today.

EXT. BACKLOT - NIGHT

Rain machine floods the street. A car rig waits.

DANIEL
The chase starts here.

CUT TO:

INT. STUDIO A - NIGHT

The crew resets for the last take.
"""


def make_corpus() -> list[Document]:
    return [
        Document(
            doc_id="script_v17",
            source="script_v17.pdf",
            data_class=DataClass.PRIVATE_DEVELOPMENT,
            text="Script v17 — scenes, locations and cast.\n" + SCRIPT,
        ),
        Document(
            doc_id="callsheet_0910",
            source="callsheet_2026-09-10.pdf",
            data_class=DataClass.RESTRICTED_PRODUCTION,
            text="Call sheet: scenes 12-18, Studio A and Backlot, cast MAYA and DANIEL.",
        ),
        Document(
            doc_id="press_kit",
            source="press_kit.md",
            data_class=DataClass.PUBLIC_FAN_SAFE,
            text=(
                "The film shoots on a practical backlot. Contact press@example.com "
                "for interview requests about the scenes and cast."
            ),
        ),
    ]


def make_deps(**overrides) -> AgentDeps:
    defaults = {
        "retrieval": InMemoryRetrievalService(make_corpus()),
        "router": ModelRouter(),
        "llm": None,
        "mcp": None,
        "gate": ApprovalGate(),
    }
    defaults.update(overrides)
    return AgentDeps(**defaults)


def internal_state(goal: str = "собери план съёмочного дня по сценам 12-18") -> WorkflowState:
    return WorkflowState(
        context=ProjectContext(
            project_id="film-123",
            user_id="u_7781",
            contour=Contour.INTERNAL,
            script_version="v17",
        ),
        goal=goal,
    )


# --- screenplay parsing -------------------------------------------------------


def test_parse_scenes_extracts_headings_cast_and_departments():
    scenes = parse_scenes(SCRIPT)

    assert [scene.scene_no for scene in scenes] == [1, 2, 3]
    assert scenes[0].location == "STUDIO A"
    assert scenes[0].time_of_day == "DAY"
    assert scenes[0].interior and not scenes[0].exterior
    assert scenes[0].cast == ["MAYA"]

    backlot = scenes[1]
    assert backlot.exterior and backlot.is_night
    assert "sfx" in backlot.departments  # rain machine
    assert "picture_vehicles" in backlot.departments  # car rig


def test_continuity_flags_report_night_exteriors_and_split_locations():
    flags = continuity_flags(parse_scenes(SCRIPT))
    kinds = {flag["flag"] for flag in flags}

    assert "night_exterior" in kinds
    assert "split_day_night_location" in kinds  # STUDIO A runs DAY and NIGHT


def test_transitions_are_not_parsed_as_scenes():
    assert all("CUT TO" not in scene.heading for scene in parse_scenes(SCRIPT))


# --- access control -----------------------------------------------------------


def test_fan_contour_cannot_read_private_documents():
    policy = AccessPolicy()

    assert policy.can_read(Contour.INTERNAL, DataClass.PRIVATE_DEVELOPMENT)
    assert not policy.can_read(Contour.FAN, DataClass.PRIVATE_DEVELOPMENT)
    assert not policy.can_read(Contour.FAN, DataClass.RESTRICTED_PRODUCTION)
    assert policy.can_read(Contour.FAN, DataClass.PUBLIC_FAN_SAFE)
    assert not policy.can_read(Contour.PARTNER, DataClass.RESTRICTED_PRODUCTION)


def test_retrieval_filters_by_contour_and_reports_what_it_withheld():
    service = InMemoryRetrievalService(make_corpus())

    internal = service.retrieve("scenes cast backlot", contour=Contour.INTERNAL)
    fan = service.retrieve("scenes cast backlot", contour=Contour.FAN)

    assert {doc.doc_id for doc in internal.documents} >= {"script_v17", "callsheet_0910"}
    assert [doc.data_class for doc in fan.documents] == [DataClass.PUBLIC_FAN_SAFE]
    assert fan.denied, "withheld documents must be reported to governance"
    assert all(denied.contour is Contour.FAN for denied in fan.denied)


def test_redactor_strips_contact_details():
    redactor = Redactor(spoiler_terms=("final twist",))
    text = redactor.apply("Write to press@example.com about the final twist.")

    assert "press@example.com" not in text
    assert "[redacted:email]" in text
    assert "[redacted:spoiler]" in text


# --- MCP client ---------------------------------------------------------------


class FakeTransport:
    """Scripted transport: each entry is ``(status, body)`` or an exception."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[tuple[dict, dict]] = []

    def post(self, payload, headers):
        self.requests.append((payload, headers))
        default = (200, {"jsonrpc": "2.0", "result": {}})
        response = self.responses.pop(0) if self.responses else default
        if isinstance(response, Exception):
            raise response
        return response


def make_client(responses, **overrides) -> PartnerMcpClient:
    defaults = {
        "transport": FakeTransport(responses),
        "token_provider": lambda: "test-token",
        "retry": RetryPolicy(max_attempts=3, base_delay_sec=2.0),
        "sleeper": lambda _seconds: None,
    }
    defaults.update(overrides)
    return PartnerMcpClient(**defaults)


def test_initialize_negotiates_protocol_and_sends_bearer():
    client = make_client([(200, {"jsonrpc": "2.0", "id": "1", "result": {"serverInfo": {}}})])

    client.initialize()

    payload, headers = client.transport.requests[0]
    assert payload["method"] == "initialize"
    assert payload["params"]["protocolVersion"]
    assert headers["authorization"] == "Bearer test-token"


def test_call_tool_sends_idempotency_key_in_meta_and_header():
    client = make_client([(200, {"jsonrpc": "2.0", "id": "1", "result": {"options": []}})])

    client.call_tool("schedule.optimize", {"projectId": "film-123"}, idempotency_key="key-1")

    payload, headers = client.transport.requests[0]
    assert payload["params"]["_meta"]["idempotencyKey"] == "key-1"
    assert headers["x-idempotency-key"] == "key-1"


def test_retryable_failure_is_retried_with_the_same_idempotency_key():
    client = make_client(
        [
            (503, {"error": {"code": 503, "message": "unavailable"}}),
            (200, {"jsonrpc": "2.0", "id": "1", "result": {"options": [{"optionId": "A"}]}}),
        ]
    )

    result = client.call_tool("schedule.optimize", {}, idempotency_key="key-1")

    assert result["options"][0]["optionId"] == "A"
    assert len(client.transport.requests) == 2
    keys = {headers["x-idempotency-key"] for _payload, headers in client.transport.requests}
    assert keys == {"key-1"}, "a retry must not create a second partner-side effect"
    assert client.calls[-1].attempts == 2


def test_validation_error_is_not_retried():
    client = make_client([(400, {"error": {"code": 400, "message": "bad arguments"}})])

    with pytest.raises(McpError) as excinfo:
        client.call_tool("schedule.optimize", {})

    assert excinfo.value.category is McpErrorCategory.VALIDATION
    assert not excinfo.value.retryable
    assert len(client.transport.requests) == 1


def test_auth_failure_is_categorised_and_audited():
    client = make_client([(401, {"error": {"code": 401, "message": "expired token"}})])

    with pytest.raises(McpError) as excinfo:
        client.list_tools()

    assert excinfo.value.category is McpErrorCategory.AUTH
    assert client.calls[-1].ok is False
    assert client.calls[-1].error["category"] == "auth"


def test_retries_are_bounded_by_the_policy():
    client = make_client([(500, {"error": {"message": "boom"}})] * 5)

    with pytest.raises(McpError):
        client.list_tools()

    assert len(client.transport.requests) == 3  # max_attempts


def test_retry_backoff_is_exponential_and_capped():
    policy = RetryPolicy(max_attempts=4, base_delay_sec=2.0, max_delay_sec=16.0)

    assert [policy.delay_for(attempt) for attempt in range(1, 6)] == [2.0, 4.0, 8.0, 16.0, 16.0]


def test_jsonrpc_error_body_maps_onto_the_taxonomy():
    error = {"code": -32602, "message": "bad params"}
    client = make_client([(200, {"jsonrpc": "2.0", "id": "1", "error": error})])

    with pytest.raises(McpError) as excinfo:
        client.list_tools()

    assert excinfo.value.category is McpErrorCategory.VALIDATION


# --- event ingress ------------------------------------------------------------

SECRET = "partner-shared-secret"


def event_body(event_id: str = "evt_1", event_type: str = "asset.version.approved") -> bytes:
    return json.dumps(
        {
            "eventId": event_id,
            "eventType": event_type,
            "occurredAt": "2026-09-10T08:12:33Z",
            "tenantId": "studio-a",
            "projectId": "film-123",
            "entity": {"type": "assetVersion", "id": "av_456"},
            "actor": {"type": "user", "id": "u_7781"},
            "payload": {"status": "approved"},
        }
    ).encode("utf-8")


def signed_headers(body: bytes, **extra: str) -> dict[str, str]:
    return {"X-Signature": sign_body(SECRET, body), **extra}


def test_valid_event_is_accepted_and_parsed():
    ingress = EventIngress(SECRET)
    body = event_body()

    result = ingress.handle(body, signed_headers(body))

    assert result.status is IngressStatus.ACCEPTED
    assert result.should_process
    assert result.event.project_id == "film-123"


def test_replayed_event_is_dropped():
    ingress = EventIngress(SECRET)
    body = event_body()

    first = ingress.handle(body, signed_headers(body))
    second = ingress.handle(body, signed_headers(body))

    assert first.status is IngressStatus.ACCEPTED
    assert second.status is IngressStatus.DUPLICATE
    assert not second.should_process


def test_bad_signature_is_rejected_before_parsing():
    ingress = EventIngress(SECRET)
    body = event_body()

    result = ingress.handle(body, {"X-Signature": "sha256=deadbeef"})

    assert result.status is IngressStatus.REJECTED
    assert result.event is None


def test_tampered_body_fails_verification():
    ingress = EventIngress(SECRET)
    body = event_body()
    headers = signed_headers(body)

    result = ingress.handle(body.replace(b"film-123", b"film-999"), headers)

    assert result.status is IngressStatus.REJECTED


def test_missing_required_field_is_rejected():
    ingress = EventIngress(SECRET)
    body = json.dumps({"eventId": "evt_2", "eventType": "schedule.changed"}).encode("utf-8")

    result = ingress.handle(body, signed_headers(body))

    assert result.status is IngressStatus.REJECTED
    assert "missing required fields" in result.reason


def test_header_event_id_must_match_the_body():
    ingress = EventIngress(SECRET)
    body = event_body("evt_3")

    result = ingress.handle(body, signed_headers(body, **{"X-Event-Id": "evt_other"}))

    assert result.status is IngressStatus.REJECTED


def test_unknown_event_type_is_not_routed_to_an_agent():
    ingress = EventIngress(SECRET)
    body = event_body("evt_4", "asset.exploded")

    result = ingress.handle(body, signed_headers(body))

    assert result.status is IngressStatus.UNKNOWN_TYPE
    assert not result.should_process


# --- routing, cost and budget -------------------------------------------------


def test_router_uses_the_specified_tier_per_task():
    router = ModelRouter()

    assert router.tier_for(TaskKind.ROUTING) is ModelTier.FLASH_LITE
    assert router.tier_for(TaskKind.DRAFTING) is ModelTier.FLASH
    assert router.tier_for(TaskKind.DEEP_REASONING) is ModelTier.PRO
    assert router.model_id(TaskKind.DEEP_REASONING) == "gemini-2.5-pro"


def test_router_downgrades_when_the_budget_alerts():
    budget = BudgetGuard(cap_usd=100.0, alert_ratio=0.8)
    router = ModelRouter(budget=budget)
    budget.charge(85.0)

    assert budget.state is BudgetState.ALERT
    assert router.tier_for(TaskKind.DEEP_REASONING) is ModelTier.FLASH
    assert router.tier_for(TaskKind.REALTIME_VOICE) is ModelTier.LIVE  # no cheaper equivalent


def test_budget_alert_fires_before_the_cap():
    budget = BudgetGuard(cap_usd=10.0)

    assert budget.charge(7.0) is BudgetState.OK
    assert budget.charge(1.5) is BudgetState.ALERT
    assert budget.charge(2.0) is BudgetState.EXCEEDED


def test_usage_ledger_prices_calls_and_retrieval():
    ledger = UsageLedger()
    ledger.record("plan", ModelTier.PRO, input_tokens=1_000_000, output_tokens=100_000)
    ledger.record_retrieval("agent_search_enterprise", calls=1000)

    assert ledger.model_cost_usd == pytest.approx(1.25 + 1.0)
    assert ledger.retrieval_cost_usd == pytest.approx(4.0)
    assert ledger.summary()["calls"] == 1


# --- agents and orchestration -------------------------------------------------


def test_coordinator_keeps_a_fan_session_out_of_internal_workflows():
    state = WorkflowState(
        context=ProjectContext("film-123", "fan_9", contour=Contour.FAN),
        goal="покажи расписание съёмок",  # asks for an internal workflow
    )

    result = CoordinatorAgent().run(state, make_deps())

    assert result.output["workflow"] == "fan-answer"
    assert result.warnings


def test_shoot_day_plan_suspends_for_approval_and_publishes_nothing():
    deps = make_deps()
    orchestrator = build_shoot_day_plan_workflow(deps)

    run = orchestrator.run(internal_state())

    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.state.approval_request is not None
    assert run.state.approval_request.action is ActionKind.SCHEDULE_COMMIT
    assert run.state.approved_option_id is None
    assert "tasks_draft" not in run.state.artifacts, "nothing may be drafted before approval"
    assert [entry.node for entry in run.audit][-1] == "approval"


def test_approval_resumes_the_run_and_produces_task_drafts():
    deps = make_deps()
    orchestrator = build_shoot_day_plan_workflow(deps)
    run = orchestrator.run(internal_state())
    option_id = run.state.ranked_options[0]["optionId"]

    resumed = orchestrator.resume(
        run,
        ApprovalDecision(
            request_id=run.state.approval_request.request_id,
            approver_id="producer_1",
            approved=True,
            selected_option_id=option_id,
        ),
    )

    assert resumed.status is RunStatus.COMPLETED
    assert resumed.state.approved_option_id == option_id
    assert json.loads(resumed.state.artifacts["tasks_draft"])
    tasks = json.loads(resumed.state.artifacts["tasks_draft"])
    assert all(task["status"] == "draft" for task in tasks)


def test_rejection_is_terminal():
    deps = make_deps()
    orchestrator = build_shoot_day_plan_workflow(deps)
    run = orchestrator.run(internal_state())

    rejected = orchestrator.resume(
        run,
        ApprovalDecision(
            request_id=run.state.approval_request.request_id,
            approver_id="producer_1",
            approved=False,
            note="location falls through",
        ),
    )

    assert rejected.status is RunStatus.REJECTED
    assert rejected.is_final
    assert "tasks_draft" not in rejected.state.artifacts


def test_resume_rejects_an_option_the_planner_never_offered():
    deps = make_deps()
    orchestrator = build_shoot_day_plan_workflow(deps)
    run = orchestrator.run(internal_state())

    with pytest.raises(ValueError, match="unknown option"):
        orchestrator.resume(
            run,
            ApprovalDecision(
                request_id=run.state.approval_request.request_id,
                approver_id="producer_1",
                approved=True,
                selected_option_id="option-that-does-not-exist",
            ),
        )


def test_planner_uses_the_partner_scheduler_when_available():
    client = make_client(
        [
            (200, {"jsonrpc": "2.0", "id": "1", "result": {}}),  # initialize
            (
                200,
                {
                    "jsonrpc": "2.0",
                    "id": "2",
                    "result": {
                        "options": [
                            {"optionId": "A", "score": 0.81, "risks": ["weather"]},
                            {"optionId": "B", "score": 0.87, "risks": ["crew overtime"]},
                        ]
                    },
                },
            ),
        ]
    )
    orchestrator = build_shoot_day_plan_workflow(make_deps(mcp=client))

    run = orchestrator.run(internal_state())

    assert [option["optionId"] for option in run.state.ranked_options] == ["B", "A"]
    assert client.calls[-1].tool == "schedule.optimize"


def test_planner_degrades_to_a_local_heuristic_when_the_partner_fails():
    client = make_client(
        [(200, {"jsonrpc": "2.0", "id": "1", "result": {}})]
        + [(500, {"error": {"message": "down"}})] * 3
    )
    orchestrator = build_shoot_day_plan_workflow(make_deps(mcp=client))

    run = orchestrator.run(internal_state())

    assert run.state.ranked_options, "the crew still gets options when the partner is down"
    assert all(option["optionId"].startswith("local-") for option in run.state.ranked_options)
    assert any("partner scheduler unavailable" in warning for warning in run.state.warnings)


def test_internal_workflow_refuses_a_fan_contour_session_before_any_node_runs():
    orchestrator = build_shoot_day_plan_workflow(make_deps())
    state = WorkflowState(
        context=ProjectContext("film-123", "fan_9", contour=Contour.FAN, script_version="v17"),
        goal="собери план съёмочного дня по сценам cast backlot",
    )

    run = orchestrator.run(state)

    assert run.status is RunStatus.BLOCKED
    assert "may not run workflow" in run.reason
    assert [entry.node for entry in run.audit] == ["contour_check"]
    assert run.state.citations == [], "no retrieval may happen on a refused contour"
    assert run.state.approval_request is None


def test_governance_blocks_when_above_ceiling_data_reached_the_output():
    # Defence in depth: the citation is planted as if a retrieval filter had
    # failed, and governance must catch it independently.
    from pinocut.agentplatform import Citation, QualityGovernanceAgent

    state = WorkflowState(
        context=ProjectContext("film-123", "fan_9", contour=Contour.FAN),
        goal="what is in the script",
    )
    state.citations.append(
        Citation(
            doc_id="script_v17",
            source="script_v17.pdf",
            snippet="INT. STUDIO A - DAY",
            data_class=DataClass.PRIVATE_DEVELOPMENT,
        )
    )

    result = QualityGovernanceAgent().run(state, make_deps())

    assert state.qa_report.blocking
    assert state.qa_report.access_ok is False
    assert any(issue["severity"] == "critical" for issue in result.output["issues"])


def test_withheld_documents_are_reported_but_do_not_block():
    orchestrator = build_fan_answer_workflow(make_deps())
    state = WorkflowState(
        context=ProjectContext("film-123", "fan_9", contour=Contour.FAN),
        goal="which practical backlot scenes and cast are in the film",
    )

    run = orchestrator.run(state)

    assert run.state.denied_reads, "the private script was withheld from the fan contour"
    assert run.status is RunStatus.COMPLETED
    assert not run.state.qa_report.blocking


def test_fan_agent_refuses_when_nothing_public_matches():
    state = WorkflowState(
        context=ProjectContext("film-123", "fan_9", contour=Contour.FAN),
        goal="what happens in the unreleased finale rig sequence",
    )

    result = FanExperienceAgent().run(state, make_deps())

    assert result.output["grounded"] is False
    assert result.output["answer"] == FanExperienceAgent.REFUSAL
    assert result.output["citations"] == []


def test_fan_workflow_answers_from_published_material_only():
    deps = make_deps()
    orchestrator = build_fan_answer_workflow(deps)
    state = WorkflowState(
        context=ProjectContext("film-123", "fan_9", contour=Contour.FAN),
        goal="where does the film shoot practical backlot scenes",
    )

    run = orchestrator.run(state)

    assert run.status is RunStatus.COMPLETED
    assert all(citation.doc_id == "press_kit" for citation in run.state.citations)


def test_stub_model_output_is_charged_to_the_usage_ledger():
    llm = StubLanguageModel({"script_analyst.summary": {"notes": ["act two runs long"]}})
    deps = make_deps(llm=llm)
    orchestrator = build_shoot_day_plan_workflow(deps)

    run = orchestrator.run(internal_state())

    assert run.state.scene_breakdown["notes"] == ["act two runs long"]
    assert run.state.usage.total_usd > 0
    assert ModelTier.PRO in run.state.usage.by_tier()


def test_approval_gate_defaults_to_requiring_a_human():
    gate = ApprovalGate()

    assert gate.requires_approval(ActionKind.FAN_RELEASE)
    assert gate.requires_approval(ActionKind.SCHEDULE_COMMIT)
    assert gate.requires_approval(ActionKind.PARTNER_WRITE)
    assert not gate.requires_approval(ActionKind.DRAFT_ARTIFACT)


def test_idempotency_key_is_stable_across_identical_requests():
    from pinocut.agentplatform.agents import SchedulePlannerAgent

    first = SchedulePlannerAgent.idempotency_key(internal_state())
    second = SchedulePlannerAgent.idempotency_key(internal_state())

    assert first == second
    assert first != SchedulePlannerAgent.idempotency_key(internal_state("другая цель"))


# --- corpus manifests and CLI --------------------------------------------------


def test_manifest_rejects_an_unknown_data_class(tmp_path):
    from pinocut.agentplatform.corpus import load_manifest

    manifest = tmp_path / "corpus.json"
    manifest.write_text(
        json.dumps([{"docId": "a", "source": "a.md", "dataClass": "secret", "text": "x"}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown dataClass"):
        load_manifest(manifest)


def test_manifest_requires_text_or_path(tmp_path):
    from pinocut.agentplatform.corpus import load_manifest

    manifest = tmp_path / "corpus.json"
    manifest.write_text(
        json.dumps([{"docId": "a", "source": "a.md", "dataClass": "public_fan_safe"}]),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="needs either"):
        load_manifest(manifest)


def test_manifest_loads_documents_from_disk(tmp_path):
    from pinocut.agentplatform.corpus import load_manifest

    (tmp_path / "script.txt").write_text(SCRIPT, encoding="utf-8")
    manifest = tmp_path / "corpus.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "docId": "script_v17",
                    "source": "script_v17.pdf",
                    "dataClass": "private_development",
                    "path": "script.txt",
                }
            ]
        ),
        encoding="utf-8",
    )

    documents = load_manifest(manifest)

    assert documents[0].data_class is DataClass.PRIVATE_DEVELOPMENT
    assert "INT. STUDIO A - DAY" in documents[0].text


def test_example_corpus_runs_end_to_end_through_the_cli(tmp_path, capsys):
    from pinocut.cli import main

    out = tmp_path / "run.json"
    code = main(
        [
            "platform",
            "plan",
            "docs/gemini-platform/corpus.example.json",
            "--goal",
            "собери план съёмочного дня по сценам 12-18",
            "--script-version",
            "v17",
            "--out",
            str(out),
        ]
    )

    assert code == 0
    printed = capsys.readouterr().out
    assert "awaiting_approval" in printed
    assert "the CLI does not approve" in printed

    run = json.loads(out.read_text(encoding="utf-8"))
    assert run["status"] == "awaiting_approval"
    assert run["state"]["approvedOptionId"] is None
    assert [scene["location"] for scene in run["state"]["sceneBreakdown"]["scenes"]] == [
        "STUDIO A",
        "BACKLOT",
        "STUDIO A",
    ]
    assert len({citation["docId"] for citation in run["state"]["citations"]}) == len(
        run["state"]["citations"]
    ), "a document must be cited once per run"
