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
)
from pinocut.stages.audio_mix import AudioMixStage
from pinocut.stages.ingest import IngestStage
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
