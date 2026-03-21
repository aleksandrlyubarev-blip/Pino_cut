"""Scene-level planning and export for Pinnocat Scene Stitcher v1."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

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
        self._prepare_approved_clips(scene_state)
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
        scene_state.reviews = {
            key: value
            for key, value in scene_state.reviews.items()
            if not key.startswith("andrew:")
        }
        for clip in scene_state.available_clips:
            clip_id = clip.path.stem
            score = self._score_clip(clip, scene_state)
            review = self._review_clip(clip, score, scene_state)
            clip_scores[clip_id] = score
            romeo_reviews[clip_id] = review
            scene_state.reviews[f"andrew:{clip_id}"] = self.generate_quality_report(clip_id, score)
        scene_state.clip_scores = clip_scores
        scene_state.romeo_reviews = romeo_reviews

    def _score_clip(self, clip: ClipSegment, scene_state: SceneState) -> ClipScore:
        rubric = self.score_clip_full_rubric(clip, scene_state.scene_goal)
        flaws = self.detect_technical_flaws(clip)
        notes: list[str] = []
        risk_flags: list[str] = list(flaws)
        recommended_action = "keep"

        if rubric["visual_quality"] < 3:
            notes.append("clip resolution is too low for scene rough cut")
            risk_flags.append("low_visual_quality")
            recommended_action = "reject_clip"
        if rubric["timeline_usefulness"] < 3:
            notes.append("clip duration is poorly suited for timeline assembly")
            risk_flags.append("low_timeline_usefulness")
            recommended_action = "reject_clip"
        if clip.duration > 8.0:
            notes.append("trim intro or tail for tighter pacing")
            if recommended_action == "keep":
                recommended_action = "trim_for_pacing"
        if rubric["prompt_match"] <= 2:
            notes.append("semantic match to the scene goal is weak")
            if recommended_action == "keep":
                recommended_action = "review_scene_fit"
        if rubric["motion_stability"] <= 2:
            notes.append("motion instability may reduce shot usability")
            risk_flags.append("motion_risk")
            if recommended_action == "keep":
                recommended_action = "request_restyle"
        if "unstable_lighting" in flaws:
            notes.append("lighting changes may require visual cleanup")
        if "excessive_camera_shake" in flaws:
            notes.append("camera shake may require stabilization or restyling")

        return ClipScore(
            clip_id=clip.path.stem,
            visual_quality=rubric["visual_quality"],
            continuity_fit=rubric["continuity_fit"],
            prompt_match=rubric["prompt_match"],
            motion_stability=rubric["motion_stability"],
            timeline_usefulness=rubric["timeline_usefulness"],
            notes=notes,
            risk_flags=sorted(set(risk_flags)),
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
        scored_clips = [
            (clip, scene_state.clip_scores[clip.path.stem])
            for clip in scene_state.available_clips
        ]
        gated_approved, gated_rejected = self.apply_rejection_policy(scored_clips)
        ranked = []
        for clip in gated_approved:
            clip_id = clip.path.stem
            score = scene_state.clip_scores[clip_id]
            review = scene_state.romeo_reviews[clip_id]
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
        scene_state.rejected_clips = [clip.path.stem for clip in gated_rejected]
        rejected_extra = [clip_id for _, clip_id in ranked[desired_count:]]
        scene_state.rejected_clips.extend(rejected_extra)

    def _prepare_approved_clips(self, scene_state: SceneState) -> None:
        clip_map = {clip.path.stem: clip for clip in scene_state.available_clips}
        selected_clips = [
            clip_map[clip_id]
            for clip_id in scene_state.approved_clips
            if clip_id in clip_map
        ]
        if not selected_clips:
            scene_state.bridge_jobs = []
            scene_state.regeneration_jobs = []
            return

        target_per_clip = scene_state.target_duration_sec / len(selected_clips)
        projected_duration = 0.0
        scene_state.bridge_jobs = []
        scene_state.regeneration_jobs = []

        for clip in selected_clips:
            reframing = self.calculate_dynamic_reframing(clip, scene_state.editing_template)
            self.prepare_clip_for_assembly(clip, reframing)
            projected_duration += min(clip.duration, max(2.0, target_per_clip))

            if scene_state.editing_mode == "manual":
                continue

            if clip.duration < max(2.0, target_per_clip) * 0.75:
                scene_state.regeneration_jobs.append(
                    self.extend_or_restyle_clip(
                        clip,
                        mode="extend",
                        prompt=(
                            f"Extend clip {clip.path.stem} to better support "
                            f"{scene_state.scene_goal}."
                        ),
                    )
                )

            flaws = self.detect_technical_flaws(clip)
            if "excessive_camera_shake" in flaws or "unstable_lighting" in flaws:
                scene_state.regeneration_jobs.append(
                    self.extend_or_restyle_clip(
                        clip,
                        mode="restyle",
                        prompt=(
                            f"Restyle clip {clip.path.stem} for {scene_state.style_profile} "
                            f"while preserving scene goal: {scene_state.scene_goal}."
                        ),
                    )
                )

        if (
            scene_state.editing_mode != "manual"
            and projected_duration < scene_state.target_duration_sec * 0.85
        ):
            scene_state.bridge_jobs.append(
                self.create_bridge_shot(
                    f"{scene_state.scene_goal} | {scene_state.style_profile} | "
                    f"{scene_state.editing_template}"
                )
            )

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
            segment = self.smart_trim_clip(
                clip,
                target_sec=clip_target,
                segment_id=f"seg_{index:02d}",
            )
            segment.notes.extend(scene_state.clip_scores[clip_id].notes)
            if clip.metadata.get("bassito_prepared"):
                segment.notes.append("bassito_prepared")
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
        scene_state.reviews["bassito"] = self._build_bassito_summary(scene_state)
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

    def _build_bassito_summary(self, scene_state: SceneState) -> str:
        prepared = 0
        clip_map = {clip.path.stem: clip for clip in scene_state.available_clips}
        for clip_id in scene_state.approved_clips:
            clip = clip_map.get(clip_id)
            if clip and clip.metadata.get("bassito_prepared"):
                prepared += 1
        return (
            f"Prepared {prepared} approved clips, queued {len(scene_state.bridge_jobs)} bridge jobs "
            f"and {len(scene_state.regeneration_jobs)} regeneration jobs."
        )

    def score_clip_full_rubric(self, clip: ClipSegment, scene_goal: str) -> dict[str, int]:
        """Andrew's 5-dimension rubric using deterministic metadata heuristics."""
        quality_score = float(clip.metadata.get("quality_score", 0.5))
        brightness_variance = float(clip.metadata.get("brightness_variance", 0.0))
        shake_score = float(clip.metadata.get("shake_score", 0.0))

        if clip.width >= 1920 and clip.height >= 1080:
            visual_quality = 5
        elif clip.width >= 1280 and clip.height >= 720:
            visual_quality = 4
        elif clip.width >= 854 and clip.height >= 480:
            visual_quality = 3
        else:
            visual_quality = 2
        if quality_score >= 0.9:
            visual_quality = min(5, visual_quality + 1)
        elif quality_score < 0.4:
            visual_quality = max(1, visual_quality - 1)
        if brightness_variance > 40:
            visual_quality = max(1, visual_quality - 1)

        semantic_overlap = self._calculate_scene_overlap(clip, scene_goal)
        continuity_fit = 5 if semantic_overlap >= 2 else 4 if semantic_overlap == 1 else 3
        prompt_match = 5 if semantic_overlap >= 2 else 4 if semantic_overlap == 1 else 3
        if clip.metadata.get("scene_description") or clip.metadata.get("location_tag"):
            continuity_fit = min(5, continuity_fit + 1)
        continuity_fit = min(5, continuity_fit)

        if shake_score <= 0.05:
            motion_stability = 5
        elif shake_score <= 0.15:
            motion_stability = 4
        elif shake_score <= 0.3:
            motion_stability = 3
        elif shake_score <= 0.45:
            motion_stability = 2
        else:
            motion_stability = 1
        if clip.fps < 24:
            motion_stability = max(1, motion_stability - 1)

        if 3.0 <= clip.duration <= 8.0:
            timeline_usefulness = 5
        elif 2.0 <= clip.duration <= 12.0:
            timeline_usefulness = 4
        elif 1.5 <= clip.duration <= 15.0:
            timeline_usefulness = 3
        else:
            timeline_usefulness = 2

        return {
            "visual_quality": visual_quality,
            "continuity_fit": continuity_fit,
            "prompt_match": prompt_match,
            "motion_stability": motion_stability,
            "timeline_usefulness": timeline_usefulness,
        }

    def detect_technical_flaws(self, clip: ClipSegment) -> list[str]:
        """Return deterministic technical flaw tags for Andrew's report."""
        flaws: list[str] = []
        if float(clip.metadata.get("shake_score", 0.0)) > 0.3:
            flaws.append("excessive_camera_shake")
        if float(clip.metadata.get("brightness_variance", 0.0)) > 40:
            flaws.append("unstable_lighting")
        return flaws

    def apply_rejection_policy(
        self,
        scored_clips: list[tuple[ClipSegment, ClipScore]],
    ) -> tuple[list[ClipSegment], list[ClipSegment]]:
        """Apply Andrew's hard gates before Romeo ranks the remaining takes."""
        approved: list[ClipSegment] = []
        rejected: list[ClipSegment] = []
        for clip, score in scored_clips:
            if score.visual_quality < 3 or score.timeline_usefulness < 3:
                rejected.append(clip)
            else:
                approved.append(clip)
        return approved, rejected

    def generate_quality_report(self, clip_id: str, score: ClipScore) -> str:
        """Create Andrew's written review line for timeline and scene outputs."""
        average = round(score.total / 5.0, 1)
        strengths: list[str] = []
        if score.continuity_fit >= 4:
            strengths.append("strong continuity")
        if score.prompt_match >= 4:
            strengths.append("good scene alignment")
        if score.timeline_usefulness >= 4:
            strengths.append("useful timeline fit")
        if not strengths:
            strengths.append("usable support coverage")

        concerns: list[str] = []
        if score.motion_stability <= 3:
            concerns.append("minor motion issues")
        if "unstable_lighting" in score.risk_flags:
            concerns.append("lighting instability")
        if "excessive_camera_shake" in score.risk_flags:
            concerns.append("camera shake")

        concern_text = ", ".join(concerns) if concerns else "no major technical risks"
        return (
            f"Clip {clip_id}: {average}/5 - "
            f"{', '.join(strengths)}; {concern_text}."
        )

    def smart_trim_clip(
        self,
        clip: ClipSegment,
        *,
        target_sec: float,
        segment_id: str,
    ) -> TimelineSegment:
        """Choose a deterministic trim window using speech/action metadata when present."""
        clip_id = clip.path.stem
        duration = min(clip.duration, max(0.0, target_sec))
        start_sec = 0.0

        if clip.speech_segments:
            speech_start = clip.speech_segments[0][0]
            start_sec = max(0.0, min(speech_start, max(0.0, clip.duration - duration)))
        elif "action_peak_sec" in clip.metadata:
            action_peak = float(clip.metadata["action_peak_sec"])
            centered_start = action_peak - (duration / 2.0)
            start_sec = max(0.0, min(centered_start, max(0.0, clip.duration - duration)))

        end_sec = min(clip.duration, start_sec + duration)
        return self.tools.trim_clip(
            clip_id,
            start_sec=round(start_sec, 3),
            end_sec=round(end_sec, 3),
            timeline_in_sec=0.0,
            segment_id=segment_id,
        )

    def calculate_dynamic_reframing(self, clip: ClipSegment, template: str) -> dict[str, object]:
        """Create a simple Bassito reframing plan for the approved clip."""
        widescreen = clip.width >= clip.height
        return {
            "ken_burns": template == "cinematic_montage",
            "start_zoom": 1.0,
            "end_zoom": 1.15 if template == "cinematic_montage" else 1.05,
            "pan_direction": "left_to_right" if template == "cinematic_montage" and widescreen else "static",
            "safe_crop": "rule_of_thirds",
        }

    def create_bridge_shot(self, scene_context: str) -> dict:
        """Queue a bridge-shot request through the bounded Bassito tool API."""
        return self.tools.request_bridge_shot(prompt=scene_context)

    def extend_or_restyle_clip(
        self,
        clip: ClipSegment,
        mode: Literal["extend", "restyle"],
        prompt: str,
    ) -> dict:
        """Map directly to the async regeneration tools in SceneToolbox."""
        if mode == "extend":
            return self.tools.request_extend(clip.path.stem, prompt)
        return self.tools.request_restyle(clip.path.stem, prompt)

    def prepare_clip_for_assembly(
        self,
        clip: ClipSegment,
        reframing: dict[str, object],
    ) -> ClipSegment:
        """Annotate the clip with Bassito preparation metadata before assembly."""
        clip.metadata["reframing"] = reframing
        clip.metadata["bassito_prepared"] = True
        return clip

    def _calculate_scene_overlap(self, clip: ClipSegment, scene_goal: str) -> int:
        goal_tokens = set(re.findall(r"[a-z0-9]+", scene_goal.lower()))
        semantic_parts: list[str] = [clip.path.stem.replace("_", " ")]
        for key in ("scene_description", "location_tag", "mood"):
            value = clip.metadata.get(key)
            if value:
                semantic_parts.append(str(value))
        subjects = clip.metadata.get("subjects", [])
        if isinstance(subjects, list):
            semantic_parts.extend(str(subject) for subject in subjects)
        semantic_text = " ".join(semantic_parts).lower()
        clip_tokens = set(re.findall(r"[a-z0-9]+", semantic_text))
        return len(goal_tokens & clip_tokens)
