"""Deterministic scene-level tools for Pinnocat Scene Stitcher v1."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from pinocut.config import ProjectConfig
from pinocut.render_service import SceneRenderService
from pinocut.state import (
    BassitoJob,
    ClipSegment,
    ExportProfile,
    SceneState,
    SubtitleSegment,
    TimelineAudioSegment,
    TimelineSegment,
    TimelineV1,
    TransitionSpec,
)
from pinocut.utils.logging import StageLogger


class SceneToolbox:
    """Bounded deterministic operations for scene assembly."""

    def __init__(self, *, structured_logs: bool = False):
        self.log = StageLogger("scene_tools", structured=structured_logs)
        self.renderer = SceneRenderService(structured_logs=structured_logs)

    def import_clips(self, paths: list[Path], config: ProjectConfig) -> list[ClipSegment]:
        clips: list[ClipSegment] = []
        for path in paths[: config.max_clips]:
            clip = self._probe_clip(path, config)
            if clip is not None:
                clips.append(clip)
        self.log.info(f"Imported {len(clips)} clips")
        return clips

    def extract_metadata(self, clip: ClipSegment) -> dict:
        return {
            "clip_id": clip.path.stem,
            "path": str(clip.path),
            "duration_sec": clip.duration,
            "has_audio": clip.has_audio,
            "resolution": f"{clip.width}x{clip.height}",
            "fps": clip.fps,
            "metadata": dict(clip.metadata),
        }

    def normalize_media(
        self,
        clip: ClipSegment,
        *,
        resolution: str,
        fps: int,
        audio_rate: int,
    ) -> ClipSegment:
        clip.metadata = {
            **clip.metadata,
            "normalized_to": {
                "resolution": resolution,
                "fps": fps,
                "audio_rate": audio_rate,
            },
        }
        return clip

    def trim_clip(
        self,
        clip_id: str,
        *,
        start_sec: float,
        end_sec: float,
        timeline_in_sec: float,
        segment_id: str,
    ) -> TimelineSegment:
        duration = max(0.0, end_sec - start_sec)
        return TimelineSegment(
            segment_id=segment_id,
            clip_id=clip_id,
            source_in_sec=round(start_sec, 3),
            source_out_sec=round(end_sec, 3),
            timeline_in_sec=round(timeline_in_sec, 3),
            timeline_out_sec=round(timeline_in_sec + duration, 3),
        )

    def add_transition(
        self,
        left_clip: str,
        right_clip: str,
        *,
        transition_type: str,
        duration_sec: float,
    ) -> TransitionSpec:
        self.log.debug(
            f"Transition {transition_type} between {left_clip} and {right_clip}"
        )
        return TransitionSpec(type=transition_type, duration_sec=round(duration_sec, 3))

    def concat_clips(
        self,
        segments: list[TimelineSegment],
        *,
        transition_type: str = "cut",
    ) -> list[TimelineSegment]:
        if not segments:
            return []

        for idx, segment in enumerate(segments[:-1]):
            segment.transition_out = TransitionSpec(type=transition_type, duration_sec=0.0)
            segments[idx + 1].transition_in = TransitionSpec(type=transition_type, duration_sec=0.0)
        return segments

    def reorder_timeline(
        self,
        segments: list[TimelineSegment],
        clip_ids: list[str],
    ) -> list[TimelineSegment]:
        segment_map = {segment.clip_id: segment for segment in segments}
        return [segment_map[clip_id] for clip_id in clip_ids if clip_id in segment_map]

    def insert_clip(
        self,
        segments: list[TimelineSegment],
        *,
        position: int,
        segment: TimelineSegment,
    ) -> list[TimelineSegment]:
        updated = list(segments)
        updated.insert(position, segment)
        return updated

    def add_music(self, track_id: str, *, gain_db: float) -> TimelineAudioSegment:
        return TimelineAudioSegment(
            track_id=track_id,
            role="music",
            timeline_in_sec=0.0,
            gain_db=gain_db,
            duck_music=False,
        )

    def mix_voiceover(
        self,
        track_id: str,
        *,
        duck_music: bool = True,
    ) -> TimelineAudioSegment:
        return TimelineAudioSegment(
            track_id=track_id,
            role="voiceover",
            timeline_in_sec=0.0,
            duck_music=duck_music,
        )

    def normalize_audio(
        self,
        tracks: list[TimelineAudioSegment],
        *,
        target_lufs: float,
    ) -> list[TimelineAudioSegment]:
        for track in tracks:
            track.gain_db = round(track.gain_db, 2)
        self.log.debug(f"Normalized audio plan to target {target_lufs} LUFS")
        return tracks

    def generate_subtitles(self, language: str) -> list[SubtitleSegment]:
        self.log.debug(f"Generated empty subtitle track for {language}")
        return []

    def burn_subtitles(self, style_preset: str) -> str:
        return style_preset

    def render_preview(self, scene_state: SceneState) -> Path:
        preview_path = Path(scene_state.output_dir) / f"{scene_state.scene_id}.preview.json"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_payload = {
            "scene_id": scene_state.scene_id,
            "project_id": scene_state.project_id,
            "status": "preview_ready",
            "timeline_version": scene_state.timeline_version,
            "approved_clips": scene_state.approved_clips,
            "rejected_clips": scene_state.rejected_clips,
        }
        preview_path.write_text(json.dumps(preview_payload, indent=2), encoding="utf-8")
        return preview_path

    def export_scene(self, timeline: TimelineV1, profile: ExportProfile, output_dir: str) -> Path:
        timeline_path = Path(output_dir) / f"{timeline.scene_id}.timeline.json"
        timeline_path.parent.mkdir(parents=True, exist_ok=True)
        payload = timeline.to_dict()
        payload["export_profile"] = {
            "resolution": profile.resolution,
            "fps": profile.fps,
            "format": profile.format,
        }
        timeline_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return timeline_path

    def save_timeline_version(self, timeline: TimelineV1, output_dir: str, comment: str) -> Path:
        version_path = Path(output_dir) / f"{timeline.scene_id}.timeline.v{timeline.timeline_version}.json"
        version_path.parent.mkdir(parents=True, exist_ok=True)
        payload = timeline.to_dict()
        payload["version_comment"] = comment
        version_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return version_path

    def render_scene_media(self, scene_state: SceneState, *, preview: bool = False) -> Path | None:
        """Render the scene timeline to an mp4; None when render is unavailable."""
        if scene_state.timeline is None:
            return None
        clip_paths = {clip.path.stem: clip.path for clip in scene_state.available_clips}
        suffix = ".preview.mp4" if preview else f".{scene_state.export_profile.format}"
        output_path = Path(scene_state.output_dir) / f"{scene_state.scene_id}{suffix}"
        return self.renderer.render(
            scene_state.timeline,
            clip_paths,
            output_path,
            preview=preview,
        )

    def request_extend(
        self, clip_id: str, prompt: str, *, params: dict | None = None
    ) -> BassitoJob:
        return BassitoJob(
            job_id="",
            job_type="extend",
            prompt=prompt,
            source_clip_id=clip_id,
            replaces_clip_id=clip_id,
            params=params or {},
        )

    def request_restyle(
        self, clip_id: str, prompt: str, *, params: dict | None = None
    ) -> BassitoJob:
        return BassitoJob(
            job_id="",
            job_type="restyle",
            prompt=prompt,
            source_clip_id=clip_id,
            replaces_clip_id=clip_id,
            params=params or {},
        )

    def request_bridge_shot(self, prompt: str, *, params: dict | None = None) -> BassitoJob:
        return BassitoJob(
            job_id="",
            job_type="bridge_shot",
            prompt=prompt,
            params=params or {},
        )

    def probe_media(self, filepath: Path, *, min_duration: float = 0.1) -> ClipSegment | None:
        """Probe an arbitrary media file (e.g. a Bassito artifact) into a ClipSegment."""
        return self._probe_clip(filepath, ProjectConfig(min_duration=min_duration))

    def _probe_clip(self, filepath: Path, config: ProjectConfig) -> ClipSegment | None:
        probed = self._probe_with_moviepy(filepath)
        if probed is None:
            probed = self._probe_with_ffprobe(filepath)
        if probed is None:
            probed = self._probe_with_opencv(filepath)
        if probed is None:
            return None

        duration, has_audio, resolution, fps = probed
        if duration < config.min_duration:
            return None
        if config.max_duration and duration > config.max_duration:
            duration = config.max_duration
        return ClipSegment(
            path=filepath,
            duration=duration,
            has_audio=has_audio,
            resolution=resolution,
            fps=fps,
            metadata={},
        )

    def _probe_with_moviepy(
        self, filepath: Path
    ) -> tuple[float, bool, tuple[int, int], float] | None:
        try:
            from moviepy.editor import VideoFileClip

            with VideoFileClip(str(filepath)) as vclip:
                return (
                    float(vclip.duration),
                    vclip.audio is not None,
                    (vclip.w, vclip.h),
                    float(vclip.fps),
                )
        except Exception:
            return None

    def _probe_with_opencv(
        self, filepath: Path
    ) -> tuple[float, bool, tuple[int, int], float] | None:
        """Last-resort probe via cv2 (no moviepy/ffprobe on the host).

        OpenCV cannot see audio streams, so ``has_audio`` is reported False;
        audio-dependent steps degrade the same way they do for silent clips.
        """
        try:
            import cv2

            capture = cv2.VideoCapture(str(filepath))
            try:
                if not capture.isOpened():
                    return None
                frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
                fps = float(capture.get(cv2.CAP_PROP_FPS)) or 24.0
                width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if frame_count <= 0 or fps <= 0 or width <= 0 or height <= 0:
                    return None
                return (frame_count / fps, False, (width, height), fps)
            finally:
                capture.release()
        except Exception:
            return None

    def _probe_with_ffprobe(
        self, filepath: Path
    ) -> tuple[float, bool, tuple[int, int], float] | None:
        if shutil.which("ffprobe") is None:
            return None
        try:
            proc = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-print_format", "json",
                    "-show_format", "-show_streams",
                    str(filepath),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0:
                return None
            payload = json.loads(proc.stdout)
            video = next(
                (s for s in payload.get("streams", []) if s.get("codec_type") == "video"),
                None,
            )
            if video is None:
                return None
            duration = float(payload.get("format", {}).get("duration", 0.0))
            has_audio = any(
                s.get("codec_type") == "audio" for s in payload.get("streams", [])
            )
            num, _, den = str(video.get("avg_frame_rate", "24/1")).partition("/")
            fps = float(num) / float(den) if den and float(den) else 24.0
            return (
                duration,
                has_audio,
                (int(video.get("width", 0)), int(video.get("height", 0))),
                fps,
            )
        except Exception:
            return None
