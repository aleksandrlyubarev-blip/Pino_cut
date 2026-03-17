"""IngestStage: load, validate, and catalog source clips."""

from __future__ import annotations

from pathlib import Path

from pinocut.config import SUPPORTED_VIDEO_EXTENSIONS
from pinocut.stages.base import BaseStage
from pinocut.state import ClipSegment, PinoCutState
from pinocut.utils.errors import ErrorSeverity, StageError


class IngestStage(BaseStage):
    """Scan input folder, probe each file, build ClipSegment list."""

    name = "ingest"

    def process(self, state: PinoCutState) -> PinoCutState:
        config = state["project_config"]
        input_folder = Path(config.input_folder)
        errors: list[StageError] = list(state.get("errors", []))

        if not input_folder.exists():
            errors.append(
                StageError(
                    stage=self.name,
                    message=f"Input folder not found: {input_folder}",
                    severity=ErrorSeverity.FATAL,
                )
            )
            state["errors"] = errors
            state["clips"] = []
            return state

        # Discover video files
        files = sorted(
            f
            for f in input_folder.iterdir()
            if f.is_file() and f.suffix.lower() in SUPPORTED_VIDEO_EXTENSIONS
        )

        if not files:
            errors.append(
                StageError(
                    stage=self.name,
                    message=f"No video files found in {input_folder}",
                    severity=ErrorSeverity.FATAL,
                )
            )
            state["errors"] = errors
            state["clips"] = []
            return state

        # Limit to max_clips
        files = files[: config.max_clips]
        self.log.info(f"Found {len(files)} video files")

        clips: list[ClipSegment] = []
        for filepath in files:
            clip = self._probe_clip(filepath, config, errors)
            if clip is not None:
                clips.append(clip)

        self.log.info(f"Ingested {len(clips)} valid clips")
        state["clips"] = clips
        state["errors"] = errors
        return state

    def _probe_clip(
        self,
        filepath: Path,
        config,
        errors: list[StageError],
    ) -> ClipSegment | None:
        """Probe a single video file and return ClipSegment or None."""
        try:
            from moviepy.editor import VideoFileClip

            with VideoFileClip(str(filepath)) as vclip:
                duration = vclip.duration

                # Skip too-short clips
                if duration < config.min_duration:
                    self.log.debug(f"Skipping {filepath.name}: duration {duration:.1f}s < min")
                    return None

                # Trim if max_duration set
                if config.max_duration and duration > config.max_duration:
                    duration = config.max_duration

                return ClipSegment(
                    path=filepath,
                    duration=duration,
                    has_audio=vclip.audio is not None,
                    resolution=(vclip.w, vclip.h),
                    fps=vclip.fps,
                    metadata={},
                )
        except Exception as exc:
            self.log.warn(f"Failed to probe {filepath.name}: {exc}")
            errors.append(
                StageError(
                    stage=self.name,
                    message=f"Probe failed: {exc}",
                    severity=ErrorSeverity.WARN,
                    clip_path=str(filepath),
                )
            )
            return None
