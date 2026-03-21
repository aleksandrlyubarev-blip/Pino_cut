"""Tests for Pinnocat Scene Stitcher v1 skeleton."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pinocut.cli as cli
from pinocut.cli import parse_scene_args
from pinocut.scene_stitcher import SceneBuildRequest, SceneStitcherAgent
from pinocut.state import ClipSegment, ExportProfile, SceneState


def make_clip(
    tmp_path: Path,
    name: str,
    *,
    duration: float,
    resolution: tuple[int, int],
    fps: float = 24.0,
    metadata: dict | None = None,
) -> ClipSegment:
    return ClipSegment(
        path=tmp_path / f"{name}.mp4",
        duration=duration,
        has_audio=True,
        resolution=resolution,
        fps=fps,
        metadata=metadata or {},
    )


def test_scene_rejects_low_quality_clips(tmp_path: Path) -> None:
    clips = [
        make_clip(tmp_path, "hero_open", duration=6.0, resolution=(1920, 1080)),
        make_clip(tmp_path, "wide_move", duration=7.0, resolution=(1920, 1080)),
        make_clip(tmp_path, "detail_shot", duration=5.0, resolution=(1920, 1080)),
        make_clip(tmp_path, "low_res", duration=5.0, resolution=(640, 360)),
    ]

    scene = SceneState(
        project_id="rfv_pinnocat_001",
        scene_id="scene_03",
        scene_goal="arrival at abandoned spaceport",
        style_profile="cinematic dark sci-fi",
        target_duration_sec=24.0,
        available_clips=clips,
        output_dir=str(tmp_path / "out"),
    )

    result = SceneStitcherAgent().build_scene(scene)

    assert "low_res" in result.rejected_clips
    assert "low_res" not in result.approved_clips
    assert result.clip_scores["low_res"].passes_gate is False


def test_scene_build_exports_timeline_and_preview(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    music_path = tmp_path / "music.mp3"
    music_path.write_text("placeholder", encoding="utf-8")

    clips = [
        make_clip(tmp_path, "c01", duration=6.0, resolution=(1920, 1080)),
        make_clip(tmp_path, "c02", duration=6.5, resolution=(1920, 1080)),
        make_clip(tmp_path, "c03", duration=5.0, resolution=(1920, 1080)),
        make_clip(tmp_path, "c04", duration=7.5, resolution=(1920, 1080)),
    ]

    scene = SceneState(
        project_id="rfv_pinnocat_001",
        scene_id="scene_03",
        scene_goal="arrival at abandoned spaceport",
        style_profile="cinematic dark sci-fi",
        target_duration_sec=20.0,
        editing_template="cinematic_montage",
        available_clips=clips,
        music_track=str(music_path),
        output_dir=str(output_dir),
        export_profile=ExportProfile(resolution="1920x1080", fps=24, format="mp4"),
    )

    result = SceneStitcherAgent().build_scene(scene)

    assert result.timeline is not None
    assert len(result.timeline.video_segments) == 4
    assert any(
        segment.transition_out and segment.transition_out.type == "crossfade"
        for segment in result.timeline.video_segments
    )
    assert result.timeline.audio_segments[0].track_id == str(music_path)

    timeline_path = output_dir / "scene_03.timeline.json"
    preview_path = output_dir / "scene_03.preview.json"
    version_path = output_dir / "scene_03.timeline.v1.json"
    assert timeline_path.exists()
    assert preview_path.exists()
    assert version_path.exists()

    payload = json.loads(timeline_path.read_text(encoding="utf-8"))
    assert payload["scene_id"] == "scene_03"
    assert payload["editing_template"] == "cinematic_montage"
    assert payload["tracks"]["video"][1]["transition_out"]["type"] == "crossfade"


def test_scene_timeline_keeps_approved_order_and_transition_overlap(tmp_path: Path) -> None:
    clips = [
        make_clip(tmp_path, "c04", duration=11.0, resolution=(1920, 1080), fps=20.0),
        make_clip(tmp_path, "c03", duration=2.2, resolution=(1920, 1080)),
        make_clip(tmp_path, "c02", duration=6.0, resolution=(1280, 720)),
        make_clip(tmp_path, "c01", duration=6.0, resolution=(1920, 1080)),
    ]

    scene = SceneState(
        project_id="rfv_pinnocat_001",
        scene_id="scene_04",
        scene_goal="arrival at abandoned spaceport",
        style_profile="cinematic dark sci-fi",
        target_duration_sec=20.0,
        editing_template="cinematic_montage",
        available_clips=clips,
        output_dir=str(tmp_path / "output"),
    )

    result = SceneStitcherAgent().build_scene(scene)

    assert result.timeline is not None
    assert result.approved_clips == ["c01", "c02", "c03", "c04"]
    assert result.timeline.used_clips == result.approved_clips
    assert [segment.clip_id for segment in result.timeline.video_segments] == result.approved_clips
    assert result.timeline.video_segments[2].timeline_in_sec < result.timeline.video_segments[1].timeline_out_sec
    assert result.timeline.actual_duration_sec == 16.8


def test_parse_scene_build_args() -> None:
    args = parse_scene_args(
        [
            "build",
            "clips",
            "--goal",
            "arrival at abandoned spaceport",
            "--scene-id",
            "scene_03",
            "--template",
            "trailer_cut",
            "--duration",
            "35",
        ]
    )

    assert args.scene_command == "build"
    assert args.input_folder == Path("clips")
    assert args.goal == "arrival at abandoned spaceport"
    assert args.scene_id == "scene_03"
    assert args.template == "trailer_cut"
    assert args.duration == 35.0


def test_main_routes_scene_subcommand(monkeypatch) -> None:
    def fake_main_scene(argv: list[str] | None = None) -> int:
        assert argv == ["build", "clips", "--goal", "arrival"]
        return 7

    monkeypatch.setattr(cli, "main_scene", fake_main_scene)

    result = cli.main(["scene", "build", "clips", "--goal", "arrival"])

    assert result == 7


def test_main_preserves_legacy_folder_named_scene(monkeypatch) -> None:
    parsed = cli.parse_args(["scene"])

    def fake_parse_args(argv: list[str] | None = None):
        assert argv == ["scene"]
        return parsed

    class FakeAgent:
        def __init__(self, *, structured_logs: bool = False):
            self.structured_logs = structured_logs

        def run(self, config):
            assert config.input_folder == Path("scene")
            return {"output_path": "output.mp4"}

        def metrics_report(self) -> str:
            return "metrics"

    monkeypatch.setattr(cli, "parse_args", fake_parse_args)
    fake_agent_module = ModuleType("pinocut.agent")
    fake_agent_module.PinoCutAgent = FakeAgent
    monkeypatch.setitem(__import__("sys").modules, "pinocut.agent", fake_agent_module)

    result = cli.main(["scene"])

    assert result == 0


def test_scene_request_builds_state_from_folder(tmp_path: Path) -> None:
    clips_dir = tmp_path / "clips"
    clips_dir.mkdir()
    for name in ["a.mp4", "b.mp4"]:
        (clips_dir / name).write_text("not real media", encoding="utf-8")

    request = SceneBuildRequest(
        input_folder=clips_dir,
        output_dir=tmp_path / "output",
        scene_goal="test",
        subtitle_language="en",
    )

    result = SceneStitcherAgent().build_from_request(request)

    assert result.scene_id == "scene_01"
    assert result.subtitle_track == "en"
    assert result.errors  # probe fails on fake media, but the path is exercised
