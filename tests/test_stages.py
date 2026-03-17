"""Tests for PinoCut pipeline stages and effects."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from pinocut.config import ColorGradeStyle, ProjectConfig, TransitionConfig
from pinocut.effects import (
    KenBurnsConfig,
    LowerThirdStyle,
    TitleAnimation,
    animated_opacity,
    animated_position,
    apply_color_grade,
    ken_burns_frame,
    make_color_grade_filter,
    render_lower_third,
    write_cube_lut,
)
from pinocut.stages.analyze import AnalyzeStage
from pinocut.stages.audio_mix import AudioMixStage
from pinocut.stages.ingest import IngestStage
from pinocut.stages.render import RenderStage
from pinocut.stages.titles import TitleStage
from pinocut.stages.transitions import TransitionStage
from pinocut.state import (
    ClipAnalysis,
    ClipSegment,
    EditDecision,
    PinoCutState,
    RomeoVisionData,
    TransitionType,
)
from pinocut.utils.errors import ErrorSeverity


# ── Fixtures ──

@pytest.fixture
def config(tmp_path):
    folder = tmp_path / "footage"
    folder.mkdir()
    return ProjectConfig(input_folder=folder)


@pytest.fixture
def sample_clips(tmp_path):
    return [
        ClipSegment(
            path=tmp_path / "clip1.mp4", duration=8.5, has_audio=True,
            resolution=(1920, 1080), fps=30.0,
            speech_segments=[(1.0, 3.0), (5.0, 7.0)],
        ),
        ClipSegment(
            path=tmp_path / "clip2.mp4", duration=12.0, has_audio=True,
            resolution=(1920, 1080), fps=30.0, speech_segments=[],
        ),
    ]


def make_state(config, clips=None, **overrides) -> PinoCutState:
    state: PinoCutState = {
        "project_config": config, "clips": clips or [],
        "analysis": {}, "romeo_data": {}, "edit_decisions": [],
        "title_overlays": [], "audio_mix": None,
        "output_path": None, "render_preset": "standard", "errors": [],
    }
    state.update(overrides)
    return state


# ── IngestStage ──

class TestIngestStage:
    def test_empty_folder(self, config):
        result = IngestStage()(make_state(config))
        assert result["clips"] == []
        assert any(e.severity == ErrorSeverity.FATAL for e in result["errors"])

    def test_missing_folder(self, tmp_path):
        cfg = ProjectConfig(input_folder=tmp_path / "nonexistent")
        result = IngestStage()(make_state(cfg))
        assert any("not found" in e.message for e in result["errors"])


# ── TransitionStage ──

class TestTransitionStage:
    def test_similar_scenes_hard_cut(self, config, sample_clips):
        hist = [float(x) for x in range(64)]
        analysis = {
            str(sample_clips[0].path): ClipAnalysis(clip_path=str(sample_clips[0].path), last_frame_hist=hist),
            str(sample_clips[1].path): ClipAnalysis(clip_path=str(sample_clips[1].path), first_frame_hist=hist),
        }
        result = TransitionStage()(make_state(config, clips=sample_clips, analysis=analysis))
        assert result["edit_decisions"][0].transition == TransitionType.HARD_CUT

    def test_different_scenes_crossfade(self, config, sample_clips):
        analysis = {
            str(sample_clips[0].path): ClipAnalysis(clip_path=str(sample_clips[0].path), last_frame_hist=[float(x) for x in range(64)]),
            str(sample_clips[1].path): ClipAnalysis(clip_path=str(sample_clips[1].path), first_frame_hist=[float(63 - x) for x in range(64)]),
        }
        result = TransitionStage()(make_state(config, clips=sample_clips, analysis=analysis))
        assert result["edit_decisions"][0].transition == TransitionType.CROSSFADE

    def test_location_change_dip_to_black(self, config, sample_clips):
        romeo_data = {
            str(sample_clips[0].path): RomeoVisionData(location_tag="studio"),
            str(sample_clips[1].path): RomeoVisionData(location_tag="outdoor"),
        }
        result = TransitionStage()(make_state(config, clips=sample_clips, romeo_data=romeo_data))
        assert result["edit_decisions"][0].transition == TransitionType.DIP_TO_BLACK


# ── TitleStage ──

class TestTitleStage:
    def test_generates_titles_from_romeo(self, config, sample_clips):
        romeo_data = {
            str(sample_clips[0].path): RomeoVisionData(scene_description="Opening shot"),
            str(sample_clips[1].path): RomeoVisionData(scene_description=""),
        }
        result = TitleStage()(make_state(config, clips=sample_clips, romeo_data=romeo_data))
        assert result["title_overlays"][0].text == "Opening shot"
        assert result["title_overlays"][1].text == "Scene 2"

    def test_mood_to_color_grade(self, config, sample_clips):
        romeo_data = {
            str(sample_clips[0].path): RomeoVisionData(mood="warm summer"),
            str(sample_clips[1].path): RomeoVisionData(mood="warm evening"),
        }
        result = TitleStage()(make_state(config, clips=sample_clips, romeo_data=romeo_data))
        assert result["color_grade"] == ColorGradeStyle.WARM


# ── AudioMixStage ──

class TestAudioMixStage:
    def test_ducking_regions_from_speech(self, config, sample_clips, tmp_path):
        music_file = tmp_path / "music.mp3"
        music_file.touch()
        config.bg_music_path = music_file
        edit_decisions = [EditDecision(from_clip=str(sample_clips[0].path), to_clip=str(sample_clips[1].path), transition=TransitionType.HARD_CUT)]
        result = AudioMixStage()(make_state(config, clips=sample_clips, edit_decisions=edit_decisions))
        assert len(result["audio_mix"].ducking_regions) == 6  # 2 speech segs * 3 regions each

    def test_crossfade_offset(self, config, sample_clips, tmp_path):
        music_file = tmp_path / "music.mp3"
        music_file.touch()
        config.bg_music_path = music_file
        edit_decisions = [EditDecision(from_clip=str(sample_clips[0].path), to_clip=str(sample_clips[1].path), transition=TransitionType.CROSSFADE, duration=0.75)]
        result = AudioMixStage()(make_state(config, clips=sample_clips, edit_decisions=edit_decisions))
        for r in result["audio_mix"].ducking_regions:
            assert r["end"] <= 20.5

    def test_no_music_no_crash(self, config, sample_clips):
        result = AudioMixStage()(make_state(config, clips=sample_clips))
        assert len(result["audio_mix"].tracks) == 0


# ── Effects ──

class TestColorGrading:
    def test_warm_lut(self):
        frame = np.full((100, 100, 3), 128, dtype=np.uint8)
        result = apply_color_grade(frame, "warm")
        assert result.shape == frame.shape
        assert result[:, :, 0].mean() > frame[:, :, 0].mean()  # red boosted
        assert result[:, :, 2].mean() < frame[:, :, 2].mean()  # blue reduced

    def test_cinematic_lut(self):
        frame = np.full((100, 100, 3), 128, dtype=np.uint8)
        result = apply_color_grade(frame, "cinematic")
        assert result.shape == frame.shape
        assert not np.array_equal(result, frame)

    def test_none_grade_passthrough(self):
        frame = np.full((100, 100, 3), 128, dtype=np.uint8)
        result = apply_color_grade(frame, "none")
        np.testing.assert_array_equal(result, frame)

    def test_make_filter(self):
        fn = make_color_grade_filter("vintage")
        assert fn is not None
        assert make_color_grade_filter("none") is None
        assert make_color_grade_filter("nonexistent") is None


class TestKenBurns:
    def test_zoom_in(self):
        frame = np.random.randint(0, 255, (200, 300, 3), dtype=np.uint8)
        result = ken_burns_frame(frame, t=5.0, duration=10.0)
        assert result.shape == frame.shape

    def test_custom_config(self):
        frame = np.random.randint(0, 255, (200, 300, 3), dtype=np.uint8)
        cfg = KenBurnsConfig(zoom_start=1.0, zoom_end=1.3, pan_x=0.5, pan_y=-0.3)
        result = ken_burns_frame(frame, t=0.0, duration=10.0, config=cfg)
        assert result.shape == frame.shape


class TestLowerThird:
    def test_render(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = render_lower_third(frame, "Test Title", t=0.3, total_duration=4.0)
        assert result.shape == frame.shape
        assert not np.array_equal(result, frame)  # something was drawn

    def test_opacity_zero_at_end(self):
        assert animated_opacity(0.0, 4.0) < 0.1
        assert animated_opacity(2.0, 4.0) == 1.0
        assert animated_opacity(3.9, 4.0) < 0.3


class TestAnimatedPosition:
    def test_slide_up_start(self):
        _, y = animated_position(0.0, 0.5, TitleAnimation.SLIDE_UP, 100, 400, 480, 640)
        assert y > 400  # starts below target

    def test_slide_up_end(self):
        _, y = animated_position(0.5, 0.5, TitleAnimation.SLIDE_UP, 100, 400, 480, 640)
        assert y == 400  # at target


# ── AnalyzeStage ──

class TestAnalyzeStage:
    def test_no_clips_produces_empty_analysis(self, config):
        result = AnalyzeStage()(make_state(config, clips=[]))
        assert result["analysis"] == {}
        assert result["romeo_data"] == {}

    def test_serial_analysis_without_moltis(self, config, sample_clips, monkeypatch):
        stage = AnalyzeStage()
        monkeypatch.setattr(
            stage, "_analyze_clip",
            lambda clip, whisper, cfg: ClipAnalysis(clip_path=str(clip.path), avg_brightness=100.0),
        )
        result = stage(make_state(config, clips=sample_clips))
        assert len(result["analysis"]) == 2
        assert all(a.avg_brightness == 100.0 for a in result["analysis"].values())

    def test_parallel_analysis_with_moltis(self, config, sample_clips, monkeypatch):
        from pinocut.integrations.moltis_bridge import MoltisBridge
        moltis = MoltisBridge()
        stage = AnalyzeStage(moltis=moltis)
        monkeypatch.setattr(
            stage, "_analyze_clip",
            lambda clip, whisper, cfg: ClipAnalysis(clip_path=str(clip.path), avg_brightness=50.0),
        )
        result = stage(make_state(config, clips=sample_clips))
        assert len(result["analysis"]) == 2
        moltis.shutdown()

    def test_analysis_failure_uses_empty_fallback(self, config, sample_clips, monkeypatch):
        stage = AnalyzeStage()
        def _fail(clip, whisper, cfg):
            raise RuntimeError("mock fail")
        monkeypatch.setattr(stage, "_analyze_clip", _fail)
        result = stage(make_state(config, clips=sample_clips))
        assert len(result["analysis"]) == len(sample_clips)
        assert all(a.speech_segments == [] for a in result["analysis"].values())
        assert any(e.severity == ErrorSeverity.WARN for e in result["errors"])

    def test_cache_hit_skips_analyze(self, config, sample_clips, monkeypatch):
        from pinocut.integrations.moltis_bridge import MoltisBridge
        moltis = MoltisBridge()
        clip = sample_clips[0]
        cache_key = f"analysis:{clip.path.name}:{clip.duration}"
        moltis.memory.set(cache_key, {
            "clip_path": str(clip.path),
            "speech_segments": [(1.0, 2.0)],
            "first_frame_hist": None,
            "last_frame_hist": None,
            "avg_brightness": 77.0,
        })
        stage = AnalyzeStage(moltis=moltis)
        calls = {"n": 0}
        def _count(clip, whisper, cfg):
            calls["n"] += 1
            return ClipAnalysis(clip_path=str(clip.path))
        monkeypatch.setattr(stage, "_analyze_clip", _count)
        result = stage(make_state(config, clips=[clip]))
        assert result["analysis"][str(clip.path)].speech_segments == [(1.0, 2.0)]
        assert calls["n"] == 0  # cache was used, _analyze_clip never called
        moltis.shutdown()


# ── RenderStage ──

class TestRenderStage:
    def test_no_clips_fatal_moviepy(self, config):
        result = RenderStage()(make_state(config, clips=[]))
        assert result["output_path"] is None
        assert any(e.severity == ErrorSeverity.FATAL for e in result["errors"])

    def test_no_clips_fatal_ffmpeg(self, config):
        result = RenderStage(render_mode="ffmpeg")(make_state(config, clips=[]))
        assert result["output_path"] is None
        assert any(e.severity == ErrorSeverity.FATAL for e in result["errors"])

    def test_ffmpeg_normalize_failure_records_fatal(self, config, sample_clips, monkeypatch):
        stage = RenderStage(render_mode="ffmpeg")
        monkeypatch.setattr(stage, "_run_ffmpeg", lambda args: 1)
        result = stage(make_state(config, clips=sample_clips))
        assert result["output_path"] is None
        assert any(e.severity == ErrorSeverity.FATAL for e in result["errors"])

    def test_ffmpeg_success_sets_output_path(self, tmp_path, monkeypatch):
        out = tmp_path / "out.mp4"
        cfg = ProjectConfig(input_folder=tmp_path / "footage", output_path=out)
        clips = [ClipSegment(path=tmp_path / "clip1.mp4", duration=5.0, has_audio=False,
                             resolution=(1920, 1080), fps=30.0)]
        stage = RenderStage(render_mode="ffmpeg")

        def _mock_ffmpeg(args):
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake")
            return 0

        monkeypatch.setattr(stage, "_run_ffmpeg", _mock_ffmpeg)
        result = stage(make_state(cfg, clips=clips))
        assert result["output_path"] == str(out)

    def test_ffmpeg_applies_lut3d_when_color_grade_set(self, tmp_path, monkeypatch):
        out = tmp_path / "out.mp4"
        cfg = ProjectConfig(input_folder=tmp_path / "footage", output_path=out)
        clips = [ClipSegment(path=tmp_path / "clip1.mp4", duration=5.0, has_audio=False,
                             resolution=(1920, 1080), fps=30.0)]
        stage = RenderStage(render_mode="ffmpeg")

        captured: list[list[str]] = []

        def _mock_ffmpeg(args):
            captured.append(list(args))
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake")
            return 0

        monkeypatch.setattr(stage, "_run_ffmpeg", _mock_ffmpeg)
        state = make_state(cfg, clips=clips, color_grade=ColorGradeStyle.CINEMATIC)
        result = stage(state)

        # The normalize call (first ffmpeg invocation) must include -vf lut3d=...
        norm_args = captured[0]
        assert "-vf" in norm_args
        lut_arg = norm_args[norm_args.index("-vf") + 1]
        assert lut_arg.startswith("lut3d=") and lut_arg.endswith(".cube")

    def test_ffmpeg_no_lut_when_color_grade_none(self, tmp_path, monkeypatch):
        out = tmp_path / "out.mp4"
        cfg = ProjectConfig(input_folder=tmp_path / "footage", output_path=out)
        clips = [ClipSegment(path=tmp_path / "clip1.mp4", duration=5.0, has_audio=False,
                             resolution=(1920, 1080), fps=30.0)]
        stage = RenderStage(render_mode="ffmpeg")

        captured: list[list[str]] = []

        def _mock_ffmpeg(args):
            captured.append(list(args))
            dest = Path(args[-1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake")
            return 0

        monkeypatch.setattr(stage, "_run_ffmpeg", _mock_ffmpeg)
        result = stage(make_state(cfg, clips=clips))
        assert "-vf" not in captured[0]


# ── Ken Burns wiring ──

class TestKenBurnsWiring:
    def test_silent_clips_marked_for_ken_burns(self, config, sample_clips):
        # sample_clips[0] has speech, sample_clips[1] does not
        result = TitleStage()(make_state(config, clips=sample_clips, romeo_data={}))
        kb = result.get("ken_burns_clips", [])
        assert str(sample_clips[1].path) in kb
        assert str(sample_clips[0].path) not in kb

    def test_speech_clips_not_marked_for_ken_burns(self, config, sample_clips):
        result = TitleStage()(make_state(config, clips=sample_clips, romeo_data={}))
        assert str(sample_clips[0].path) not in result.get("ken_burns_clips", [])

    def test_write_cube_lut_produces_valid_file(self, tmp_path):
        path = tmp_path / "test.cube"
        assert write_cube_lut(ColorGradeStyle.CINEMATIC, path, size=4)
        lines = path.read_text().splitlines()
        assert lines[0] == "LUT_3D_SIZE 4"
        assert len(lines) == 1 + 4 ** 3  # header + 64 entries

    def test_write_cube_lut_none_returns_false(self, tmp_path):
        assert not write_cube_lut(ColorGradeStyle.NONE, tmp_path / "none.cube")


# ── Utils ──

class TestUtils:
    def test_stage_error_string(self):
        from pinocut.utils.errors import StageError
        e = StageError(stage="ingest", message="File not found", clip_path="/a/b.mp4")
        assert "ingest" in str(e) and "/a/b.mp4" in str(e)

    def test_result_types(self):
        from pinocut.utils.errors import ok, err
        assert ok(42).is_ok and ok(42).value == 42
        assert err("boom").is_err and err("boom").error == "boom"
