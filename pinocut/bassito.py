"""Bassito execution layer: run queued visual-generation jobs.

Bassito never edits the timeline. It turns queued :class:`BassitoJob`
requests into clip artifacts on disk; ``SceneStitcherAgent`` integrates the
artifacts into the clip pool and rebuilds the scene (timeline_version + 1),
so every generated clip re-enters Andrew's quality gate like any other take.

Backends implement :class:`GenerativeBackend`. The first shipped backend is
:class:`FfmpegEffectsBackend` — a deterministic, local, zero-cost executor:

- ``extend``      → freeze-tail padding (tpad clone) up to the target duration
- ``restyle``     → stabilization + style filter chain (contrast/saturation/vignette)
- ``bridge_shot`` → Ken Burns move on the anchor clip's final frame

Cloud/generative providers (Runway, Kling, ComfyUI, ...) plug in by
implementing the same interface.
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from pinocut.state import BassitoJob, BassitoJobStatus, ClipSegment
from pinocut.utils.logging import StageLogger


class BackendError(RuntimeError):
    """A backend failed to produce an artifact for a job."""


@dataclass(slots=True)
class BassitoContext:
    """Everything a backend needs to execute jobs for one scene."""

    scene_id: str
    output_dir: Path
    clips: dict[str, ClipSegment] = field(default_factory=dict)
    resolution: str = "1920x1080"
    fps: int = 24
    style_profile: str = "cinematic"

    @property
    def artifact_dir(self) -> Path:
        return self.output_dir / "bassito"

    def size(self) -> tuple[int, int]:
        try:
            width_text, height_text = self.resolution.lower().split("x", 1)
            return int(width_text), int(height_text)
        except ValueError:
            return 1920, 1080


class GenerativeBackend(ABC):
    """Contract for Bassito job executors."""

    name: str = "backend"

    def is_available(self) -> bool:
        return True

    def can_run(self, job: BassitoJob) -> bool:
        return job.job_type in ("extend", "restyle", "bridge_shot")

    @abstractmethod
    def run(self, job: BassitoJob, context: BassitoContext) -> Path:
        """Produce a clip artifact for the job; raise BackendError on failure."""


class FfmpegEffectsBackend(GenerativeBackend):
    """Local fallback backend built on plain ffmpeg filters."""

    name = "ffmpeg_effects"

    def __init__(self, *, structured_logs: bool = False, timeout_sec: int = 600):
        self.log = StageLogger("bassito_ffmpeg", structured=structured_logs)
        self._timeout = timeout_sec

    def is_available(self) -> bool:
        return shutil.which("ffmpeg") is not None

    def run(self, job: BassitoJob, context: BassitoContext) -> Path:
        context.artifact_dir.mkdir(parents=True, exist_ok=True)
        if job.job_type == "extend":
            return self._extend(job, context)
        if job.job_type == "restyle":
            return self._restyle(job, context)
        if job.job_type == "bridge_shot":
            return self._bridge_shot(job, context)
        raise BackendError(f"Unsupported job type: {job.job_type}")

    # ── operations ──

    def _extend(self, job: BassitoJob, context: BassitoContext) -> Path:
        clip = self._require_clip(job.source_clip_id, context)
        target = float(job.params.get("target_duration_sec", clip.duration + 2.0))
        extra = max(0.5, target - clip.duration)
        output = context.artifact_dir / f"{clip.path.stem}_bx_extend.mp4"

        vf = self._conform_filter(context) + f",tpad=stop_mode=clone:stop_duration={extra:.3f}"
        args = [
            "-i", str(clip.path),
            "-vf", vf,
            "-af", f"apad=pad_dur={extra:.3f}",
            "-t", f"{target:.3f}",
            *self._encode_args(),
            "-y", str(output),
        ]
        self._run(args, job)
        return output

    def _restyle(self, job: BassitoJob, context: BassitoContext) -> Path:
        clip = self._require_clip(job.source_clip_id, context)
        output = context.artifact_dir / f"{clip.path.stem}_bx_restyle.mp4"

        # Deterministic "grade + settle" pass: deshake for motion risk,
        # then a contrast/saturation lift with a soft vignette.
        vf = (
            "deshake,"
            + self._conform_filter(context)
            + ",eq=contrast=1.08:saturation=1.12:brightness=-0.02,vignette=PI/5"
        )
        args = [
            "-i", str(clip.path),
            "-vf", vf,
            "-c:a", "aac", "-b:a", "160k",
            *self._encode_args(audio=False),
            "-y", str(output),
        ]
        self._run(args, job)
        return output

    def _bridge_shot(self, job: BassitoJob, context: BassitoContext) -> Path:
        anchor_id = job.params.get("anchor_clip_id")
        anchor = context.clips.get(str(anchor_id)) if anchor_id else None
        if anchor is None and context.clips:
            anchor = next(iter(context.clips.values()))
        if anchor is None:
            raise BackendError("Bridge shot requires at least one anchor clip")

        duration = float(job.params.get("duration_sec", 4.0))
        duration = min(8.0, max(2.0, duration))
        width, height = context.size()
        job_slug = job.job_id or f"{context.scene_id}_bridge"
        frame = context.artifact_dir / f"{job_slug}.png"
        output = context.artifact_dir / f"{job_slug}.mp4"

        # Grab the anchor's closing frame, then drift into it (Ken Burns).
        self._run(
            [
                "-sseof", "-0.25",
                "-i", str(anchor.path),
                "-frames:v", "1",
                "-y", str(frame),
            ],
            job,
        )
        total_frames = int(duration * context.fps)
        zoompan = (
            f"scale={width * 2}:{height * 2},"
            f"zoompan=z='1+0.15*on/{total_frames}'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={total_frames}:s={width}x{height}:fps={context.fps}"
        )
        self._run(
            [
                "-loop", "1",
                "-t", f"{duration:.3f}",
                "-i", str(frame),
                "-f", "lavfi",
                "-t", f"{duration:.3f}",
                "-i", "anullsrc=r=48000:cl=stereo",
                "-vf", zoompan + ",format=yuv420p",
                "-map", "0:v", "-map", "1:a",
                "-shortest",
                *self._encode_args(audio=False),
                "-c:a", "aac", "-b:a", "160k",
                "-y", str(output),
            ],
            job,
        )
        frame.unlink(missing_ok=True)
        return output

    # ── helpers ──

    def _require_clip(self, clip_id: str | None, context: BassitoContext) -> ClipSegment:
        clip = context.clips.get(str(clip_id)) if clip_id else None
        if clip is None or not clip.path.exists():
            raise BackendError(f"Source clip not found for job: {clip_id}")
        return clip

    def _conform_filter(self, context: BassitoContext) -> str:
        width, height = context.size()
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={context.fps},format=yuv420p,setsar=1"
        )

    def _encode_args(self, *, audio: bool = True) -> list[str]:
        args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21"]
        if audio:
            args += ["-c:a", "aac", "-b:a", "160k"]
        return args

    def _run(self, args: list[str], job: BassitoJob) -> None:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout)
        except subprocess.TimeoutExpired as exc:
            raise BackendError(f"ffmpeg timed out for job {job.job_id}") from exc
        except FileNotFoundError as exc:
            raise BackendError("ffmpeg not found in PATH") from exc
        if proc.returncode != 0:
            raise BackendError(
                f"ffmpeg failed for job {job.job_id}: {proc.stderr[-300:]}"
            )


class BassitoRunner:
    """Execute queued Bassito jobs with the first capable backend."""

    def __init__(
        self,
        backends: list[GenerativeBackend] | None = None,
        *,
        structured_logs: bool = False,
    ):
        self.log = StageLogger("bassito_runner", structured=structured_logs)
        self.backends = backends or [FfmpegEffectsBackend(structured_logs=structured_logs)]

    def run_jobs(self, jobs: list[BassitoJob], context: BassitoContext) -> list[BassitoJob]:
        """Run every queued job in place; statuses become done/failed."""
        for job in jobs:
            if job.status != BassitoJobStatus.QUEUED.value:
                continue
            backend = self._pick_backend(job)
            if backend is None:
                job.status = BassitoJobStatus.FAILED.value
                job.error = "No available backend for job type"
                self.log.warn(f"Job {job.job_id}: no backend available")
                continue

            job.status = BassitoJobStatus.RUNNING.value
            job.backend = backend.name
            try:
                artifact = backend.run(job, context)
            except BackendError as exc:
                job.status = BassitoJobStatus.FAILED.value
                job.error = str(exc)
                self.log.warn(f"Job {job.job_id} failed: {exc}")
                continue
            job.artifact_path = str(artifact)
            job.status = BassitoJobStatus.DONE.value
            self.log.info(f"Job {job.job_id} done via {backend.name}: {artifact}")
        return jobs

    def _pick_backend(self, job: BassitoJob) -> GenerativeBackend | None:
        for backend in self.backends:
            if backend.can_run(job) and backend.is_available():
                return backend
        return None
