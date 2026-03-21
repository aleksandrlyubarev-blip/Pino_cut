"""PinoCut pipeline state and data models.

All dataclasses that flow through the LangGraph StateGraph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TypedDict

from pinocut.config import ColorGradeStyle, ProjectConfig
from pinocut.utils.errors import StageError


# ── Clip data ──

@dataclass(slots=True)
class ClipSegment:
    """A validated video clip with its properties."""

    path: Path
    duration: float
    has_audio: bool
    resolution: tuple[int, int]  # (width, height)
    fps: float
    metadata: dict = field(default_factory=dict)
    speech_segments: list[tuple[float, float]] = field(default_factory=list)

    @property
    def has_speech(self) -> bool:
        return len(self.speech_segments) > 0

    @property
    def width(self) -> int:
        return self.resolution[0]

    @property
    def height(self) -> int:
        return self.resolution[1]


# ── Analysis data ──

@dataclass(slots=True)
class ClipAnalysis:
    """Analysis results for a single clip."""

    clip_path: str = ""
    speech_segments: list[tuple[float, float]] = field(default_factory=list)
    first_frame_hist: list[float] | None = None
    last_frame_hist: list[float] | None = None
    avg_brightness: float = 0.0
    dominant_colors: list[tuple[int, int, int]] = field(default_factory=list)


@dataclass(slots=True)
class RomeoVisionData:
    """Semantic metadata from Romeo Flex Vision API."""

    scene_description: str = ""
    location_tag: str = ""
    mood: str = ""
    subjects: list[str] = field(default_factory=list)
    suggested_duration: float | None = None
    quality_score: float = 0.5


# ── Edit decisions ──

class TransitionType(str, Enum):
    HARD_CUT = "hard_cut"
    CROSSFADE = "crossfade"
    DIP_TO_BLACK = "dip_to_black"
    MATCH_CUT = "match_cut"


@dataclass(frozen=True, slots=True)
class EditDecision:
    """Transition decision at a junction between two clips."""

    from_clip: str
    to_clip: str
    transition: TransitionType
    duration: float = 0.0
    reason: str = ""


# ── Title overlays ──

@dataclass(slots=True)
class TitleOverlay:
    """A title/text overlay applied to a clip."""

    clip_path: str
    text: str
    start_time: float = 0.0
    duration: float = 4.0
    position: str = "bottom"
    animated: bool = True
    style: str = "lower_third"  # lower_third | full_screen | caption


# ── Audio mix ──

@dataclass(slots=True)
class AudioTrack:
    path: str
    role: str  # music | sfx | voiceover
    volume: float = 1.0
    start_offset: float = 0.0
    loop: bool = False


@dataclass(slots=True)
class AudioMixConfig:
    tracks: list[AudioTrack] = field(default_factory=list)
    ducking_regions: list[dict] = field(default_factory=list)
    master_volume: float = 1.0


# ── Pipeline state (LangGraph) ──

class PinoCutState(TypedDict, total=False):
    """State passed between all LangGraph nodes."""

    project_config: ProjectConfig
    clips: list[ClipSegment]
    analysis: dict[str, ClipAnalysis]
    romeo_data: dict[str, RomeoVisionData]
    edit_decisions: list[EditDecision]
    title_overlays: list[TitleOverlay]
    color_grade: ColorGradeStyle
    audio_mix: AudioMixConfig
    output_path: str | None
    render_preset: str
    errors: list[StageError]


# -- Scene Stitcher v1 models --

class SceneAction(str, Enum):
    LOAD_SCENE = "LOAD_SCENE"
    ANALYZE_CLIPS = "ANALYZE_CLIPS"
    SCORE_CLIPS = "SCORE_CLIPS"
    SELECT_TAKES = "SELECT_TAKES"
    TRIM_SEQUENCE = "TRIM_SEQUENCE"
    BUILD_ROUGH_CUT = "BUILD_ROUGH_CUT"
    ADD_TRANSITIONS = "ADD_TRANSITIONS"
    ADD_AUDIO = "ADD_AUDIO"
    ADD_SUBTITLES = "ADD_SUBTITLES"
    REQUEST_BRIDGE_SHOT = "REQUEST_BRIDGE_SHOT"
    REQUEST_REGENERATION = "REQUEST_REGENERATION"
    EXPORT_SCENE = "EXPORT_SCENE"
    SAVE_VERSION = "SAVE_VERSION"


@dataclass(slots=True)
class ExportProfile:
    resolution: str = "1920x1080"
    fps: int = 24
    format: str = "mp4"


@dataclass(slots=True)
class ClipScore:
    clip_id: str
    visual_quality: int
    continuity_fit: int
    prompt_match: int
    motion_stability: int
    timeline_usefulness: int
    notes: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    recommended_action: str = "keep"

    @property
    def passes_gate(self) -> bool:
        return self.visual_quality >= 3 and self.timeline_usefulness >= 3

    @property
    def total(self) -> int:
        return (
            self.visual_quality
            + self.continuity_fit
            + self.prompt_match
            + self.motion_stability
            + self.timeline_usefulness
        )


@dataclass(slots=True)
class RomeoReview:
    clip_id: str
    cinematic_value: int
    emotional_relevance: int
    scene_fit: int
    pacing_value: int
    final_image_strength: int
    notes: str = ""

    @property
    def total(self) -> int:
        return (
            self.cinematic_value
            + self.emotional_relevance
            + self.scene_fit
            + self.pacing_value
            + self.final_image_strength
        )


@dataclass(slots=True)
class TransitionSpec:
    type: str = "cut"
    duration_sec: float = 0.0


@dataclass(slots=True)
class TimelineSegment:
    segment_id: str
    clip_id: str
    source_in_sec: float
    source_out_sec: float
    timeline_in_sec: float
    timeline_out_sec: float
    transition_in: TransitionSpec | None = None
    transition_out: TransitionSpec | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TimelineAudioSegment:
    track_id: str
    role: str
    timeline_in_sec: float
    timeline_out_sec: float | None = None
    gain_db: float = 0.0
    duck_music: bool = False


@dataclass(slots=True)
class SubtitleSegment:
    text: str
    start_sec: float
    end_sec: float
    style_preset: str = "default"


@dataclass(slots=True)
class TimelineV1:
    project_id: str
    scene_id: str
    timeline_version: int
    scene_goal: str
    style_profile: str
    editing_mode: str
    editing_template: str
    target_duration_sec: float
    actual_duration_sec: float = 0.0
    video_segments: list[TimelineSegment] = field(default_factory=list)
    audio_segments: list[TimelineAudioSegment] = field(default_factory=list)
    subtitle_segments: list[SubtitleSegment] = field(default_factory=list)
    used_clips: list[str] = field(default_factory=list)
    rejected_clips: list[str] = field(default_factory=list)
    reviews: dict[str, str] = field(default_factory=dict)
    export_profile: ExportProfile = field(default_factory=ExportProfile)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "scene_id": self.scene_id,
            "timeline_version": self.timeline_version,
            "scene_goal": self.scene_goal,
            "style_profile": self.style_profile,
            "editing_mode": self.editing_mode,
            "editing_template": self.editing_template,
            "target_duration_sec": self.target_duration_sec,
            "actual_duration_sec": self.actual_duration_sec,
            "tracks": {
                "video": [_dataclass_to_dict(segment) for segment in self.video_segments],
                "audio": [_dataclass_to_dict(segment) for segment in self.audio_segments],
                "subtitles": [_dataclass_to_dict(segment) for segment in self.subtitle_segments],
            },
            "used_clips": self.used_clips,
            "rejected_clips": self.rejected_clips,
            "reviews": self.reviews,
            "export_profile": _dataclass_to_dict(self.export_profile),
        }


@dataclass(slots=True)
class SceneState:
    project_id: str
    scene_id: str
    scene_goal: str
    style_profile: str
    target_duration_sec: float
    editing_mode: str = "hybrid"
    editing_template: str = "cinematic_montage"
    available_clips: list[ClipSegment] = field(default_factory=list)
    approved_clips: list[str] = field(default_factory=list)
    rejected_clips: list[str] = field(default_factory=list)
    clip_scores: dict[str, ClipScore] = field(default_factory=dict)
    romeo_reviews: dict[str, RomeoReview] = field(default_factory=dict)
    timeline_version: int = 1
    music_track: str | None = None
    voiceover_track: str | None = None
    subtitle_track: str | None = None
    export_profile: ExportProfile = field(default_factory=ExportProfile)
    output_dir: str = "./output"
    timeline: TimelineV1 | None = None
    reviews: dict[str, str] = field(default_factory=dict)
    version_history: list[str] = field(default_factory=list)
    errors: list[StageError] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "scene_id": self.scene_id,
            "scene_goal": self.scene_goal,
            "style_profile": self.style_profile,
            "target_duration_sec": self.target_duration_sec,
            "editing_mode": self.editing_mode,
            "editing_template": self.editing_template,
            "available_clips": [clip.path.name for clip in self.available_clips],
            "approved_clips": self.approved_clips,
            "rejected_clips": self.rejected_clips,
            "clip_scores": {key: _dataclass_to_dict(value) for key, value in self.clip_scores.items()},
            "romeo_reviews": {key: _dataclass_to_dict(value) for key, value in self.romeo_reviews.items()},
            "timeline_version": self.timeline_version,
            "music_track": self.music_track,
            "voiceover_track": self.voiceover_track,
            "subtitle_track": self.subtitle_track,
            "export_profile": _dataclass_to_dict(self.export_profile),
            "output_dir": self.output_dir,
            "timeline": self.timeline.to_dict() if self.timeline else None,
            "reviews": self.reviews,
            "version_history": self.version_history,
            "errors": [str(error) for error in self.errors],
        }


def _dataclass_to_dict(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_dataclass_to_dict(item) for item in value]
    if isinstance(value, dict):
        return {key: _dataclass_to_dict(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {
            key: _dataclass_to_dict(getattr(value, key))
            for key in value.__dataclass_fields__
        }
    return value
