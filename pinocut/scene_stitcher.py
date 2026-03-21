"""Scene-level planning and export for Pinnocat Scene Stitcher v1."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from pinocut.config import ProjectConfig, SUPPORTED_VIDEO_EXTENSIONS
from pinocut.scene_tools import SceneToolbox
from pinocut.state import (
    ClipScore,
    ClipSegment,
    ExportProfile,
    RomeoReview,
    SceneState,
    TimelineAudioSegment,
    TimelineSegment,
    TimelineV1,
)
from pinocut.utils.errors import ErrorSeverity, StageError
from pinocut.utils.logging import StageLogger


@dataclass(slots=True)
class SceneBuildRequest:
    input_folder: Path
    output_dir: Path = Path("./output")
    project_id: str = "rfv_pinnocat_001"
    scene_id: str = "scene_01"
    scene_goal: str = "assemble a scene"
    style_profile: str = "cinematic"
    target_duration_sec: float = 30.0
    editing_mode: str = "hybrid"
    editing_template: str = "cinematic_montage"
    max_clips: int = 10
    min_duration: float = 1.0
    max_duration: float | None = None
    music_path: Path | None = None
    voiceover_path: Path | None = None
    subtitle_language: str | None = None
    export_profile: ExportProfile = field(default_factory=ExportProfile)

    def to_project_config(self) -> ProjectConfig:
        return ProjectConfig(
            input_folder=self.input_folder,
            output_path=self.output_dir / f"{self.scene_id}.{self.export_profile.format}",
            bg_music_path=self.music_path,
            max_clips=self.max_clips,
            min_duration=self.min_duration,
            max_duration=self.max_duration,
        )


class SceneStitcherAgent:
    """Deterministic scene-level planner for Pinnocat v1."""

    def __init__(self, *, structured_logs: bool = False):
        self.log = StageLogger("scene_stitcher", structured=structured_logs)
        self.tools = SceneToolbox(structured_logs=structured_logs)

    def build_from_request(self, request: SceneBuildRequest) -> SceneState:
        clip_paths = self._discover_clip_paths(request.input_folder, request.max_clips)
        clips = self.tools.import_clips(clip_paths, request.to_project_config())

        scene_state = SceneState(
            project_id=request.project_id,
            scene_id=request.scene_id,
            scene_goal=request.scene_goal,
            style_profile=request.style_profile,
            target_duration_sec=request.target_duration_sec,
            editing_mode=request.editing_mode,
            editing_template=request.editing_template,
            available_clips=clips,
            music_track=str(request.music_path) if request.music_path else None,
            voiceover_track=str(request.voiceover_path) if request.voiceover_path else None,
            subtitle_track=request.subtitle_language,
            export_profile=request.export_profile,
            output_dir=str(request.output_dir),
        )
        return self.build_scene(scene_state)

    def build_scene(self, scene_state: SceneState) -> SceneState:
        self.log.info(
            f"Building scene {scene_state.scene_id} from {len(scene_state.available_clips)} clips"
        )
        if not scene_state.available_clips:
            scene_state.errors.append(
                StageError(
                    stage="scene_stitcher",
                    message="No source clips available for scene assembly",
                    severity=ErrorSeverity.FATAL,
                )
            )
            return scene_state
        self._normalize_clips(scene_state)
        self._score_clips(scene_state)
        self._select_clips(scene_state)
        self._assemble_timeline(scene_state)
        self._attach_audio(scene_state)
        self._attach_subtitles(scene_state)
        self._export(scene_state)
        return scene_state

    def _normalize_clips(self, scene_state: SceneState) -> None:
        target_audio_rate = 48000
        for clip in scene_state.available_clips:
            self.tools.normalize_media(
                clip,
                resolution=scene_state.export_profile.resolution,
                fps=scene_state.export_profile.fps,
                audio_rate=target_audio_rate,
            )

    def _score_clips(self, scene_state: SceneState) -> None:
        clip_scores: dict[str, ClipScore] = {}
        romeo_reviews: dict[str, RomeoReview] = {}
        for clip in scene_state.available_clips:
            clip_id = clip.path.stem
            score = self._score_clip(clip, scene_state)
            review = self._review_clip(clip, score, scene_state)
            clip_scores[clip_id] = score
            romeo_reviews[clip_id] = review
        scene_state.clip_scores = clip_scores
        scene_state.romeo_reviews = romeo_reviews

    def _score_clip(self, clip: ClipSegment, scene_state: SceneState) -> ClipScore:
        quality_score = float(clip.metadata.get("quality_score", 0.0))
        visual_quality = 5 if clip.width >= 1920 and clip.height >= 1080 else 4 if clip.width >= 1280 and clip.height >= 720 else 2
        if quality_score >= 0.9:
            visual_quality = min(5, visual_quality + 1)

        continuity_fit = 5 if 2.0 <= clip.duration <= 10.0 else 3
        prompt_match = 4 if scene_state.style_profile else 3
        motion_stability = 4 if clip.fps >= 24 else 3
        timeline_usefulness = 5 if 3.0 <= clip.duration <= 8.0 else 4 if 2.0 <= clip.duration <= 12.0 else 2

        notes: list[str] = []
        risk_flags: list[str] = []
        recommended_action = "keep"

        if visual_quality < 3:
            notes.append("clip resolution is too low for scene rough cut")
            risk_flags.append("low_visual_quality")
            recommended_action = "reject_clip"
        if timeline_usefulness < 3:
            notes.append("clip duration is poorly suited for timeline assembly")
            risk_flags.append("low_timeline_usefulness")
            recommended_action = "reject_clip"
        if clip.duration > 8.0:
            notes.append("trim intro or tail for tighter pacing")
            if recommended_action == "keep":
                recommended_action = "trim_for_pacing"
        if clip.fps < 24:
            notes.append("lower fps may reduce motion stability")
            risk_flags.append("motion_risk")

        return ClipScore(
            clip_id=clip.path.stem,
            visual_quality=visual_quality,
            continuity_fit=continuity_fit,
            prompt_match=prompt_match,
            motion_stability=motion_stability,
            timeline_usefulness=timeline_usefulness,
            notes=notes,
            risk_flags=risk_flags,
            recommended_action=recommended_action,
        )

    def _review_clip(
        self,
        clip: ClipSegment,
        score: ClipScore,
        scene_state: SceneState,
    ) -> RomeoReview:
        cinematic_value = min(5, max(1, 2 + score.visual_quality // 2 + score.timeline_usefulness // 3))
        emotional_relevance = 4 if scene_state.editing_template == "cinematic_montage" else 3
        scene_fit = score.continuity_fit
        pacing_value = 5 if 2.5 <= clip.duration <= 7.5 else 3
        final_image_strength = min(5, max(1, score.visual_quality))
        notes = "Best opening frame for scene." if cinematic_value >= 5 else "Usable support shot for scene rhythm."
        return RomeoReview(
            clip_id=clip.path.stem,
            cinematic_value=cinematic_value,
            emotional_relevance=emotional_relevance,
            scene_fit=scene_fit,
            pacing_value=pacing_value,
            final_image_strength=final_image_strength,
            notes=notes,
        )

    def _select_clips(self, scene_state: SceneState) -> None:
        ranked = []
        for clip in scene_state.available_clips:
            clip_id = clip.path.stem
            score = scene_state.clip_scores[clip_id]
            review = scene_state.romeo_reviews[clip_id]
            if not score.passes_gate:
                scene_state.rejected_clips.append(clip_id)
                continue
            ranked.append((score.total + review.total, clip_id))

        ranked.sort(reverse=True)
        desired_count = max(4, min(8, int(math.ceil(scene_state.target_duration_sec / 8.0))))
        approved = [clip_id for _, clip_id in ranked[:desired_count]]
        if not approved:
            scene_state.errors.append(
                StageError(
                    stage="scene_stitcher",
                    message="No clips passed the quality gate for rough cut assembly",
                    severity=ErrorSeverity.FATAL,
                )
            )
        scene_state.approved_clips = approved
        rejected_extra = [clip_id for _, clip_id in ranked[desired_count:]]
        scene_state.rejected_clips.extend(rejected_extra)

    def _assemble_timeline(self, scene_state: SceneState) -> None:
        clip_map = {clip.path.stem: clip for clip in scene_state.available_clips}
        selected_clips = [
            clip_map[clip_id]
            for clip_id in scene_state.approved_clips
            if clip_id in clip_map
        ]
        if not selected_clips:
            return

        target_per_clip = scene_state.target_duration_sec / len(selected_clips)
        segments: list[TimelineSegment] = []

        for index, clip in enumerate(selected_clips, start=1):
            clip_id = clip.path.stem
            clip_target = min(clip.duration, max(2.0, target_per_clip))
            segment = self.tools.trim_clip(
                clip_id,
                start_sec=0.0,
                end_sec=clip_target,
                timeline_in_sec=0.0,
                segment_id=f"seg_{index:02d}",
            )
            segment.notes.extend(scene_state.clip_scores[clip_id].notes)
            segments.append(segment)

        self.tools.concat_clips(segments, transition_type="cut")
        self._apply_template_transitions(scene_state.editing_template, segments)
        actual_duration = self._reflow_timeline(segments)

        timeline = TimelineV1(
            project_id=scene_state.project_id,
            scene_id=scene_state.scene_id,
            timeline_version=scene_state.timeline_version,
            scene_goal=scene_state.scene_goal,
            style_profile=scene_state.style_profile,
            editing_mode=scene_state.editing_mode,
            editing_template=scene_state.editing_template,
            target_duration_sec=scene_state.target_duration_sec,
            actual_duration_sec=actual_duration,
            video_segments=segments,
            used_clips=[clip.path.stem for clip in selected_clips],
            rejected_clips=scene_state.rejected_clips,
            reviews={},
            export_profile=scene_state.export_profile,
        )
        scene_state.timeline = timeline
        scene_state.reviews["romeo"] = self._build_romeo_summary(scene_state)
        scene_state.reviews["andrew"] = self._build_andrew_summary(scene_state)
        scene_state.timeline.reviews = dict(scene_state.reviews)

    def _apply_template_transitions(
        self,
        template: str,
        segments: list[TimelineSegment],
    ) -> None:
        if len(segments) < 2:
            return

        if template == "cinematic_montage" and len(segments) >= 3:
            left = segments[1]
            right = segments[2]
            transition = self.tools.add_transition(
                left.clip_id,
                right.clip_id,
                transition_type="crossfade",
                duration_sec=0.4,
            )
            left.transition_out = transition
            right.transition_in = transition
        elif template == "trailer_cut":
            for left, right in zip(segments, segments[1:]):
                transition = self.tools.add_transition(
                    left.clip_id,
                    right.clip_id,
                    transition_type="cut",
                    duration_sec=0.0,
                )
                left.transition_out = transition
                right.transition_in = transition
        elif template == "dialogue_scene":
            for left, right in zip(segments, segments[1:]):
                transition = self.tools.add_transition(
                    left.clip_id,
                    right.clip_id,
                    transition_type="cut",
                    duration_sec=0.0,
                )
                left.transition_out = transition
                right.transition_in = transition

    def _reflow_timeline(self, segments: list[TimelineSegment]) -> float:
        cursor = 0.0
        for segment in segments:
            source_duration = round(segment.source_out_sec - segment.source_in_sec, 3)
            segment.timeline_in_sec = round(cursor, 3)
            segment.timeline_out_sec = round(segment.timeline_in_sec + source_duration, 3)
            overlap = 0.0
            if segment.transition_out is not None:
                overlap = max(0.0, segment.transition_out.duration_sec)
            cursor = max(0.0, segment.timeline_out_sec - overlap)
        return round(segments[-1].timeline_out_sec, 3) if segments else 0.0

    def _attach_audio(self, scene_state: SceneState) -> None:
        if scene_state.timeline is None:
            return

        audio_segments: list[TimelineAudioSegment] = []
        if scene_state.music_track:
            music = self.tools.add_music(scene_state.music_track, gain_db=-7.0)
            music.timeline_out_sec = scene_state.timeline.actual_duration_sec
            audio_segments.append(music)
        if scene_state.voiceover_track:
            voiceover = self.tools.mix_voiceover(scene_state.voiceover_track, duck_music=True)
            voiceover.timeline_out_sec = scene_state.timeline.actual_duration_sec
            audio_segments.append(voiceover)
        scene_state.timeline.audio_segments = self.tools.normalize_audio(audio_segments, target_lufs=-14.0)

    def _attach_subtitles(self, scene_state: SceneState) -> None:
        if scene_state.timeline is None:
            return
        if not scene_state.subtitle_track:
            scene_state.timeline.subtitle_segments = []
            return
        subtitles = self.tools.generate_subtitles(scene_state.subtitle_track)
        style = self.tools.burn_subtitles("default")
        for subtitle in subtitles:
            subtitle.style_preset = style
        scene_state.timeline.subtitle_segments = subtitles

    def _export(self, scene_state: SceneState) -> None:
        if scene_state.timeline is None:
            return
        preview_path = self.tools.render_preview(scene_state)
        timeline_path = self.tools.export_scene(
            scene_state.timeline,
            scene_state.export_profile,
            scene_state.output_dir,
        )
        version_path = self.tools.save_timeline_version(
            scene_state.timeline,
            scene_state.output_dir,
            comment="Initial rough cut export",
        )
        scene_state.version_history.append(str(version_path))
        scene_state.reviews["preview_path"] = str(preview_path)
        scene_state.reviews["timeline_path"] = str(timeline_path)

    def _discover_clip_paths(self, input_folder: Path, max_clips: int) -> list[Path]:
        if not input_folder.exists():
            return []
        files = [
            path
            for path in sorted(input_folder.iterdir())
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        ]
        return files[:max_clips]

    def _build_romeo_summary(self, scene_state: SceneState) -> str:
        if not scene_state.approved_clips:
            return "No approved clips. Scene needs new source material or regeneration."
        opening = scene_state.approved_clips[0]
        closing = scene_state.approved_clips[-1]
        return (
            f"Selected {len(scene_state.approved_clips)} clips. "
            f"Opening on {opening} and closing on {closing} for scene emphasis."
        )

    def _build_andrew_summary(self, scene_state: SceneState) -> str:
        flagged = []
        for clip_id in scene_state.approved_clips:
            flagged.extend(scene_state.clip_scores[clip_id].risk_flags)
        if not flagged:
            return "Approved clips pass the technical gate for rough cut assembly."
        unique_flags = ", ".join(sorted(set(flagged)))
        return f"Approved clips are usable, but review these risks: {unique_flags}."
