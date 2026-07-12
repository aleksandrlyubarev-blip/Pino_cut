"""Tests for SceneClipAnalyzer: real visual/audio signals for Andrew's rubric."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pinocut.scene_analysis import ANALYSIS_VERSION, SceneClipAnalyzer
from pinocut.state import ClipSegment

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")


def generate_clip(path: Path, *, video_filter: str = "", audio: str = "sine=frequency=440") -> None:
    source = "testsrc2=duration=3:size=640x360:rate=24"
    vf = video_filter or "null"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", source,
            "-f", "lavfi", "-t", "3", "-i", audio,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest",
            "-y", str(path),
        ],
        check=True,
        timeout=120,
    )


def make_segment(path: Path, *, duration: float = 3.0, has_audio: bool = True) -> ClipSegment:
    return ClipSegment(
        path=path,
        duration=duration,
        has_audio=has_audio,
        resolution=(640, 360),
        fps=24.0,
        metadata={},
    )


def test_missing_file_leaves_clip_untouched(tmp_path: Path) -> None:
    clip = make_segment(tmp_path / "ghost.mp4")
    assert SceneClipAnalyzer().analyze(clip) is False
    assert clip.metadata == {}


def test_already_analyzed_clip_is_skipped(tmp_path: Path) -> None:
    clip = make_segment(tmp_path / "ghost.mp4")
    clip.metadata["analysis_version"] = ANALYSIS_VERSION
    assert SceneClipAnalyzer().analyze(clip) is True


@requires_ffmpeg
def test_analyze_fills_rubric_signals(tmp_path: Path) -> None:
    clip_path = tmp_path / "steady.mp4"
    generate_clip(clip_path)
    clip = make_segment(clip_path)

    assert SceneClipAnalyzer().analyze(clip) is True

    assert 0.0 < clip.metadata["quality_score"] <= 1.0
    assert clip.metadata["brightness_variance"] >= 0.0
    assert 0.0 <= clip.metadata["shake_score"] <= 1.0
    assert clip.metadata["analysis_version"] == ANALYSIS_VERSION
    if "action_peak_sec" in clip.metadata:
        assert 0.0 <= clip.metadata["action_peak_sec"] <= clip.duration
    # A constant tone is non-silent for the whole clip.
    assert clip.speech_segments
    start, end = clip.speech_segments[0]
    assert start < 0.5 and end > 2.0


@requires_ffmpeg
def test_silent_clip_has_no_speech_segments(tmp_path: Path) -> None:
    clip_path = tmp_path / "silent.mp4"
    generate_clip(clip_path, audio="anullsrc=r=48000:cl=stereo")
    clip = make_segment(clip_path)

    assert SceneClipAnalyzer().analyze(clip) is True
    assert clip.speech_segments == []


@requires_ffmpeg
def test_shaky_clip_scores_higher_shake_than_steady(tmp_path: Path) -> None:
    steady_path = tmp_path / "steady.mp4"
    shaky_path = tmp_path / "shaky.mp4"
    generate_clip(steady_path)
    generate_clip(
        shaky_path,
        video_filter=(
            "crop=w=iw-80:h=ih-80"
            ":x='40+35*random(1)':y='40+35*random(1)'"
        ),
    )
    steady = make_segment(steady_path)
    shaky = make_segment(shaky_path)

    analyzer = SceneClipAnalyzer()
    assert analyzer.analyze(steady) is True
    assert analyzer.analyze(shaky) is True

    assert shaky.metadata["shake_score"] > steady.metadata["shake_score"]
