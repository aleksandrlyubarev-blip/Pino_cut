"""PinoCut configuration, presets, and constants — Phase 2-3."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


# ── Supported formats ──

SUPPORTED_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".avi", ".mkv", ".webm"})
SUPPORTED_AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".ogg", ".flac", ".aac"})


# ── Render presets ──

class RenderPreset(str, Enum):
    DRAFT = "draft"
    STANDARD = "standard"
    CINEMA = "cinema"


@dataclass(frozen=True, slots=True)
class RenderSettings:
    codec: str
    preset: str
    crf: int
    audio_codec: str
    audio_bitrate: str
    fps: int
    threads: int


RENDER_PRESETS: dict[RenderPreset, RenderSettings] = {
    RenderPreset.DRAFT: RenderSettings(
        codec="libx264", preset="ultrafast", crf=28,
        audio_codec="aac", audio_bitrate="128k", fps=30, threads=4,
    ),
    RenderPreset.STANDARD: RenderSettings(
        codec="libx264", preset="medium", crf=23,
        audio_codec="aac", audio_bitrate="192k", fps=30, threads=6,
    ),
    RenderPreset.CINEMA: RenderSettings(
        codec="libx265", preset="slow", crf=18,
        audio_codec="aac", audio_bitrate="320k", fps=30, threads=8,
    ),
}


# ── Transitions ──

@dataclass(frozen=True, slots=True)
class TransitionConfig:
    histogram_threshold: float = 0.55
    crossfade_duration: float = 0.75
    dip_to_black_duration: float = 1.0


# ── Audio ducking ──

@dataclass(frozen=True, slots=True)
class AudioDuckingConfig:
    speech_volume: float = 0.12
    no_speech_volume: float = 0.32
    ramp_duration: float = 0.2
    whisper_model: str = "tiny"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    energy_vad_threshold: float = 0.015


# ── Color grading ──

class ColorGradeStyle(str, Enum):
    NONE = "none"
    WARM = "warm"
    COLD = "cold"
    VINTAGE = "vintage"
    CINEMATIC = "cinematic"


# ── Titles ──

@dataclass(frozen=True, slots=True)
class TitleConfig:
    font: str = "Arial-Bold"
    font_size: int = 52
    color: str = "white"
    stroke_color: str = "black"
    stroke_width: int = 3
    position: str = "bottom"
    duration: float = 4.0
    fade_in: float = 0.5
    margin_bottom: int = 60


# ── Watermark ──

@dataclass(frozen=True, slots=True)
class WatermarkConfig:
    text: str = "Romeo Flex Vision"
    font_size: int = 28
    opacity: float = 0.4
    position: str = "bottom-right"
    margin: int = 30
    image_path: Path | None = None


# ── Project config (top-level) ──

@dataclass(slots=True)
class ProjectConfig:
    """Top-level project configuration."""

    # Input
    input_folder: Path = field(default_factory=lambda: Path("./raw_footage"))
    max_clips: int = 30
    min_duration: float = 1.0
    max_duration: float | None = None

    # Output
    output_path: Path = field(default_factory=lambda: Path("./output/final.mp4"))
    render_preset: RenderPreset = RenderPreset.STANDARD
    render_mode: str = "moviepy"  # moviepy | ffmpeg   (Phase 3)

    # Background music
    bg_music_path: Path | None = None

    # Feature configs
    transitions: TransitionConfig = field(default_factory=TransitionConfig)
    audio_ducking: AudioDuckingConfig = field(default_factory=AudioDuckingConfig)
    title: TitleConfig = field(default_factory=TitleConfig)
    watermark: WatermarkConfig = field(default_factory=WatermarkConfig)
    color_grade: ColorGradeStyle = ColorGradeStyle.NONE

    # Execution
    structured_logs: bool = False
    sandbox: str = "local"  # local | e2b
    parallel: bool = True  # enable Moltis parallel analysis
    memory_persist: bool = False  # persist analysis cache between runs

    # Metrics
    metrics_enabled: bool = True
    metrics_path: Path | None = None  # JSON file for metrics history
