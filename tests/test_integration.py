"""Integration tests — Phase 2-3 ecosystem components."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pinocut.integrations.e2b_sandbox import SandboxConfig, SandboxExecutor, SecurityError
from pinocut.integrations.moltis_bridge import Channel, ChannelClosed, Memory, MoltisBridge, TaskScheduler
from pinocut.integrations.romeo_phd import MetricsCollector, PipelineMetrics
from pinocut.integrations.romeo_vision import MockVisionProvider, RomeoVisionClient
from pinocut.integrations.swarm_registry import AgentCard, SwarmRegistry


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
