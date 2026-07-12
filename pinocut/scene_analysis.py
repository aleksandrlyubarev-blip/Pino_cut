"""Real signal extraction for scene clips (S3).

Fills ``ClipSegment.metadata`` with the measurements Andrew's rubric reads,
replacing the empty-metadata defaults that made scoring resolution-only:

- ``quality_score``       0..1 — sharpness (Laplacian variance), normalized
- ``brightness_variance``      — std of per-frame mean luma on a 0-255 scale
- ``shake_score``         0..1 — erratic inter-frame global motion
- ``action_peak_sec``     sec  — the moment of highest visual change

Speech is detected with ffmpeg ``silencedetect`` (non-silence intervals — a
VAD approximation; a Whisper provider can replace it for word-level timing)
and stored on ``ClipSegment.speech_segments`` where smart trim already
looks. When Romeo Flex Vision is configured (``ROMEO_VISION_URL``/``KEY``),
semantic fields (scene description, mood, subjects) are merged in as well.

Analysis is best-effort: missing files, missing OpenCV, or unreadable media
leave the clip untouched so the deterministic fallbacks keep working.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess

from pinocut.state import ClipSegment
from pinocut.utils.logging import StageLogger

ANALYSIS_VERSION = 1

_SILENCE_START = re.compile(r"silence_start:\s*([0-9.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([0-9.]+)")


class SceneClipAnalyzer:
    """Best-effort visual/audio measurement for scene source clips."""

    def __init__(
        self,
        *,
        structured_logs: bool = False,
        sample_points: int = 10,
        analysis_width: int = 320,
    ):
        self.log = StageLogger("scene_analysis", structured=structured_logs)
        self._sample_points = max(2, sample_points)
        self._width = analysis_width
        self._vision_client = None

    def analyze_clips(self, clips: list[ClipSegment]) -> int:
        analyzed = 0
        for clip in clips:
            if self.analyze(clip):
                analyzed += 1
        if analyzed:
            self.log.info(f"Analyzed {analyzed}/{len(clips)} clips with real signals")
        return analyzed

    def analyze(self, clip: ClipSegment) -> bool:
        if clip.metadata.get("analysis_version") == ANALYSIS_VERSION:
            return True
        if not clip.path.exists():
            return False

        visual = self._analyze_visual(clip)
        if visual is None:
            return False
        clip.metadata.update(visual)

        if clip.has_audio and not clip.speech_segments:
            speech = self._detect_speech(clip)
            if speech is not None:
                clip.speech_segments = speech

        self._enrich_with_vision(clip)
        clip.metadata["analysis_version"] = ANALYSIS_VERSION
        return True

    # ── visual signals (OpenCV) ──

    def _analyze_visual(self, clip: ClipSegment) -> dict[str, float] | None:
        try:
            import cv2
            import numpy as np
        except ImportError:
            self.log.warn("OpenCV unavailable; visual analysis skipped")
            return None

        capture = cv2.VideoCapture(str(clip.path))
        if not capture.isOpened():
            return None
        try:
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = capture.get(cv2.CAP_PROP_FPS) or clip.fps or 24.0
            if frame_count < 2:
                return None

            anchors = self._anchor_frames(frame_count)
            brightness: list[float] = []
            sharpness: list[float] = []
            motion_magnitudes: list[float] = []
            change_energy: list[tuple[float, float]] = []  # (energy, time_sec)

            for anchor in anchors:
                capture.set(cv2.CAP_PROP_POS_FRAMES, anchor)
                ok_a, frame_a = capture.read()
                ok_b, frame_b = capture.read()
                if not ok_a:
                    continue
                gray_a = self._to_analysis_gray(cv2, frame_a)
                brightness.append(float(gray_a.mean()))
                sharpness.append(float(cv2.Laplacian(gray_a, cv2.CV_64F).var()))
                if not ok_b:
                    continue
                gray_b = self._to_analysis_gray(cv2, frame_b)
                shift, _ = cv2.phaseCorrelate(
                    gray_a.astype(np.float32), gray_b.astype(np.float32)
                )
                motion_magnitudes.append(math.hypot(shift[0], shift[1]))
                energy = float(cv2.absdiff(gray_a, gray_b).mean())
                change_energy.append((energy, anchor / fps))

            if not brightness or not sharpness:
                return None

            median_sharpness = float(np.median(sharpness))
            quality_score = round(min(1.0, median_sharpness / 400.0), 3)
            brightness_variance = round(float(np.std(brightness)), 2)
            # Smooth pans move consistently; shake shows up as jitter around
            # the mean displacement between adjacent frames.
            if len(motion_magnitudes) >= 2:
                shake_score = round(min(1.0, float(np.std(motion_magnitudes)) / 8.0), 3)
            else:
                shake_score = 0.0

            result: dict[str, float] = {
                "quality_score": quality_score,
                "brightness_variance": brightness_variance,
                "shake_score": shake_score,
            }
            if change_energy:
                peak_energy, peak_time = max(change_energy, key=lambda item: item[0])
                if peak_energy > 0:
                    result["action_peak_sec"] = round(peak_time, 3)
            return result
        finally:
            capture.release()

    def _anchor_frames(self, frame_count: int) -> list[int]:
        step = max(1, (frame_count - 2) // self._sample_points)
        return list(range(0, max(1, frame_count - 2), step))[: self._sample_points]

    def _to_analysis_gray(self, cv2, frame):
        height, width = frame.shape[:2]
        if width > self._width:
            scale = self._width / width
            frame = cv2.resize(frame, (self._width, max(2, int(height * scale))))
        return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # ── speech (ffmpeg silencedetect) ──

    def _detect_speech(self, clip: ClipSegment) -> list[tuple[float, float]] | None:
        if shutil.which("ffmpeg") is None:
            return None
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-nostats",
                    "-i", str(clip.path),
                    "-af", "silencedetect=noise=-35dB:d=0.4",
                    "-f", "null", "-",
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        if proc.returncode != 0:
            return None

        silences: list[tuple[float, float]] = []
        pending_start: float | None = None
        for line in proc.stderr.splitlines():
            start_match = _SILENCE_START.search(line)
            if start_match:
                pending_start = float(start_match.group(1))
                continue
            end_match = _SILENCE_END.search(line)
            if end_match and pending_start is not None:
                silences.append((pending_start, float(end_match.group(1))))
                pending_start = None
        if pending_start is not None:
            silences.append((pending_start, clip.duration))

        # Invert silences into non-silent (speech-like) intervals.
        segments: list[tuple[float, float]] = []
        cursor = 0.0
        for silence_start, silence_end in sorted(silences):
            if silence_start - cursor >= 0.2:
                segments.append((round(cursor, 3), round(silence_start, 3)))
            cursor = max(cursor, silence_end)
        if clip.duration - cursor >= 0.2:
            segments.append((round(cursor, 3), round(clip.duration, 3)))
        return segments

    # ── semantic enrichment (Romeo Flex Vision, optional) ──

    def _enrich_with_vision(self, clip: ClipSegment) -> None:
        client = self._get_vision_client()
        if client is None:
            return
        try:
            data = client.analyze(clip.path)
        except Exception as exc:
            self.log.warn(f"Romeo Vision enrichment failed for {clip.path.name}: {exc}")
            return
        if data.scene_description:
            clip.metadata.setdefault("scene_description", data.scene_description)
        if data.location_tag:
            clip.metadata.setdefault("location_tag", data.location_tag)
        if data.mood:
            clip.metadata.setdefault("mood", data.mood)
        if data.subjects:
            clip.metadata.setdefault("subjects", list(data.subjects))

    def _get_vision_client(self):
        if self._vision_client is not None:
            return self._vision_client if self._vision_client is not False else None
        try:
            from pinocut.integrations.romeo_vision import RomeoVisionClient

            client = RomeoVisionClient()
            if client.is_configured():
                self._vision_client = client
                return client
        except Exception:
            pass
        self._vision_client = False
        return None
