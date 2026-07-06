"""Tests for SceneRenderService: filter graph building and real ffmpeg render."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from pinocut.render_service import RenderSettings, SceneRenderService
from pinocut.scene_stitcher import SceneBuildRequest, SceneStitcherAgent
from pinocut.state import (
    ExportProfile,
    TimelineSegment,
    TimelineV1,
    TransitionSpec,
)

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

requires_ffmpeg = pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="ffmpeg/ffprobe not installed")


def make_segment(
    index: int,
    *,
    duration: float = 4.0,
    transition_in: TransitionSpec | None = None,
) -> TimelineSegment:
    return TimelineSegment(
        segment_id=f"seg_{index:02d}",
        clip_id=f"clip_{index:02d}",
        source_in_sec=0.0,
        source_out_sec=duration,
        timeline_in_sec=0.0,
        timeline_out_sec=duration,
        transition_in=transition_in,
    )


def make_timeline(segments: list[TimelineSegment], **overrides) -> TimelineV1:
    actual = sum(s.source_out_sec - s.source_in_sec for s in segments)
    defaults = dict(
        project_id="p1",
        scene_id="scene_t",
        timeline_version=1,
        scene_goal="test scene",
        style_profile="test",
        editing_mode="hybrid",
        editing_template="cinematic_montage",
        target_duration_sec=actual,
        actual_duration_sec=actual,
        video_segments=segments,
    )
    defaults.update(overrides)
    return TimelineV1(**defaults)


# ── unit: command/filter construction ──


def test_join_filtergraph_all_cuts_uses_concat() -> None:
    service = SceneRenderService()
    segments = [
        make_segment(0),
        make_segment(1, transition_in=TransitionSpec(type="cut", duration_sec=0.0)),
        make_segment(2, transition_in=TransitionSpec(type="cut", duration_sec=0.0)),
    ]
    filters, video_label, audio_label = service.build_join_filtergraph(segments)

    join_filters = [f for f in filters if "settb" not in f]
    assert all("concat" in f for f in join_filters)
    assert not any("xfade" in f for f in filters)
    assert video_label == "[v2]"
    assert audio_label == "[a2]"


def test_join_filtergraph_crossfade_uses_xfade_with_offset() -> None:
    service = SceneRenderService()
    segments = [
        make_segment(0, duration=4.0),
        make_segment(
            1,
            duration=4.0,
            transition_in=TransitionSpec(type="crossfade", duration_sec=0.4),
        ),
    ]
    filters, _, _ = service.build_join_filtergraph(segments)

    xfade = [f for f in filters if "xfade" in f]
    acrossfade = [f for f in filters if "acrossfade" in f]
    assert len(xfade) == 1 and len(acrossfade) == 1
    assert "transition=fade" in xfade[0]
    assert "offset=3.600" in xfade[0]
    assert "d=0.400" in acrossfade[0]


def test_join_filtergraph_dip_to_black_maps_to_fadeblack() -> None:
    service = SceneRenderService()
    segments = [
        make_segment(0),
        make_segment(
            1,
            transition_in=TransitionSpec(type="dip_to_black", duration_sec=0.5),
        ),
    ]
    filters, _, _ = service.build_join_filtergraph(segments)
    assert any("transition=fadeblack" in f for f in filters)


def test_join_args_cut_only_uses_concat_demuxer(tmp_path: Path) -> None:
    service = SceneRenderService()
    files = [tmp_path / "a.mp4", tmp_path / "b.mp4"]
    segments = [
        make_segment(0),
        make_segment(1, transition_in=TransitionSpec(type="cut", duration_sec=0.0)),
    ]
    settings = RenderSettings.for_profile(ExportProfile())
    args = service.build_join_args(
        files, segments, tmp_path / "out.mp4", settings, work_dir=tmp_path
    )

    assert "concat" in args
    assert (tmp_path / "concat.txt").exists()
    assert "-filter_complex" not in args


def test_render_settings_preview_downscales_and_stays_even() -> None:
    settings = RenderSettings.for_profile(ExportProfile(resolution="1920x1080"), preview=False)
    preview = RenderSettings.for_profile(ExportProfile(resolution="1920x1080"), preview=True)

    assert (settings.width, settings.height) == (1920, 1080)
    assert preview.width <= 854 and preview.height <= 480
    assert preview.width % 2 == 0 and preview.height % 2 == 0
    assert preview.crf > settings.crf


def test_render_returns_none_when_clips_missing(tmp_path: Path) -> None:
    service = SceneRenderService()
    timeline = make_timeline([make_segment(0)])
    result = service.render(timeline, {}, tmp_path / "out.mp4")
    assert result is None


def test_audio_overlay_skipped_without_tracks(tmp_path: Path) -> None:
    service = SceneRenderService()
    timeline = make_timeline([make_segment(0)])
    args = service.build_audio_overlay_args(tmp_path / "in.mp4", timeline, tmp_path / "out.mp4")
    assert args is None


# ── e2e: real ffmpeg render through scene build ──


def generate_clip(path: Path, *, duration: float, color_shift: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i",
            f"testsrc=duration={duration}:size=1280x720:rate=24",
            "-f", "lavfi", "-i", f"sine=frequency={330 + color_shift}:duration={duration}",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", "-shortest",
            "-y", str(path),
        ],
        check=True,
        timeout=120,
    )


def probe(path: Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    return json.loads(proc.stdout)


@requires_ffmpeg
def test_scene_build_renders_real_video(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    for index, name in enumerate(["spaceport_wide", "spaceport_move", "spaceport_detail"]):
        generate_clip(clips_dir / f"{name}.mp4", duration=4.0, color_shift=index * 110)

    music_path = tmp_path / "music.m4a"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=220:duration=20",
            "-c:a", "aac", "-y", str(music_path),
        ],
        check=True,
        timeout=120,
    )

    request = SceneBuildRequest(
        input_folder=clips_dir,
        output_dir=tmp_path / "out",
        scene_id="scene_e2e",
        scene_goal="arrival at abandoned spaceport",
        style_profile="cinematic dark sci-fi",
        target_duration_sec=12.0,
        music_path=music_path,
    )
    scene_state = SceneStitcherAgent().build_from_request(request)

    assert scene_state.timeline is not None
    video_path = Path(scene_state.reviews["video_path"])
    assert video_path.exists() and video_path.stat().st_size > 0

    info = probe(video_path)
    duration = float(info["format"]["duration"])
    assert abs(duration - scene_state.timeline.actual_duration_sec) <= 0.5

    codec_types = {stream["codec_type"] for stream in info["streams"]}
    assert codec_types == {"video", "audio"}

    video_stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (video_stream["width"], video_stream["height"]) == (1920, 1080)

    preview_path = Path(scene_state.reviews["preview_video_path"])
    assert preview_path.exists()
    preview_stream = next(
        s for s in probe(preview_path)["streams"] if s["codec_type"] == "video"
    )
    assert preview_stream["width"] <= 854


@requires_ffmpeg
def test_render_skip_warns_but_keeps_timeline_export(tmp_path: Path, monkeypatch) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    generate_clip(clips_dir / "solo_take.mp4", duration=4.0, color_shift=0)

    request = SceneBuildRequest(
        input_folder=clips_dir,
        output_dir=tmp_path / "out",
        scene_id="scene_skip",
        scene_goal="single take scene",
        target_duration_sec=4.0,
    )
    agent = SceneStitcherAgent()
    monkeypatch.setattr(agent.tools.renderer, "is_available", lambda: False)
    scene_state = agent.build_from_request(request)

    assert scene_state.timeline is not None
    assert "video_path" not in scene_state.reviews
    assert any("render skipped" in str(error) for error in scene_state.errors)
    assert Path(scene_state.reviews["timeline_path"]).exists()
