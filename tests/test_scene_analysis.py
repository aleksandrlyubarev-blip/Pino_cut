"""Tests for content-based clip analysis (G3.1) using synthetic OpenCV videos."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from pinocut.scene_analysis import (  # noqa: E402
    SceneClipAnalyzer,
    energy_vad,
)
from pinocut.scene_stitcher import SceneStitcherAgent  # noqa: E402
from pinocut.state import ClipSegment  # noqa: E402

FPS = 24
SIZE = (320, 240)  # (width, height)


def write_video(path: Path, frames: list[np.ndarray]) -> Path:
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, SIZE)
    if not writer.isOpened():
        pytest.skip("cv2.VideoWriter cannot encode mp4v in this environment")
    for frame in frames:
        writer.write(frame)
    writer.release()
    return path


def textured_frame(offset: tuple[int, int] = (0, 0), brightness: int = 128) -> np.ndarray:
    """Detailed frame (checkerboard + rectangles) so sharpness/flow are measurable."""
    frame = np.full((SIZE[1], SIZE[0], 3), brightness, np.uint8)
    step = 20
    for y in range(0, SIZE[1], step):
        for x in range(0, SIZE[0], step):
            if (x // step + y // step) % 2 == 0:
                cv2.rectangle(
                    frame,
                    (x + offset[0], y + offset[1]),
                    (x + step + offset[0], y + step + offset[1]),
                    (60, 60, 60),
                    -1,
                )
    cv2.circle(frame, (160 + offset[0], 120 + offset[1]), 30, (220, 220, 220), -1)
    return frame


@pytest.fixture(scope="module")
def videos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("synthetic")
    rng = np.random.default_rng(42)
    out: dict[str, Path] = {}

    out["static"] = write_video(
        root / "static.mp4", [textured_frame() for _ in range(48)]
    )

    shaky_frames = []
    for _ in range(48):
        jitter = (int(rng.integers(-8, 9)), int(rng.integers(-8, 9)))
        shaky_frames.append(textured_frame(offset=jitter))
    out["shaky"] = write_video(root / "shaky.mp4", shaky_frames)

    out["pan"] = write_video(
        root / "pan.mp4", [textured_frame(offset=(i * 3, 0)) for i in range(48)]
    )

    out["flicker"] = write_video(
        root / "flicker.mp4",
        [
            np.full((SIZE[1], SIZE[0], 3), 30 if i % 8 < 4 else 220, np.uint8)
            for i in range(48)
        ],
    )

    # Static except for a motion burst in the middle third (~1.0-1.3 s).
    burst = []
    for i in range(72):
        offset = (int(rng.integers(-10, 11)), 0) if 24 <= i <= 32 else (0, 0)
        burst.append(textured_frame(offset=offset))
    out["burst"] = write_video(root / "burst.mp4", burst)

    return out


@pytest.fixture()
def analyzer() -> SceneClipAnalyzer:
    return SceneClipAnalyzer(samples=16)


def test_static_clip_is_stable(analyzer: SceneClipAnalyzer, videos: dict[str, Path]) -> None:
    metrics = analyzer.analyze(videos["static"])
    assert metrics is not None
    assert metrics.shake_score <= 0.05
    assert metrics.brightness_variance < 5
    assert metrics.quality_score > 0.5  # detailed checkerboard is sharp
    assert metrics.action_peak_sec is None  # nothing moves


def test_shaky_clip_scores_high_shake(analyzer: SceneClipAnalyzer, videos: dict[str, Path]) -> None:
    static = analyzer.analyze(videos["static"])
    shaky = analyzer.analyze(videos["shaky"])
    assert shaky.shake_score > 0.3
    assert shaky.shake_score > static.shake_score + 0.2


def test_smooth_pan_is_not_shake(analyzer: SceneClipAnalyzer, videos: dict[str, Path]) -> None:
    pan = analyzer.analyze(videos["pan"])
    shaky = analyzer.analyze(videos["shaky"])
    # Consistent motion vector => low jitter even though the frame moves.
    assert pan.shake_score < shaky.shake_score - 0.2


def test_flicker_raises_brightness_variance(
    analyzer: SceneClipAnalyzer, videos: dict[str, Path]
) -> None:
    metrics = analyzer.analyze(videos["flicker"])
    assert metrics.brightness_variance > 40  # rubric flags unstable lighting


def test_action_peak_lands_on_motion_burst(
    analyzer: SceneClipAnalyzer, videos: dict[str, Path]
) -> None:
    metrics = analyzer.analyze(videos["burst"])
    assert metrics.action_peak_sec is not None
    assert 0.8 <= metrics.action_peak_sec <= 1.6  # burst spans ~1.0-1.33 s


def test_unreadable_file_returns_none(analyzer: SceneClipAnalyzer, tmp_path: Path) -> None:
    missing = tmp_path / "missing.mp4"
    assert analyzer.analyze(missing) is None

    garbage = tmp_path / "garbage.mp4"
    garbage.write_bytes(b"not a video")
    assert analyzer.analyze(garbage) is None


def test_annotate_clip_fills_metadata(analyzer: SceneClipAnalyzer, videos: dict[str, Path]) -> None:
    clip = ClipSegment(
        path=videos["shaky"], duration=2.0, has_audio=False,
        resolution=SIZE, fps=float(FPS), metadata={},
    )
    assert analyzer.annotate_clip(clip) is True
    for key in ("quality_score", "brightness_variance", "shake_score", "sharpness"):
        assert key in clip.metadata
    assert clip.metadata["content_analysis"] == "opencv-v1"


def test_annotate_clip_never_clobbers_existing(
    analyzer: SceneClipAnalyzer, videos: dict[str, Path]
) -> None:
    clip = ClipSegment(
        path=videos["shaky"], duration=2.0, has_audio=False,
        resolution=SIZE, fps=float(FPS),
        metadata={"quality_score": 0.99, "mood": "tense"},
    )
    analyzer.annotate_clip(clip)
    assert clip.metadata["quality_score"] == 0.99  # upstream value preserved
    assert clip.metadata["mood"] == "tense"
    assert "shake_score" in clip.metadata  # missing keys still filled


def test_annotate_unreadable_clip_is_noop(analyzer: SceneClipAnalyzer, tmp_path: Path) -> None:
    clip = ClipSegment(
        path=tmp_path / "ghost.mp4", duration=2.0, has_audio=False,
        resolution=SIZE, fps=float(FPS), metadata={},
    )
    assert analyzer.annotate_clip(clip) is False
    assert clip.metadata == {}


def test_energy_vad_detects_loud_segment() -> None:
    sr = 16000
    quiet = np.zeros(sr, dtype=np.float64)
    loud = 0.5 * np.sin(2 * np.pi * 220 * np.linspace(0, 1, sr))
    segments = energy_vad(np.concatenate([quiet, loud, quiet]), 0.015, sr=sr)
    assert len(segments) == 1
    start, end = segments[0]
    assert start == pytest.approx(1.0, abs=0.5)
    assert end == pytest.approx(2.0, abs=0.5)


def test_energy_vad_silence_yields_nothing() -> None:
    assert energy_vad(np.zeros(32000, dtype=np.float64), 0.015) == []


def test_opencv_probe_fallback(videos: dict[str, Path], tmp_path: Path) -> None:
    from pinocut.scene_tools import SceneToolbox

    toolbox = SceneToolbox()
    probed = toolbox._probe_with_opencv(videos["static"])
    assert probed is not None
    duration, has_audio, resolution, fps = probed
    assert duration == pytest.approx(2.0, abs=0.1)  # 48 frames @ 24 fps
    assert has_audio is False  # cv2 cannot see audio streams
    assert resolution == SIZE
    assert fps == pytest.approx(FPS)
    assert toolbox._probe_with_opencv(tmp_path / "missing.mp4") is None


def test_scene_pipeline_uses_real_metrics(videos: dict[str, Path], tmp_path: Path) -> None:
    """End-to-end: Andrew's rubric sees analyzer output, not defaults."""
    agent = SceneStitcherAgent()

    def make_clip(name: str) -> ClipSegment:
        return ClipSegment(
            path=videos[name], duration=2.0, has_audio=False,
            resolution=SIZE, fps=float(FPS), metadata={},
        )

    from pinocut.state import SceneState

    scene_state = SceneState(
        project_id="proj",
        scene_id="scene_metrics",
        scene_goal="stability test",
        style_profile="cinematic",
        target_duration_sec=6.0,
        available_clips=[make_clip("static"), make_clip("shaky")],
        output_dir=str(tmp_path),
    )
    agent.build_scene(scene_state)

    static_score = scene_state.clip_scores["static"]
    shaky_score = scene_state.clip_scores["shaky"]
    assert static_score.motion_stability == 5
    assert shaky_score.motion_stability <= 3
    assert static_score.motion_stability > shaky_score.motion_stability
