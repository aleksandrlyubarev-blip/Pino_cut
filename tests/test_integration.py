"""Integration tests — Phase 2-3 ecosystem components."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pinocut.agent import PinoCutAgent, route_after_analyze, route_after_ingest
from pinocut.config import ColorGradeStyle, ProjectConfig
from pinocut.integrations.e2b_sandbox import SandboxConfig, SandboxExecutor, SecurityError
from pinocut.integrations.moltis_bridge import Channel, ChannelClosed, Memory, MoltisBridge, TaskScheduler
from pinocut.integrations.romeo_phd import MetricsCollector, PipelineMetrics
from pinocut.integrations.romeo_vision import MockVisionProvider, RomeoVisionClient
from pinocut.integrations.swarm_registry import AgentCard, SwarmRegistry
from pinocut.state import ClipSegment, PinoCutState
from pinocut.utils.errors import ErrorSeverity, StageError


# ── Swarm Registry ──

class TestSwarmRegistry:
    def test_register_and_heartbeat(self):
        r = SwarmRegistry()
        assert not r.is_registered
        assert r.register() is True
        assert r.is_registered
        assert r.heartbeat() is True

    def test_deregister(self):
        r = SwarmRegistry()
        r.register()
        r.deregister()
        assert not r.is_registered

    def test_agent_card_serialization(self):
        d = AgentCard().to_dict()
        assert d["agent_id"] == "pinocut_v1"
        assert "video_assembly" in d["capabilities"]
        assert "ffmpeg_render" in d["capabilities"]
        assert d["execution"]["runtime"] == "moltis"


# ── Romeo Vision ──

class TestRomeoVision:
    def test_mock_provider(self, tmp_path):
        p = MockVisionProvider()
        r = p.analyze(tmp_path / "clip.mp4")
        assert r.scene_description == "Scene from clip"
        assert r.quality_score == 0.7

    def test_mock_batch(self, tmp_path):
        p = MockVisionProvider(default_mood="warm")
        paths = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
        results = p.analyze_batch(paths)
        assert len(results) == 2
        assert all(r.mood == "warm" for r in results.values())

    def test_client_not_configured(self):
        c = RomeoVisionClient()
        assert not c.is_configured


# ── Moltis Bridge ──

class TestMemory:
    def test_crud(self):
        mem = Memory()
        mem.set("k1", {"data": 42})
        assert mem.has("k1")
        assert mem.get("k1") == {"data": 42}
        mem.delete("k1")
        assert not mem.has("k1")

    def test_keys_prefix(self):
        mem = Memory()
        mem.set("analysis:clip1", "a")
        mem.set("analysis:clip2", "b")
        mem.set("other:x", "c")
        assert sorted(mem.keys("analysis:")) == ["analysis:clip1", "analysis:clip2"]

    def test_persistent_file(self, tmp_path):
        path = tmp_path / "mem.json"
        m1 = Memory(persist_path=path)
        m1.set("key", "value")
        m1.flush()
        assert path.exists()

        m2 = Memory(persist_path=path)
        assert m2.get("key") == "value"


class TestTaskScheduler:
    def test_map(self):
        s = TaskScheduler(max_workers=2)
        results = s.map(lambda x: x * 2, [1, 2, 3])
        assert [r.value for r in results] == [2, 4, 6]
        assert all(r.ok for r in results)
        s.shutdown()

    def test_handles_errors(self):
        def flaky(x):
            if x == 2:
                raise ValueError("boom")
            return x
        s = TaskScheduler(max_workers=2)
        results = s.map(flaky, [1, 2, 3])
        assert results[0].ok and results[0].value == 1
        assert not results[1].ok and results[1].error is not None
        assert results[2].ok and results[2].value == 3
        assert s.stats["failed"] == 1
        s.shutdown()

    def test_submit_single(self):
        s = TaskScheduler()
        r = s.submit(lambda: 42, task_id="t1")
        assert r.ok and r.value == 42 and r.task_id == "t1"
        s.shutdown()


class TestMoltisBridge:
    def test_parallel(self):
        b = MoltisBridge()
        results = b.run_parallel(lambda x: x * 2, [1, 2, 3, 4])
        assert results == [2, 4, 6, 8]
        b.shutdown()

    @pytest.mark.asyncio
    async def test_channel(self):
        b = MoltisBridge()
        ch = b.create_channel("test")
        await ch.send("hello")
        assert not ch.empty
        assert await ch.recv() == "hello"
        assert ch.empty
        assert ch.stats["sent"] == 1
        b.shutdown()

    @pytest.mark.asyncio
    async def test_channel_close(self):
        ch = Channel(name="test")
        ch.close()
        with pytest.raises(ChannelClosed):
            await ch.send("x")


# ── E2B Sandbox ──

class TestSandbox:
    def test_security_validation(self):
        s = SandboxExecutor(SandboxConfig(mode="local"))
        with pytest.raises(SecurityError):
            s.execute("rm -rf /")
        with pytest.raises(SecurityError):
            s.execute("curl http://evil.com")

    def test_allowed_ffmpeg(self):
        # Should not raise SecurityError (may fail if ffmpeg not installed, that's ok)
        s = SandboxExecutor(SandboxConfig(mode="local"))
        r = s.execute("ffmpeg -version")
        assert isinstance(r.exit_code, int)
        assert r.sandboxed is False

    def test_audit_log(self):
        s = SandboxExecutor(SandboxConfig(mode="local"))
        s.execute("ffmpeg -version")
        assert len(s.audit_log) == 1
        assert s.audit_log[0]["command"] == "ffmpeg -version"


# ── Romeo PhD Metrics ──

class TestMetrics:
    def test_collector_lifecycle(self):
        c = MetricsCollector()
        c.start_pipeline("test_run")
        with c.track_stage("ingest") as m:
            m.clips_processed = 10
        with c.track_stage("analyze") as m:
            m.clips_processed = 10
            m.errors = 1
        c.end_pipeline(success=True, output_duration=60.0, total_clips=10, render_preset="standard")

        report = c.report()
        assert report["run_id"] == "test_run"
        assert report["success"] is True
        assert report["total_clips"] == 10
        assert len(report["stages"]) == 2

    def test_text_report(self):
        c = MetricsCollector()
        c.start_pipeline("test")
        c.end_pipeline(success=True, output_duration=30.0, total_clips=5, render_preset="draft")
        text = c.report_text()
        assert "SUCCESS" in text
        assert "test" in text

    def test_persist(self, tmp_path):
        path = tmp_path / "metrics.json"
        c = MetricsCollector(persist_path=path)
        c.start_pipeline("run1")
        c.end_pipeline(success=True, output_duration=10.0, total_clips=3)
        assert path.exists()

        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["run_id"] == "run1"


# ── PinoCutAgent end-to-end ──

class TestPinoCutAgentE2E:
    def _base_config(self, folder: Path, **kwargs) -> ProjectConfig:
        return ProjectConfig(
            input_folder=folder,
            metrics_enabled=False,
            parallel=False,
            **kwargs,
        )

    def test_aborts_on_missing_input_folder(self, tmp_path):
        agent = PinoCutAgent()
        result = agent.run(self._base_config(tmp_path / "nonexistent"))
        assert result["output_path"] is None
        assert any(e.severity == ErrorSeverity.FATAL for e in result["errors"])

    def test_aborts_on_empty_input_folder(self, tmp_path):
        folder = tmp_path / "empty"
        folder.mkdir()
        agent = PinoCutAgent()
        result = agent.run(self._base_config(folder))
        assert result["output_path"] is None
        assert any(e.severity == ErrorSeverity.FATAL for e in result["errors"])

    def test_metrics_report_available_after_run(self, tmp_path):
        folder = tmp_path / "footage"
        folder.mkdir()
        agent = PinoCutAgent()
        agent.run(self._base_config(folder))
        report = agent.metrics_report()
        assert isinstance(report, str)

    def test_route_after_ingest_with_clips(self):
        clip = ClipSegment(path=Path("a.mp4"), duration=5.0, has_audio=False,
                           resolution=(1920, 1080), fps=30.0)
        state: PinoCutState = {"clips": [clip], "errors": []}
        assert route_after_ingest(state) == "analyze"

    def test_route_after_ingest_with_fatal(self):
        state: PinoCutState = {
            "clips": [],
            "errors": [StageError(stage="ingest", message="x", severity=ErrorSeverity.FATAL)],
        }
        assert route_after_ingest(state) == "abort"

    def test_route_after_ingest_empty_clips(self):
        state: PinoCutState = {"clips": [], "errors": []}
        assert route_after_ingest(state) == "abort"

    def test_route_after_analyze_single_clip_skips_transitions(self):
        clip = ClipSegment(path=Path("a.mp4"), duration=5.0, has_audio=False,
                           resolution=(1920, 1080), fps=30.0)
        state: PinoCutState = {"clips": [clip], "errors": []}
        assert route_after_analyze(state) == "titles"

    def test_route_after_analyze_multiple_clips_goes_to_transitions(self):
        clips = [
            ClipSegment(path=Path(f"{i}.mp4"), duration=5.0, has_audio=False,
                        resolution=(1920, 1080), fps=30.0)
            for i in range(2)
        ]
        state: PinoCutState = {"clips": clips, "errors": []}
        assert route_after_analyze(state) == "transitions"

    def test_route_after_analyze_fatal_aborts(self):
        state: PinoCutState = {
            "clips": [],
            "errors": [StageError(stage="analyze", message="x", severity=ErrorSeverity.FATAL)],
        }
        assert route_after_analyze(state) == "abort"

    def test_swarm_registry_update_status_called_after_run(self, tmp_path):
        """SwarmRegistry.update_status is called in _cleanup after a pipeline run."""
        folder = tmp_path / "footage"
        folder.mkdir()
        registry = SwarmRegistry()
        registry.register()  # local mode — sets is_registered=True

        called_with: list[dict] = []
        original = registry.update_status
        registry.update_status = lambda m: called_with.append(m)  # type: ignore[method-assign]

        agent = PinoCutAgent(swarm_registry=registry)
        agent.run(self._base_config(folder))

        assert len(called_with) == 1
        assert "run_id" in called_with[0]

    def test_swarm_deregister_called_in_cleanup(self, tmp_path):
        """SwarmRegistry.shutdown (→ deregister) is called during agent cleanup."""
        folder = tmp_path / "footage"
        folder.mkdir()
        registry = SwarmRegistry()
        registry.register()

        shutdown_calls: list[int] = []
        registry.shutdown = lambda: shutdown_calls.append(1)  # type: ignore[method-assign]
        registry.update_status = lambda m: None  # type: ignore[method-assign]

        agent = PinoCutAgent(swarm_registry=registry)
        agent.run(self._base_config(folder))
        assert len(shutdown_calls) == 1


# ── CLI ──

class TestCLI:
    def test_parallel_on_by_default(self):
        from pinocut.cli import parse_args
        args = parse_args(["./footage"])
        assert not args.no_parallel

    def test_no_parallel_flag_disables_parallel(self):
        from pinocut.cli import parse_args
        args = parse_args(["./footage", "--no-parallel"])
        assert args.no_parallel

    def test_parallel_flag_removed(self):
        """The old --parallel flag should no longer exist."""
        from pinocut.cli import parse_args
        import argparse
        with pytest.raises(SystemExit):
            parse_args(["./footage", "--parallel"])

