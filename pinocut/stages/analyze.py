"""AnalyzeStage: speech detection, keyframe histograms, Romeo FV enrichment.

Phase 2 upgrades:
- Parallel clip analysis via MoltisBridge.scheduler
- Real Romeo Flex Vision API integration
- Analysis result caching in Moltis memory
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from pinocut.integrations.moltis_bridge import MoltisBridge
from pinocut.integrations.romeo_vision import RomeoVisionClient, RomeoVisionConfig
from pinocut.stages.base import BaseStage
from pinocut.state import ClipAnalysis, ClipSegment, PinoCutState, RomeoVisionData
from pinocut.utils.errors import ErrorSeverity, StageError

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False


class AnalyzeStage(BaseStage):
    """Analyze each clip: speech detection, keyframe histograms, metadata enrichment.

    Uses MoltisBridge for parallel analysis and RomeoVisionClient for
    semantic metadata enrichment.
    """

    name = "analyze"

    def __init__(
        self,
        *,
        structured_logs: bool = False,
        moltis: MoltisBridge | None = None,
        vision_client: RomeoVisionClient | None = None,
    ):
        super().__init__(structured_logs=structured_logs)
        self._whisper = None
        self._moltis = moltis
        self._vision = vision_client

    def _get_whisper(self, config):
        if not WHISPER_AVAILABLE:
            return None
        if self._whisper is None:
            dc = config.audio_ducking
            self._whisper = WhisperModel(dc.whisper_model, device=dc.whisper_device, compute_type=dc.whisper_compute_type)
            self.log.info("Whisper model loaded")
        return self._whisper

    def process(self, state: PinoCutState) -> PinoCutState:
        clips = state.get("clips", [])
        config = state["project_config"]
        errors: list[StageError] = list(state.get("errors", []))

        whisper = self._get_whisper(config)

        # ── Parallel analysis via Moltis ──
        if self._moltis and len(clips) > 1:
            self.log.info(f"Parallel analysis: {len(clips)} clips")
            analysis_results = self._moltis.run_parallel(
                lambda clip: self._safe_analyze(clip, whisper, config),
                clips,
            )
        else:
            analysis_results = [self._safe_analyze(c, whisper, config) for c in clips]

        analysis: dict[str, ClipAnalysis] = {}
        for clip, result in zip(clips, analysis_results):
            key = str(clip.path)
            if result is None:
                errors.append(StageError(stage=self.name, message="Analysis failed", severity=ErrorSeverity.WARN, clip_path=key))
                analysis[key] = ClipAnalysis(clip_path=key)
            else:
                analysis[key] = result
                clip.speech_segments = result.speech_segments

        # ── Romeo Flex Vision enrichment ──
        romeo_data = self._enrich_all(clips, config)

        self.log.info(f"Analyzed {len(analysis)} clips ({sum(1 for a in analysis.values() if a.speech_segments)} with speech)")
        state["analysis"] = analysis
        state["romeo_data"] = romeo_data
        state["errors"] = errors
        return state

    def _safe_analyze(self, clip: ClipSegment, whisper, config) -> ClipAnalysis | None:
        """Analyze a clip, catching all exceptions."""
        # Check Moltis memory cache
        if self._moltis:
            cache_key = f"analysis:{clip.path.name}:{clip.duration}"
            cached = self._moltis.memory.get(cache_key)
            if cached and isinstance(cached, dict):
                self.log.debug(f"Cache hit: {clip.path.name}")
                return ClipAnalysis(**cached)

        try:
            result = self._analyze_clip(clip, whisper, config)
            # Cache in Moltis memory
            if self._moltis:
                self._moltis.memory.set(cache_key, {
                    "clip_path": result.clip_path,
                    "speech_segments": result.speech_segments,
                    "first_frame_hist": result.first_frame_hist,
                    "last_frame_hist": result.last_frame_hist,
                    "avg_brightness": result.avg_brightness,
                })
            return result
        except Exception as exc:
            self.log.warn(f"Analysis failed: {clip.path.name}: {exc}")
            return None

    def _analyze_clip(self, clip: ClipSegment, whisper, config) -> ClipAnalysis:
        import cv2
        from moviepy.editor import VideoFileClip

        result = ClipAnalysis(clip_path=str(clip.path))

        with VideoFileClip(str(clip.path)) as vclip:
            if clip.has_audio:
                result.speech_segments = self._detect_speech(vclip, whisper, config)

            result.first_frame_hist = self._frame_histogram(vclip, t=0.05)
            result.last_frame_hist = self._frame_histogram(vclip, t=max(0.05, vclip.duration - 0.05))

            mid_frame = vclip.get_frame(vclip.duration / 2)
            gray = cv2.cvtColor(mid_frame, cv2.COLOR_RGB2GRAY)
            result.avg_brightness = float(np.mean(gray))

        return result

    def _detect_speech(self, vclip, whisper, config) -> list[tuple[float, float]]:
        audio_array = vclip.audio.to_soundarray(fps=16000)
        if len(audio_array.shape) > 1:
            audio_array = np.mean(audio_array, axis=1)

        if whisper is not None:
            try:
                segments, _ = whisper.transcribe(audio_array, beam_size=5, vad_filter=True)
                result = [(s.start, s.end) for s in segments if s.start < s.end]
                if result:
                    return result
            except Exception:
                self.log.debug("Whisper failed, falling back to energy VAD")

        return self._energy_vad(audio_array, config.audio_ducking.energy_vad_threshold)

    def _energy_vad(self, audio: np.ndarray, threshold: float, window_sec: float = 0.5, sr: int = 16000) -> list[tuple[float, float]]:
        from pinocut.scene_analysis import energy_vad

        return energy_vad(audio, threshold, window_sec=window_sec, sr=sr)

    def _frame_histogram(self, vclip, t: float) -> list[float]:
        import cv2
        frame = vclip.get_frame(t)
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        gray = cv2.resize(gray, (64, 64))
        hist = cv2.calcHist([gray], [0], None, [64], [0, 256])
        cv2.normalize(hist, hist)
        return hist.flatten().tolist()

    def _enrich_all(self, clips: list[ClipSegment], config) -> dict[str, RomeoVisionData]:
        """Enrich all clips with Romeo FV metadata."""
        paths = [c.path for c in clips]

        if self._vision and self._vision.is_configured:
            self.log.info("Enriching via Romeo Flex Vision API")
            return self._vision.analyze_batch(paths)

        # Fallback: use clip metadata
        result = {}
        for clip in clips:
            result[str(clip.path)] = RomeoVisionData(
                scene_description=clip.metadata.get("title", ""),
                location_tag=clip.metadata.get("location", ""),
                mood=clip.metadata.get("mood", ""),
                subjects=clip.metadata.get("subjects", []),
                quality_score=clip.metadata.get("quality_score", 0.5),
            )
        return result
