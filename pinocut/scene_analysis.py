"""Content-based clip analysis for the scene pipeline (goal plan G3.1).

Fills ``ClipSegment.metadata`` with the real signals Andrew's rubric and
``smart_trim_clip`` consume — instead of the defaults they fall back to when
probe leaves metadata empty:

- ``quality_score`` (0..1): sharpness (variance of Laplacian) blended with an
  exposure-clipping penalty.
- ``brightness_variance``: std of per-frame mean luma (0-255 scale; the rubric
  penalizes values > 40 as unstable lighting).
- ``shake_score`` (0..1): camera jitter — deviation of inter-frame global
  translation (cv2.phaseCorrelate) from the mean motion vector, so a smooth
  pan scores low while handheld wobble scores high.
- ``action_peak_sec``: timestamp of the strongest inter-frame motion, used by
  smart trim to center the cut window on the action.

Speech segments come from an energy VAD over mono 16 kHz audio, extracted via
moviepy when available, else via an ffmpeg subprocess. Everything degrades
gracefully: only OpenCV (a core dependency) is required for the visual pass,
and files that cannot be opened leave the clip untouched.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from pinocut.state import ClipSegment
from pinocut.utils.logging import StageLogger

# Calibration constants (see tests/test_scene_analysis.py for the fixtures
# they were tuned against).
SHARPNESS_NORM = 300.0        # Laplacian variance of a crisp frame
CLIP_FRACTION_PENALTY = 4.0   # weight of blown/crushed pixel fraction
SHAKE_NORM_FRACTION = 0.02    # jitter (px) per downscale width that maps to 1.0
MIN_ACTION_MOTION_PX = 0.5    # below this the clip has no action peak


def energy_vad(
    audio: np.ndarray,
    threshold: float,
    *,
    window_sec: float = 0.5,
    sr: int = 16000,
) -> list[tuple[float, float]]:
    """RMS-energy voice activity detection over a mono waveform."""
    window = int(window_sec * sr)
    segments: list[tuple[float, float]] = []
    in_speech = False
    start = 0.0

    for i in range(0, len(audio) - window, window):
        rms = float(np.sqrt(np.mean(audio[i : i + window] ** 2)))
        if rms > threshold and not in_speech:
            in_speech = True
            start = i / sr
        elif rms <= threshold and in_speech:
            in_speech = False
            segments.append((start, i / sr))

    if in_speech:
        segments.append((start, len(audio) / sr))
    return segments


@dataclass(slots=True)
class ClipContentMetrics:
    quality_score: float
    brightness_variance: float
    shake_score: float
    sharpness: float
    action_peak_sec: float | None = None
    speech_segments: list[tuple[float, float]] = field(default_factory=list)


class SceneClipAnalyzer:
    """Deterministic OpenCV-based content metrics for scene clip scoring."""

    def __init__(
        self,
        *,
        samples: int = 12,
        downscale_width: int = 256,
        vad_threshold: float = 0.015,
        structured_logs: bool = False,
    ):
        self.samples = samples
        self.downscale_width = downscale_width
        self.vad_threshold = vad_threshold
        self.log = StageLogger("scene_analysis", structured=structured_logs)

    # ── public API ─────────────────────────────────────────────────────

    def annotate_clip(self, clip: ClipSegment) -> bool:
        """Fill clip.metadata with content metrics; never clobber existing keys.

        Returns True when analysis ran, False when the file was unreadable.
        """
        metrics = self.analyze(clip.path)
        if metrics is None:
            return False

        computed = {
            "quality_score": metrics.quality_score,
            "brightness_variance": metrics.brightness_variance,
            "shake_score": metrics.shake_score,
            "sharpness": metrics.sharpness,
        }
        if metrics.action_peak_sec is not None:
            computed["action_peak_sec"] = metrics.action_peak_sec
        for key, value in computed.items():
            clip.metadata.setdefault(key, value)
        clip.metadata.setdefault("content_analysis", "opencv-v1")

        if metrics.speech_segments and not clip.speech_segments:
            clip.speech_segments = metrics.speech_segments
        return True

    def analyze(self, path: Path) -> ClipContentMetrics | None:
        visual = self._analyze_visual(path)
        if visual is None:
            return None
        visual.speech_segments = self._analyze_speech(path)
        return visual

    # ── visual pass (OpenCV only) ──────────────────────────────────────

    def _analyze_visual(self, path: Path) -> ClipContentMetrics | None:
        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                return None
            frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
            if frame_count < 2 or fps <= 0:
                return None

            brightness: list[float] = []
            sharpness: list[float] = []
            clipped: list[float] = []
            shifts: list[tuple[float, float]] = []
            motion: list[tuple[float, float]] = []  # (time_sec, magnitude)

            positions = self._sample_positions(frame_count)
            for frame_index in positions:
                pair = self._read_pair(capture, frame_index)
                if pair is None:
                    continue
                first, second = pair

                brightness.append(float(np.mean(first)))
                sharpness.append(float(cv2.Laplacian(first, cv2.CV_64F).var()))
                clipped.append(
                    float(np.mean((first < 8) | (first > 247)))
                )
                (dx, dy), _response = cv2.phaseCorrelate(
                    first.astype(np.float32), second.astype(np.float32)
                )
                shifts.append((dx, dy))
                magnitude = float(np.hypot(dx, dy))
                diff = float(np.mean(np.abs(second.astype(np.int16) - first)))
                motion.append((frame_index / fps, magnitude + diff / 8.0))

            if not brightness or not shifts:
                return None

            return ClipContentMetrics(
                quality_score=self._quality_score(sharpness, clipped),
                brightness_variance=round(float(np.std(brightness)), 2),
                shake_score=self._shake_score(shifts),
                sharpness=round(float(np.median(sharpness)), 2),
                action_peak_sec=self._action_peak(motion),
            )
        finally:
            capture.release()

    def _sample_positions(self, frame_count: int) -> list[int]:
        # Skip the first/last 2% (slates, fade tails); need frame i and i+1.
        usable = max(0, frame_count - 2)
        margin = int(usable * 0.02)
        count = min(self.samples, usable - margin * 2) or 1
        return sorted(
            {int(p) for p in np.linspace(margin, usable - margin, count)}
        )

    def _read_pair(
        self, capture: cv2.VideoCapture, frame_index: int
    ) -> tuple[np.ndarray, np.ndarray] | None:
        capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok_first, first = capture.read()
        ok_second, second = capture.read()
        if not ok_first or not ok_second:
            return None
        return self._to_gray(first), self._to_gray(second)

    def _to_gray(self, frame: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        height = int(gray.shape[0] * self.downscale_width / gray.shape[1])
        return cv2.resize(gray, (self.downscale_width, max(2, height)))

    def _quality_score(self, sharpness: list[float], clipped: list[float]) -> float:
        sharp_norm = min(1.0, float(np.median(sharpness)) / SHARPNESS_NORM)
        exposure = 1.0 - min(
            1.0, CLIP_FRACTION_PENALTY * float(np.median(clipped))
        )
        return round(0.6 * sharp_norm + 0.4 * exposure, 3)

    def _shake_score(self, shifts: list[tuple[float, float]]) -> float:
        vectors = np.array(shifts, dtype=np.float64)
        mean_vector = vectors.mean(axis=0)
        jitter = float(np.mean(np.linalg.norm(vectors - mean_vector, axis=1)))
        return round(min(1.0, jitter / (SHAKE_NORM_FRACTION * self.downscale_width)), 3)

    def _action_peak(self, motion: list[tuple[float, float]]) -> float | None:
        peak_time, peak_value = max(motion, key=lambda item: item[1])
        if peak_value < MIN_ACTION_MOTION_PX:
            return None
        return round(peak_time, 3)

    # ── speech pass (moviepy → ffmpeg → skip) ──────────────────────────

    def _analyze_speech(self, path: Path) -> list[tuple[float, float]]:
        audio = self._extract_audio_mono16k(path)
        if audio is None or not len(audio):
            return []
        return energy_vad(audio, self.vad_threshold)

    def _extract_audio_mono16k(self, path: Path) -> np.ndarray | None:
        try:
            from moviepy.editor import VideoFileClip

            with VideoFileClip(str(path)) as vclip:
                if vclip.audio is None:
                    return None
                audio = vclip.audio.to_soundarray(fps=16000)
                if audio.ndim > 1:
                    audio = np.mean(audio, axis=1)
                return audio.astype(np.float64)
        except Exception:
            pass

        if shutil.which("ffmpeg") is None:
            return None
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
                proc = subprocess.run(
                    [
                        "ffmpeg", "-y", "-v", "error",
                        "-i", str(path),
                        "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "wav", tmp.name,
                    ],
                    capture_output=True,
                    timeout=120,
                )
                if proc.returncode != 0:
                    return None
                with wave.open(tmp.name, "rb") as wav:
                    raw = wav.readframes(wav.getnframes())
                samples = np.frombuffer(raw, dtype=np.int16)
                return samples.astype(np.float64) / 32768.0
        except Exception:
            self.log.debug(f"Audio extraction failed for {path.name}")
            return None
