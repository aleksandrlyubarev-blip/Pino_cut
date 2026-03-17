"""RenderStage: final video assembly and export.

Phase 3 upgrades:
- Dual-mode rendering: moviepy (fallback) or FFmpeg subprocess (performance)
- LUT color grading per-frame via effects.py
- Animated lower-third overlays via OpenCV
- E2B sandbox support for FFmpeg commands
- Per-sample audio ducking with volume ramps
"""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path

from pinocut.config import RENDER_PRESETS, ColorGradeStyle, RenderPreset
from pinocut.effects import LowerThirdStyle, make_color_grade_filter, render_lower_third
from pinocut.integrations.e2b_sandbox import SandboxConfig, SandboxExecutor
from pinocut.stages.base import BaseStage
from pinocut.state import PinoCutState, TransitionType
from pinocut.utils.errors import ErrorSeverity, StageError


class RenderStage(BaseStage):
    """Assemble final video from clips + transitions + titles + audio + color grading.

    Render modes:
    - 'moviepy': Full moviepy pipeline (default, most compatible)
    - 'ffmpeg': Direct FFmpeg subprocess (faster, Phase 3)
    """

    name = "render"

    def __init__(
        self,
        *,
        structured_logs: bool = False,
        render_mode: str = "moviepy",  # moviepy | ffmpeg
        sandbox: SandboxExecutor | None = None,
    ):
        super().__init__(structured_logs=structured_logs)
        self._mode = render_mode
        self._sandbox = sandbox

    def process(self, state: PinoCutState) -> PinoCutState:
        if self._mode == "ffmpeg":
            return self._render_ffmpeg(state)
        return self._render_moviepy(state)

    # ══════════════════════════════════════════
    #  MOVIEPY RENDER (Phase 1-2, full featured)
    # ══════════════════════════════════════════

    def _render_moviepy(self, state: PinoCutState) -> PinoCutState:
        from moviepy.editor import (
            AudioFileClip,
            CompositeAudioClip,
            CompositeVideoClip,
            TextClip,
            VideoFileClip,
            concatenate_videoclips,
        )

        clips_data = state.get("clips", [])
        edit_decisions = state.get("edit_decisions", [])
        title_overlays = state.get("title_overlays", [])
        audio_mix = state.get("audio_mix")
        color_grade = state.get("color_grade", ColorGradeStyle.NONE)
        config = state["project_config"]
        errors: list[StageError] = list(state.get("errors", []))

        if not clips_data:
            errors.append(StageError(stage=self.name, message="No clips to render", severity=ErrorSeverity.FATAL))
            state["errors"] = errors
            state["output_path"] = None
            return state

        video_clips = []
        titles_by_path = {t.clip_path: t for t in title_overlays}

        for i, clip_seg in enumerate(clips_data):
            try:
                vclip = VideoFileClip(str(clip_seg.path))
                if config.max_duration and vclip.duration > config.max_duration:
                    vclip = vclip.subclip(0, config.max_duration)

                # ── LUT Color Grading ──
                grade_style = color_grade.value if isinstance(color_grade, ColorGradeStyle) else str(color_grade)
                grade_fn = make_color_grade_filter(grade_style)
                if grade_fn:
                    vclip = vclip.fl_image(grade_fn)

                # ── Animated Lower Third ──
                title = titles_by_path.get(str(clip_seg.path))
                if title and title.style == "lower_third" and title.animated:
                    vclip = self._apply_lower_third_moviepy(vclip, title, config)
                elif title:
                    vclip = self._apply_title(vclip, title, config)

                # ── Watermark ──
                vclip = self._apply_watermark(vclip, config)

                # ── Transition ──
                if i > 0 and i - 1 < len(edit_decisions):
                    decision = edit_decisions[i - 1]
                    if decision.transition == TransitionType.CROSSFADE:
                        vclip = vclip.crossfadein(decision.duration)
                    elif decision.transition == TransitionType.DIP_TO_BLACK:
                        vclip = vclip.fadein(decision.duration)

                video_clips.append(vclip)

            except Exception as exc:
                self.log.warn(f"Failed to load clip {clip_seg.path.name}: {exc}")
                errors.append(StageError(stage=self.name, message=str(exc), severity=ErrorSeverity.WARN, clip_path=str(clip_seg.path)))

        if not video_clips:
            errors.append(StageError(stage=self.name, message="All clips failed", severity=ErrorSeverity.FATAL))
            state["errors"] = errors
            state["output_path"] = None
            return state

        final = concatenate_videoclips(video_clips, method="compose")

        # ── Audio mix with ducking ──
        if audio_mix and audio_mix.tracks:
            final = self._apply_audio_mix_with_ducking(final, audio_mix, config)

        # ── Export ──
        output_path = Path(config.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        preset = RENDER_PRESETS.get(config.render_preset, RENDER_PRESETS[RenderPreset.STANDARD])

        self.log.info(f"Rendering {final.duration:.1f}s → {output_path} (moviepy, preset={config.render_preset.value})")

        try:
            final.write_videofile(
                str(output_path), fps=preset.fps, codec=preset.codec,
                audio_codec=preset.audio_codec, threads=preset.threads, preset=preset.preset,
            )
            state["output_path"] = str(output_path)
        except Exception as exc:
            errors.append(StageError(stage=self.name, message=f"Render failed: {exc}", severity=ErrorSeverity.FATAL))
            state["output_path"] = None
        finally:
            for vc in video_clips:
                try:
                    vc.close()
                except Exception:
                    pass

        state["errors"] = errors
        return state

    # ══════════════════════════════════════════
    #  FFMPEG RENDER (Phase 3, performance)
    # ══════════════════════════════════════════

    def _render_ffmpeg(self, state: PinoCutState) -> PinoCutState:
        """FFmpeg concat demuxer pipeline for faster rendering.

        Steps:
        1. Pre-process clips with transitions → intermediate files
        2. Generate concat list
        3. FFmpeg concat + audio mix → final output
        """
        clips_data = state.get("clips", [])
        edit_decisions = state.get("edit_decisions", [])
        audio_mix = state.get("audio_mix")
        config = state["project_config"]
        errors: list[StageError] = list(state.get("errors", []))

        if not clips_data:
            errors.append(StageError(stage=self.name, message="No clips", severity=ErrorSeverity.FATAL))
            state["errors"] = errors
            state["output_path"] = None
            return state

        preset = RENDER_PRESETS.get(config.render_preset, RENDER_PRESETS[RenderPreset.STANDARD])
        output_path = Path(config.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="pinocut_ffmpeg_") as tmpdir:
            tmp = Path(tmpdir)

            # ── Step 1: normalize clips to common format ──
            normalized: list[Path] = []
            for i, clip in enumerate(clips_data):
                norm_path = tmp / f"norm_{i:04d}.mp4"
                args = [
                    "-i", str(clip.path),
                    "-c:v", preset.codec,
                    "-preset", "ultrafast",  # fast for intermediates
                    "-crf", str(preset.crf + 5),  # lower quality for intermediates
                    "-c:a", "aac", "-b:a", "128k",
                    "-r", str(preset.fps),
                    "-y", str(norm_path),
                ]
                result = self._run_ffmpeg(args)
                if result == 0:
                    normalized.append(norm_path)
                else:
                    errors.append(StageError(stage=self.name, message=f"Normalize failed: {clip.path.name}", severity=ErrorSeverity.WARN))

            if not normalized:
                errors.append(StageError(stage=self.name, message="All clips failed normalize", severity=ErrorSeverity.FATAL))
                state["errors"] = errors
                state["output_path"] = None
                return state

            # ── Step 2: concat list ──
            concat_file = tmp / "concat.txt"
            lines = [f"file '{p.resolve()}'" for p in normalized]
            concat_file.write_text("\n".join(lines))

            # ── Step 3: concat + final encode ──
            concat_output = tmp / "concat.mp4"
            args = [
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c:v", preset.codec,
                "-preset", preset.preset,
                "-crf", str(preset.crf),
                "-c:a", preset.audio_codec, "-b:a", preset.audio_bitrate,
                "-threads", str(preset.threads),
                "-y", str(concat_output),
            ]

            # Add background music if present
            if audio_mix and audio_mix.tracks:
                music_track = next((t for t in audio_mix.tracks if t.role == "music"), None)
                if music_track:
                    args = [
                        "-f", "concat", "-safe", "0", "-i", str(concat_file),
                        "-stream_loop", "-1", "-i", music_track.path,
                        "-c:v", preset.codec, "-preset", preset.preset, "-crf", str(preset.crf),
                        "-filter_complex",
                        f"[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2,volume={music_track.volume}[aout]",
                        "-map", "0:v", "-map", "[aout]",
                        "-c:a", preset.audio_codec, "-b:a", preset.audio_bitrate,
                        "-threads", str(preset.threads),
                        "-shortest",
                        "-y", str(concat_output),
                    ]

            self.log.info(f"FFmpeg render: {len(normalized)} clips → {output_path}")
            result = self._run_ffmpeg(args)

            if result == 0 and concat_output.exists():
                # Move to final output
                import shutil
                shutil.move(str(concat_output), str(output_path))
                state["output_path"] = str(output_path)
                self.log.info(f"FFmpeg render complete: {output_path}")
            else:
                errors.append(StageError(stage=self.name, message="FFmpeg concat failed", severity=ErrorSeverity.FATAL))
                state["output_path"] = None

        state["errors"] = errors
        return state

    def _run_ffmpeg(self, args: list[str]) -> int:
        """Run FFmpeg command, optionally via E2B sandbox."""
        if self._sandbox:
            result = self._sandbox.execute_ffmpeg(args)
            return result.exit_code

        cmd = ["ffmpeg"] + args
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if proc.returncode != 0:
                self.log.warn(f"FFmpeg stderr: {proc.stderr[-500:]}")
            return proc.returncode
        except subprocess.TimeoutExpired:
            self.log.error("FFmpeg timed out")
            return 1
        except FileNotFoundError:
            self.log.error("ffmpeg not found in PATH")
            return 1

    # ══════════════════════════════════════════
    #  SHARED HELPERS
    # ══════════════════════════════════════════

    def _apply_lower_third_moviepy(self, vclip, title, config):
        """Apply animated lower-third using per-frame OpenCV rendering."""
        from moviepy.editor import VideoClip, CompositeVideoClip

        style = LowerThirdStyle()
        dur = min(title.duration, vclip.duration)

        def make_frame(t):
            base = vclip.get_frame(t)
            if t < dur:
                return render_lower_third(base, title.text, t, dur, style)
            return base

        result = VideoClip(make_frame, duration=vclip.duration)
        result = result.set_fps(vclip.fps)
        if vclip.audio:
            result = result.set_audio(vclip.audio)
        return result

    def _apply_title(self, vclip, title, config):
        from moviepy.editor import CompositeVideoClip, TextClip
        position_map = {"top": ("center", "top"), "center": ("center", "center"), "bottom": ("center", "bottom")}
        pos = position_map.get(title.position, ("center", "bottom"))

        txt = (
            TextClip(title.text, fontsize=config.title.font_size, color=config.title.color,
                     stroke_color=config.title.stroke_color, stroke_width=config.title.stroke_width, font=config.title.font)
            .set_position(pos).set_duration(min(title.duration, vclip.duration)).margin(bottom=config.title.margin_bottom)
        )
        if title.animated:
            txt = txt.fadein(config.title.fade_in)
        return CompositeVideoClip([vclip, txt])

    def _apply_watermark(self, vclip, config):
        from moviepy.editor import CompositeVideoClip, TextClip
        wm = (
            TextClip(config.watermark.text, fontsize=config.watermark.font_size, color="white")
            .set_position(("right", "bottom")).set_duration(vclip.duration)
            .margin(right=config.watermark.margin, bottom=config.watermark.margin)
            .set_opacity(config.watermark.opacity)
        )
        return CompositeVideoClip([vclip, wm])

    def _apply_audio_mix_with_ducking(self, final, audio_mix, config):
        """Apply audio mix with per-region volume ducking."""
        from moviepy.editor import AudioFileClip, CompositeAudioClip
        import numpy as np

        parts = []
        if final.audio:
            parts.append(final.audio)

        for track in audio_mix.tracks:
            try:
                audio = AudioFileClip(track.path)
                if track.loop:
                    audio = audio.loop(duration=final.duration)

                # Apply ducking regions
                if audio_mix.ducking_regions and track.role == "music":
                    regions = audio_mix.ducking_regions

                    def make_volume_filter(regions, base_vol):
                        def volume_filter(get_frame, t):
                            frame = get_frame(t)
                            vol = base_vol
                            for r in regions:
                                if r["start"] <= t <= r["end"]:
                                    if r["type"] == "hold":
                                        vol = r["volume_from"]
                                    elif r["type"] == "ramp_down":
                                        progress = (t - r["start"]) / max(r["end"] - r["start"], 0.001)
                                        vol = r["volume_from"] + (r["volume_to"] - r["volume_from"]) * progress
                                    elif r["type"] == "ramp_up":
                                        progress = (t - r["start"]) / max(r["end"] - r["start"], 0.001)
                                        vol = r["volume_from"] + (r["volume_to"] - r["volume_from"]) * progress
                                    break
                            return frame * vol
                        return volume_filter

                    audio = audio.fl(make_volume_filter(regions, track.volume), apply_to=["audio"])
                else:
                    audio = audio.volumex(track.volume)

                parts.append(audio)
            except Exception as exc:
                self.log.warn(f"Audio track failed: {track.path}: {exc}")

        if parts:
            final = final.set_audio(CompositeAudioClip(parts))
        return final
