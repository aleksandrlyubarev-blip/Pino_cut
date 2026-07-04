"""SceneRenderService: real media rendering for Pinnocat scene timelines.

Renders a TimelineV1 into an mp4 through ffmpeg subprocess calls:

1. Normalize every timeline segment (trim window, scale/pad, fps, audio
   rate) into an intermediate clip so downstream filters get uniform input.
2. Join intermediates — concat demuxer when every junction is a hard cut,
   otherwise a single filter_complex chain mixing concat and xfade/acrossfade.
3. Overlay timeline audio tracks (music, voiceover) with per-track gain and
   music ducking under voiceover.

The service degrades gracefully: when ffmpeg is missing or a source clip
does not exist on disk, ``render`` returns ``None`` and the caller keeps the
JSON-only export path that Scene Stitcher v1 shipped with.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pinocut.state import ExportProfile, TimelineSegment, TimelineV1
from pinocut.utils.logging import StageLogger

# xfade transition names for TransitionSpec.type values that need a timed blend
_XFADE_BY_TRANSITION = {
    "crossfade": "fade",
    "dip_to_black": "fadeblack",
    "match_cut": "fade",
}

_PREVIEW_MAX_WIDTH = 854
_PREVIEW_MAX_HEIGHT = 480


@dataclass(slots=True)
class RenderSettings:
    width: int
    height: int
    fps: int
    crf: int
    preset: str
    audio_rate: int = 48000

    @classmethod
    def for_profile(cls, profile: ExportProfile, *, preview: bool = False) -> RenderSettings:
        width, height = _parse_resolution(profile.resolution)
        if preview:
            scale = min(_PREVIEW_MAX_WIDTH / width, _PREVIEW_MAX_HEIGHT / height, 1.0)
            width = _even(int(width * scale))
            height = _even(int(height * scale))
            return cls(width=width, height=height, fps=profile.fps, crf=32, preset="ultrafast")
        return cls(width=width, height=height, fps=profile.fps, crf=20, preset="veryfast")


def _parse_resolution(resolution: str) -> tuple[int, int]:
    try:
        width_text, height_text = resolution.lower().split("x", 1)
        return max(2, int(width_text)), max(2, int(height_text))
    except ValueError:
        return 1920, 1080


def _even(value: int) -> int:
    return max(2, value - (value % 2))


def _segment_duration(segment: TimelineSegment) -> float:
    return max(0.0, segment.source_out_sec - segment.source_in_sec)


class SceneRenderService:
    """Render a TimelineV1 to video via ffmpeg subprocess."""

    def __init__(
        self,
        *,
        structured_logs: bool = False,
        ffmpeg_bin: str = "ffmpeg",
        timeout_sec: int = 600,
    ):
        self.log = StageLogger("scene_render", structured=structured_logs)
        self._ffmpeg = ffmpeg_bin
        self._timeout = timeout_sec

    def is_available(self) -> bool:
        return shutil.which(self._ffmpeg) is not None

    def render(
        self,
        timeline: TimelineV1,
        clip_paths: dict[str, Path],
        output_path: Path,
        *,
        preview: bool = False,
        work_dir: Path | None = None,
    ) -> Path | None:
        """Render the timeline; return the output path or None when skipped."""
        segments = timeline.video_segments
        if not segments:
            self.log.warn("Timeline has no video segments; render skipped")
            return None
        if not self.is_available():
            self.log.warn("ffmpeg not found in PATH; render skipped")
            return None

        missing = [
            segment.clip_id
            for segment in segments
            if segment.clip_id not in clip_paths or not clip_paths[segment.clip_id].exists()
        ]
        if missing:
            self.log.warn(f"Source clips missing on disk, render skipped: {', '.join(missing)}")
            return None

        settings = RenderSettings.for_profile(timeline.export_profile, preview=preview)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="pinocut_scene_") as tmpdir:
            tmp = work_dir or Path(tmpdir)
            tmp.mkdir(parents=True, exist_ok=True)

            normalized: list[Path] = []
            for index, segment in enumerate(segments):
                intermediate = tmp / f"seg_{index:03d}.mp4"
                args = self.build_normalize_args(
                    segment, clip_paths[segment.clip_id], intermediate, settings
                )
                if self._run(args) != 0:
                    self.log.warn(f"Segment normalize failed: {segment.clip_id}; render aborted")
                    return None
                normalized.append(intermediate)

            joined = tmp / "joined.mp4"
            args = self.build_join_args(normalized, segments, joined, settings, work_dir=tmp)
            if self._run(args) != 0:
                self.log.warn("Timeline join failed; render aborted")
                return None

            final_source = joined
            audio_args = self.build_audio_overlay_args(
                joined, timeline, tmp / "mixed.mp4"
            )
            if audio_args is not None:
                if self._run(audio_args) != 0:
                    self.log.warn("Audio overlay failed; exporting video without music mix")
                else:
                    final_source = tmp / "mixed.mp4"

            shutil.move(str(final_source), str(output_path))

        self.log.info(f"Rendered scene video: {output_path}")
        return output_path

    # ── command builders (pure, unit-testable) ──

    def build_normalize_args(
        self,
        segment: TimelineSegment,
        source: Path,
        output: Path,
        settings: RenderSettings,
    ) -> list[str]:
        """Trim + conform one segment to the shared render profile.

        Audio is always produced: source audio is resampled, and a silent
        anullsrc bed keeps concat/acrossfade graphs valid for mute clips.
        """
        duration = _segment_duration(segment)
        vf = (
            f"scale={settings.width}:{settings.height}:force_original_aspect_ratio=decrease,"
            f"pad={settings.width}:{settings.height}:(ow-iw)/2:(oh-ih)/2,"
            f"fps={settings.fps},format=yuv420p,setsar=1"
        )
        af = f"aresample={settings.audio_rate},apad"
        return [
            "-ss", f"{segment.source_in_sec:.3f}",
            "-t", f"{duration:.3f}",
            "-i", str(source),
            "-f", "lavfi",
            "-t", f"{duration:.3f}",
            "-i", f"anullsrc=r={settings.audio_rate}:cl=stereo",
            "-filter_complex",
            (
                f"[0:v]{vf}[v];"
                f"[0:a]{af}[a0];"
                f"[a0][1:a]amix=inputs=2:duration=first[a]"
            ),
            "-map", "[v]",
            "-map", "[a]",
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", str(settings.crf + 3),
            "-c:a", "aac",
            "-b:a", "160k",
            "-ac", "2",
            "-y", str(output),
        ]

    def build_join_args(
        self,
        files: list[Path],
        segments: list[TimelineSegment],
        output: Path,
        settings: RenderSettings,
        *,
        work_dir: Path,
    ) -> list[str]:
        """Join normalized segments honoring per-junction transitions."""
        if len(files) == 1:
            return ["-i", str(files[0]), "-c", "copy", "-y", str(output)]

        if not self._has_timed_transitions(segments):
            concat_file = work_dir / "concat.txt"
            concat_file.write_text(
                "\n".join(f"file '{path.resolve()}'" for path in files),
                encoding="utf-8",
            )
            return [
                "-f", "concat", "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                "-y", str(output),
            ]

        args: list[str] = []
        for path in files:
            args.extend(["-i", str(path)])
        filters, video_label, audio_label = self.build_join_filtergraph(segments)
        args.extend([
            "-filter_complex", ";".join(filters),
            "-map", video_label,
            "-map", audio_label,
            "-c:v", "libx264",
            "-preset", settings.preset,
            "-crf", str(settings.crf),
            "-c:a", "aac",
            "-b:a", "192k",
            "-y", str(output),
        ])
        return args

    def build_join_filtergraph(
        self,
        segments: list[TimelineSegment],
    ) -> tuple[list[str], str, str]:
        """Chain segments with concat (cuts) and xfade/acrossfade (blends)."""
        filters: list[str] = []
        # xfade rejects inputs with mismatched timebases; conform every video
        # stream to AVTB before it enters the chain.
        for index in range(len(segments)):
            filters.append(f"[{index}:v]settb=AVTB[sv{index}]")
        video_label = "[sv0]"
        audio_label = "[0:a]"
        chain_duration = _segment_duration(segments[0])

        for index in range(1, len(segments)):
            segment = segments[index]
            transition = segment.transition_in
            xfade = _XFADE_BY_TRANSITION.get(transition.type) if transition else None
            blend = transition.duration_sec if transition else 0.0
            next_video = f"[v{index}]"
            next_audio = f"[a{index}]"
            segment_duration = _segment_duration(segment)

            if xfade and blend > 0:
                offset = max(0.0, chain_duration - blend)
                filters.append(
                    f"{video_label}[sv{index}]xfade=transition={xfade}"
                    f":duration={blend:.3f}:offset={offset:.3f}{next_video}"
                )
                filters.append(
                    f"{audio_label}[{index}:a]acrossfade=d={blend:.3f}{next_audio}"
                )
                chain_duration += segment_duration - blend
            else:
                filters.append(
                    f"{video_label}[sv{index}]concat=n=2:v=1:a=0{next_video}"
                )
                filters.append(
                    f"{audio_label}[{index}:a]concat=n=2:v=0:a=1{next_audio}"
                )
                chain_duration += segment_duration

            video_label = next_video
            audio_label = next_audio

        return filters, video_label, audio_label

    def build_audio_overlay_args(
        self,
        video_in: Path,
        timeline: TimelineV1,
        output: Path,
    ) -> list[str] | None:
        """Mix timeline audio tracks over the joined video; None when no tracks."""
        tracks = [
            track
            for track in timeline.audio_segments
            if track.track_id and Path(track.track_id).exists()
        ]
        if not tracks:
            return None

        ducked = any(track.duck_music for track in tracks if track.role == "voiceover")
        args: list[str] = ["-i", str(video_in)]
        filters: list[str] = []
        mix_labels = ["[0:a]"]

        for index, track in enumerate(tracks, start=1):
            if track.role == "music":
                args.extend(["-stream_loop", "-1"])
            args.extend(["-i", str(track.track_id)])
            gain = 10 ** (track.gain_db / 20)
            if track.role == "music" and ducked:
                gain *= 0.5
            filters.append(f"[{index}:a]volume={gain:.4f}[mix{index}]")
            mix_labels.append(f"[mix{index}]")

        filters.append(
            "".join(mix_labels)
            + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=2[aout]"
        )
        args.extend([
            "-filter_complex", ";".join(filters),
            "-map", "0:v",
            "-map", "[aout]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-t", f"{timeline.actual_duration_sec:.3f}",
            "-y", str(output),
        ])
        return args

    # ── execution ──

    def _has_timed_transitions(self, segments: list[TimelineSegment]) -> bool:
        for segment in segments:
            for transition in (segment.transition_in, segment.transition_out):
                if (
                    transition
                    and transition.duration_sec > 0
                    and transition.type in _XFADE_BY_TRANSITION
                ):
                    return True
        return False

    def _run(self, args: list[str]) -> int:
        cmd = [self._ffmpeg, "-hide_banner", "-loglevel", "error", *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self._timeout)
            if proc.returncode != 0:
                self.log.warn(f"ffmpeg failed: {proc.stderr[-500:]}")
            return proc.returncode
        except subprocess.TimeoutExpired:
            self.log.error("ffmpeg timed out")
            return 1
        except FileNotFoundError:
            self.log.error(f"{self._ffmpeg} not found in PATH")
            return 1
