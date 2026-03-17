"""PinoCut pipeline state and data models.

All dataclasses that flow through the LangGraph StateGraph.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypedDict

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
    ken_burns_clips: list[str]
    color_grade: ColorGradeStyle
    audio_mix: AudioMixConfig
    output_path: str | None
    render_preset: str
    errors: list[StageError]
