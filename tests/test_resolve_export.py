"""Tests for the DaVinci Resolve CMX 3600 EDL export."""

from __future__ import annotations

from pathlib import Path

from pinocut.resolve_export import export_edl, seconds_to_timecode, timeline_to_edl
from pinocut.scene_stitcher import SceneStitcherAgent
from pinocut.state import (
    ClipSegment,
    ExportProfile,
    SceneState,
    TimelineSegment,
    TimelineV1,
    TransitionSpec,
)


def make_segment(
    index: int,
    clip_id: str,
    *,
    source_in: float = 0.0,
    source_out: float = 4.0,
    timeline_in: float = 0.0,
    timeline_out: float = 4.0,
    transition_in: TransitionSpec | None = None,
) -> TimelineSegment:
    return TimelineSegment(
        segment_id=f"seg_{index:02d}",
        clip_id=clip_id,
        source_in_sec=source_in,
        source_out_sec=source_out,
        timeline_in_sec=timeline_in,
        timeline_out_sec=timeline_out,
        transition_in=transition_in,
    )


def make_timeline(segments: list[TimelineSegment]) -> TimelineV1:
    return TimelineV1(
        project_id="p1",
        scene_id="scene_edl",
        timeline_version=1,
        scene_goal="test",
        style_profile="test",
        editing_mode="hybrid",
        editing_template="cinematic_montage",
        target_duration_sec=8.0,
        actual_duration_sec=8.0,
        video_segments=segments,
        export_profile=ExportProfile(fps=24),
    )


def test_seconds_to_timecode() -> None:
    assert seconds_to_timecode(0.0, 24) == "00:00:00:00"
    assert seconds_to_timecode(4.0, 24) == "00:00:04:00"
    assert seconds_to_timecode(3661.5, 24) == "01:01:01:12"
    assert seconds_to_timecode(0.4, 24) == "00:00:00:10"


def test_cut_only_edl_events() -> None:
    timeline = make_timeline(
        [
            make_segment(1, "shot_a", timeline_in=0.0, timeline_out=4.0),
            make_segment(
                2,
                "shot_b",
                timeline_in=4.0,
                timeline_out=8.0,
                transition_in=TransitionSpec(type="cut", duration_sec=0.0),
            ),
        ]
    )
    edl = timeline_to_edl(timeline)

    assert edl.startswith("TITLE: scene_edl")
    assert "FCM: NON-DROP FRAME" in edl
    assert "001  AX       V     C        00:00:00:00 00:00:04:00 00:00:00:00 00:00:04:00" in edl
    assert "002  AX       V     C        00:00:00:00 00:00:04:00 00:00:04:00 00:00:08:00" in edl
    assert "* FROM CLIP NAME: shot_a.mp4" in edl
    assert "D " not in edl


def test_crossfade_becomes_two_line_dissolve() -> None:
    timeline = make_timeline(
        [
            make_segment(1, "shot_a", timeline_in=0.0, timeline_out=4.0),
            make_segment(
                2,
                "shot_b",
                timeline_in=3.6,
                timeline_out=7.6,
                transition_in=TransitionSpec(type="crossfade", duration_sec=0.4),
            ),
        ]
    )
    edl = timeline_to_edl(timeline)

    # 0.4s at 24fps -> 10 frames
    assert "002  AX       V     D 010    " in edl
    # Zero-duration statement of the outgoing source shares the event number.
    assert "002  AX       V     C        00:00:04:00 00:00:04:00 00:00:03:14 00:00:03:14" in edl
    assert "* FROM CLIP NAME: shot_a.mp4" in edl
    assert "* TO CLIP NAME: shot_b.mp4" in edl


def test_clip_names_resolve_from_paths() -> None:
    timeline = make_timeline([make_segment(1, "shot_a")])
    edl = timeline_to_edl(timeline, {"shot_a": Path("/footage/A001_take3.MP4")})
    assert "* FROM CLIP NAME: A001_take3.MP4" in edl


def test_export_writes_file(tmp_path: Path) -> None:
    timeline = make_timeline([make_segment(1, "shot_a")])
    path = export_edl(timeline, {}, tmp_path)
    assert path == tmp_path / "scene_edl.edl"
    assert path.read_text(encoding="utf-8").startswith("TITLE: scene_edl")


def test_scene_build_exports_edl(tmp_path: Path) -> None:
    clips = [
        ClipSegment(
            path=tmp_path / f"take_{index}.mp4",
            duration=6.0,
            has_audio=True,
            resolution=(1920, 1080),
            fps=24.0,
            metadata={},
        )
        for index in range(1, 5)
    ]
    scene = SceneState(
        project_id="p1",
        scene_id="scene_handoff",
        scene_goal="test scene",
        style_profile="test",
        target_duration_sec=20.0,
        available_clips=clips,
        output_dir=str(tmp_path / "out"),
    )
    result = SceneStitcherAgent().build_scene(scene)

    edl_path = Path(result.reviews["edl_path"])
    assert edl_path.exists()
    content = edl_path.read_text(encoding="utf-8")
    assert content.startswith("TITLE: scene_handoff")
    assert content.count("FROM CLIP NAME") >= len(result.approved_clips)
