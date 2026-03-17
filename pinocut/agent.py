"""PinoCut Agent — LangGraph StateGraph orchestration.

Phase 2-3 upgrades:
- MoltisBridge for parallel analysis
- RomeoVisionClient injection into AnalyzeStage
- E2B SandboxExecutor injection into RenderStage
- MetricsCollector for Romeo PhD analytics
- Dual render mode (moviepy / ffmpeg)
"""

from __future__ import annotations

from pathlib import Path

from langgraph.graph import END, StateGraph

from pinocut.config import ProjectConfig
from pinocut.integrations.e2b_sandbox import SandboxConfig, SandboxExecutor
from pinocut.integrations.moltis_bridge import MoltisBridge, MoltisConfig
from pinocut.integrations.romeo_phd import MetricsCollector
from pinocut.integrations.romeo_vision import RomeoVisionClient, RomeoVisionConfig
from pinocut.stages import (
    AnalyzeStage,
    AudioMixStage,
    IngestStage,
    RenderStage,
    TitleStage,
    TransitionStage,
)
from pinocut.state import PinoCutState
from pinocut.utils.errors import ErrorSeverity
from pinocut.utils.logging import StageLogger

log = StageLogger("agent")


def _has_fatal_error(state: PinoCutState) -> bool:
    return any(e.severity == ErrorSeverity.FATAL for e in state.get("errors", []))


def route_after_ingest(state: PinoCutState) -> str:
    if _has_fatal_error(state) or not state.get("clips"):
        return "abort"
    return "analyze"


def route_after_analyze(state: PinoCutState) -> str:
    if _has_fatal_error(state):
        return "abort"
    if len(state.get("clips", [])) <= 1:
        return "titles"
    return "transitions"


def abort_node(state: PinoCutState) -> PinoCutState:
    errors = state.get("errors", [])
    fatal = [e for e in errors if e.severity == ErrorSeverity.FATAL]
    for e in fatal:
        log.error(str(e))
    log.error(f"Pipeline aborted with {len(fatal)} fatal error(s)")
    return state


def build_graph(
    *,
    structured_logs: bool = False,
    moltis: MoltisBridge | None = None,
    vision_client: RomeoVisionClient | None = None,
    sandbox: SandboxExecutor | None = None,
    render_mode: str = "moviepy",
) -> StateGraph:
    """Build the PinoCut LangGraph pipeline with injected dependencies."""

    graph = StateGraph(PinoCutState)

    graph.add_node("ingest", IngestStage(structured_logs=structured_logs))
    graph.add_node("analyze", AnalyzeStage(
        structured_logs=structured_logs, moltis=moltis, vision_client=vision_client,
    ))
    graph.add_node("transitions", TransitionStage(structured_logs=structured_logs))
    graph.add_node("titles", TitleStage(structured_logs=structured_logs))
    graph.add_node("audio_mix", AudioMixStage(structured_logs=structured_logs))
    graph.add_node("render", RenderStage(
        structured_logs=structured_logs, render_mode=render_mode, sandbox=sandbox,
    ))
    graph.add_node("abort", abort_node)

    graph.set_entry_point("ingest")

    graph.add_conditional_edges("ingest", route_after_ingest, {
        "analyze": "analyze", "abort": "abort",
    })
    graph.add_conditional_edges("analyze", route_after_analyze, {
        "transitions": "transitions", "titles": "titles", "abort": "abort",
    })

    graph.add_edge("transitions", "titles")
    graph.add_edge("titles", "audio_mix")
    graph.add_edge("audio_mix", "render")
    graph.add_edge("render", END)
    graph.add_edge("abort", END)

    return graph.compile()


class PinoCutAgent:
    """High-level agent interface with full ecosystem integration.

    Usage:
        agent = PinoCutAgent()
        result = agent.run(ProjectConfig(input_folder=Path("./footage")))
        print(agent.metrics_report())
    """

    def __init__(self, *, structured_logs: bool = False):
        self.structured_logs = structured_logs
        self._moltis: MoltisBridge | None = None
        self._vision: RomeoVisionClient | None = None
        self._sandbox: SandboxExecutor | None = None
        self._metrics = MetricsCollector()
        self._graph = None  # lazy build

    def run(self, config: ProjectConfig) -> PinoCutState:
        """Execute the full pipeline."""
        log.info(f"PinoCut Agent v1.0 starting — {config.input_folder}")

        # ── Initialize integrations based on config ──
        self._init_integrations(config)

        # ── Build graph with injected dependencies ──
        self._graph = build_graph(
            structured_logs=self.structured_logs,
            moltis=self._moltis,
            vision_client=self._vision,
            sandbox=self._sandbox,
            render_mode=config.render_mode,
        )

        # ── Start metrics ──
        if config.metrics_enabled:
            self._metrics = MetricsCollector(persist_path=config.metrics_path)
            self._metrics.start_pipeline()

        # ── Initial state ──
        initial_state: PinoCutState = {
            "project_config": config,
            "clips": [],
            "analysis": {},
            "romeo_data": {},
            "edit_decisions": [],
            "title_overlays": [],
            "audio_mix": None,
            "output_path": None,
            "render_preset": config.render_preset.value,
            "errors": [],
        }

        # ── Run graph ──
        result = self._graph.invoke(initial_state)

        # ── Finalize metrics ──
        if config.metrics_enabled:
            output_path = Path(result["output_path"]) if result.get("output_path") else None
            output_duration = 0.0
            if result.get("clips"):
                output_duration = sum(c.duration for c in result["clips"])
            self._metrics.end_pipeline(
                success=result.get("output_path") is not None,
                output_path=output_path,
                output_duration=output_duration,
                total_clips=len(result.get("clips", [])),
                render_preset=config.render_preset.value,
            )

        # ── Cleanup ──
        self._cleanup()

        # ── Summary ──
        errors = result.get("errors", [])
        fatal = [e for e in errors if e.severity == ErrorSeverity.FATAL]
        warns = [e for e in errors if e.severity == ErrorSeverity.WARN]
        if result.get("output_path"):
            log.info(f"Success: {result['output_path']}")
        else:
            log.error("Pipeline failed — no output")
        if warns:
            log.warn(f"{len(warns)} warning(s)")
        if fatal:
            log.error(f"{len(fatal)} fatal error(s)")

        return result

    def metrics_report(self) -> str:
        """Get human-readable metrics report."""
        return self._metrics.report_text()

    def metrics_json(self) -> dict:
        """Get metrics as dict (for Romeo PhD)."""
        return self._metrics.report()

    @property
    def graph(self):
        return self._graph

    def _init_integrations(self, config: ProjectConfig) -> None:
        """Lazily initialize integrations based on config."""
        # Moltis
        if config.parallel:
            moltis_config = MoltisConfig(
                memory_backend="json_file" if config.memory_persist else "local",
            )
            self._moltis = MoltisBridge(config=moltis_config)

        # Romeo Vision
        self._vision = RomeoVisionClient()

        # E2B Sandbox
        if config.sandbox == "e2b":
            self._sandbox = SandboxExecutor(SandboxConfig(mode="e2b"))

    def _cleanup(self) -> None:
        """Flush caches and shutdown."""
        if self._moltis:
            self._moltis.shutdown()
