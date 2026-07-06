"""Tests for the Bassito execution layer and the regeneration loop."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pinocut.bassito import (
    BackendError,
    BassitoContext,
    BassitoRunner,
    FfmpegEffectsBackend,
    GenerativeBackend,
)
from pinocut.scene_stitcher import SceneBuildRequest, SceneStitcherAgent
from pinocut.state import BassitoJob

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")


def make_job(job_type: str = "extend", **overrides) -> BassitoJob:
    defaults = dict(
        job_id=f"scene_t_{job_type}_01",
        job_type=job_type,
        prompt="test prompt",
        source_clip_id="clip_a" if job_type != "bridge_shot" else None,
        replaces_clip_id="clip_a" if job_type != "bridge_shot" else None,
    )
    defaults.update(overrides)
    return BassitoJob(**defaults)


class StubBackend(GenerativeBackend):
    name = "stub"

    def __init__(self, *, available: bool = True, artifact: Path | None = None):
        self._available = available
        self._artifact = artifact

    def is_available(self) -> bool:
        return self._available

    def run(self, job: BassitoJob, context: BassitoContext) -> Path:
        if self._artifact is None:
            raise BackendError("stub failure")
        return self._artifact


def make_context(tmp_path: Path) -> BassitoContext:
    return BassitoContext(scene_id="scene_t", output_dir=tmp_path)


def test_runner_marks_job_failed_without_backend(tmp_path: Path) -> None:
    runner = BassitoRunner(backends=[StubBackend(available=False)])
    job = make_job()
    runner.run_jobs([job], make_context(tmp_path))

    assert job.status == "failed"
    assert "backend" in (job.error or "").lower()


def test_runner_records_artifact_and_backend(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.mp4"
    artifact.write_bytes(b"fake")
    runner = BassitoRunner(backends=[StubBackend(artifact=artifact)])
    job = make_job()
    runner.run_jobs([job], make_context(tmp_path))

    assert job.status == "done"
    assert job.backend == "stub"
    assert job.artifact_path == str(artifact)


def test_runner_captures_backend_error(tmp_path: Path) -> None:
    runner = BassitoRunner(backends=[StubBackend()])
    job = make_job()
    runner.run_jobs([job], make_context(tmp_path))

    assert job.status == "failed"
    assert job.error == "stub failure"


def test_runner_skips_non_queued_jobs(tmp_path: Path) -> None:
    runner = BassitoRunner(backends=[StubBackend()])
    job = make_job(status="done", artifact_path="kept.mp4")
    runner.run_jobs([job], make_context(tmp_path))

    assert job.status == "done"
    assert job.artifact_path == "kept.mp4"


def test_ffmpeg_backend_requires_source_clip(tmp_path: Path) -> None:
    backend = FfmpegEffectsBackend()
    job = make_job("extend", source_clip_id="missing")
    with pytest.raises(BackendError):
        backend.run(job, make_context(tmp_path))


# ── e2e: full regeneration round with the real ffmpeg backend ──


def generate_clip(path: Path, *, duration: float, freq: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=duration={duration}:size=1280x720:rate=24",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest",
            "-y", str(path),
        ],
        check=True,
        timeout=120,
    )


@requires_ffmpeg
def test_regeneration_round_extends_and_bridges(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    # Short takes against a long target: forces extend jobs and a bridge shot.
    for index, name in enumerate(["gate_wide", "gate_move", "gate_detail", "gate_close"]):
        generate_clip(clips_dir / f"{name}.mp4", duration=2.4, freq=220 + index * 60)

    request = SceneBuildRequest(
        input_folder=clips_dir,
        output_dir=tmp_path / "out",
        scene_id="scene_regen",
        scene_goal="arrival at abandoned spaceport gate",
        target_duration_sec=24.0,
    )
    agent = SceneStitcherAgent()
    scene_state = agent.build_from_request(request)

    queued = list(scene_state.bridge_jobs) + list(scene_state.regeneration_jobs)
    assert queued, "short clips against a long target must queue Bassito jobs"
    first_duration = scene_state.timeline.actual_duration_sec

    scene_state = agent.run_bassito_round(scene_state)

    done = [job for job in scene_state.bassito_history if job.status == "done"]
    assert done, "at least one job must complete via the ffmpeg backend"
    for job in done:
        assert Path(job.artifact_path).exists()

    assert scene_state.timeline_version == 2
    assert scene_state.timeline is not None
    assert scene_state.timeline.actual_duration_sec > first_duration

    generated = [
        clip
        for clip in scene_state.available_clips
        if clip.metadata.get("bassito_generated")
    ]
    assert generated, "artifacts must re-enter the clip pool"

    bridge_clips = [
        clip
        for clip in scene_state.available_clips
        if clip.metadata.get("bassito_job_type") == "bridge_shot"
    ]
    assert bridge_clips, "bridge shot must be added as a new clip"

    video_path = Path(scene_state.reviews["video_path"])
    assert video_path.exists()

    versions = sorted((tmp_path / "out").glob("*.timeline.v*.json"))
    assert len(versions) == 2, "both timeline versions must be persisted"


@requires_ffmpeg
def test_second_round_does_not_requeue_generated_clips(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    generate_clip(clips_dir / "solo_short.mp4", duration=2.2, freq=300)

    request = SceneBuildRequest(
        input_folder=clips_dir,
        output_dir=tmp_path / "out",
        scene_id="scene_requeue",
        scene_goal="lonely corridor walk",
        target_duration_sec=10.0,
    )
    agent = SceneStitcherAgent()
    scene_state = agent.build_from_request(request)
    assert scene_state.regeneration_jobs or scene_state.bridge_jobs

    scene_state = agent.run_bassito_round(scene_state)

    extend_requeued = [
        job
        for job in scene_state.regeneration_jobs
        if job.source_clip_id and "bx_" in job.source_clip_id
    ]
    assert not extend_requeued, "generated clips must not re-queue their own regeneration"
    bridge_requeued = [job for job in scene_state.bridge_jobs if job.job_type == "bridge_shot"]
    assert not bridge_requeued, "a scene with a bridge artifact must not queue another bridge"
