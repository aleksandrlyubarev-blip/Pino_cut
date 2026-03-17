"""Romeo Flex Vision API client — Phase 2 full implementation.

Real HTTP API integration with:
- Frame extraction for upload
- Retry with exponential backoff
- Batch analysis
- Response caching (memory + disk)
- Graceful fallback to mock
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pinocut.state import RomeoVisionData
from pinocut.utils.logging import StageLogger

log = StageLogger("romeo_vision")


class VisionProvider(Protocol):
    """Interface for vision metadata providers."""

    def analyze(self, video_path: Path) -> RomeoVisionData: ...
    def analyze_batch(self, video_paths: list[Path]) -> dict[str, RomeoVisionData]: ...
    @property
    def is_configured(self) -> bool: ...


@dataclass
class RomeoVisionConfig:
    """Configuration for Romeo Flex Vision API."""

    api_url: str = field(
        default_factory=lambda: os.getenv("ROMEO_VISION_URL", "http://localhost:8080/api/v1")
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("ROMEO_VISION_KEY", "")
    )
    timeout: int = 30
    batch_size: int = 5
    max_retries: int = 3
    retry_base_delay: float = 1.0
    frames_per_clip: int = 3
    cache_enabled: bool = True
    cache_dir: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "pinocut_romeo_cache"
    )


class RomeoVisionClient:
    """Romeo Flex Vision API client — full implementation.

    Extracts keyframes from clips, sends to Romeo FV for semantic analysis,
    returns structured metadata used by TransitionStage, TitleStage, etc.
    """

    def __init__(self, config: RomeoVisionConfig | None = None):
        self.config = config or RomeoVisionConfig()
        self._cache: dict[str, RomeoVisionData] = {}
        if self.config.cache_enabled:
            self.config.cache_dir.mkdir(parents=True, exist_ok=True)

    @property
    def is_configured(self) -> bool:
        return bool(self.config.api_key)

    def analyze(self, video_path: Path) -> RomeoVisionData:
        """Analyze a single video clip."""
        cache_key = self._cache_key(video_path)

        if cache_key in self._cache:
            return self._cache[cache_key]

        if self.config.cache_enabled:
            cached = self._load_disk_cache(cache_key)
            if cached:
                self._cache[cache_key] = cached
                return cached

        if not self.is_configured:
            return RomeoVisionData()

        try:
            frames = self._extract_keyframes(video_path)
            result = self._call_api(video_path, frames)
            self._cache[cache_key] = result
            if self.config.cache_enabled:
                self._save_disk_cache(cache_key, result)
            log.info(f"Analyzed: {video_path.name} mood={result.mood} score={result.quality_score:.2f}")
            return result
        except Exception as exc:
            log.warn(f"Romeo FV failed for {video_path.name}: {exc}")
            return RomeoVisionData()

    def analyze_batch(self, video_paths: list[Path]) -> dict[str, RomeoVisionData]:
        """Analyze multiple clips, batching API calls."""
        results: dict[str, RomeoVisionData] = {}
        uncached: list[Path] = []

        for path in video_paths:
            ck = self._cache_key(path)
            if ck in self._cache:
                results[str(path)] = self._cache[ck]
            elif self.config.cache_enabled:
                cached = self._load_disk_cache(ck)
                if cached:
                    results[str(path)] = cached
                    self._cache[ck] = cached
                else:
                    uncached.append(path)
            else:
                uncached.append(path)

        if uncached and self.is_configured:
            for i in range(0, len(uncached), self.config.batch_size):
                batch = uncached[i : i + self.config.batch_size]
                batch_results = self._call_batch_api(batch)
                for p, data in batch_results.items():
                    results[p] = data
                    self._cache[self._cache_key(Path(p))] = data

        for path in video_paths:
            if str(path) not in results:
                results[str(path)] = RomeoVisionData()

        return results

    # ── Frame extraction ──

    def _extract_keyframes(self, video_path: Path) -> list[str]:
        """Extract keyframes as base64 JPEG strings."""
        import cv2

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open: {video_path}")

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        duration = frame_count / fps
        n = min(self.config.frames_per_clip, max(1, frame_count))
        timestamps = [duration * i / n for i in range(n)]

        frames_b64: list[str] = []
        for ts in timestamps:
            cap.set(cv2.CAP_PROP_POS_MSEC, ts * 1000)
            ret, frame = cap.read()
            if ret:
                frame = cv2.resize(frame, (640, 360))
                _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                frames_b64.append(base64.b64encode(buf).decode("ascii"))

        cap.release()
        return frames_b64

    # ── API calls ──

    def _call_api(self, video_path: Path, frames_b64: list[str]) -> RomeoVisionData:
        payload = {
            "filename": video_path.name,
            "frames": frames_b64,
            "analysis_types": ["scene_description", "location", "mood", "subjects", "quality", "duration_suggestion"],
        }
        resp = self._request_with_retry(f"{self.config.api_url}/analyze", payload)
        return self._parse_response(resp)

    def _call_batch_api(self, paths: list[Path]) -> dict[str, RomeoVisionData]:
        items = []
        for p in paths:
            try:
                frames = self._extract_keyframes(p)
                items.append({"filename": p.name, "path": str(p), "frames": frames})
            except Exception as exc:
                log.warn(f"Frame extract failed: {p.name}: {exc}")
        if not items:
            return {}
        try:
            resp = self._request_with_retry(f"{self.config.api_url}/analyze/batch", {"items": items})
            return {
                item["path"]: self._parse_response(item)
                for item in resp.get("results", [])
            }
        except Exception:
            return {str(p): self.analyze(p) for p in paths}

    def _request_with_retry(self, url: str, payload: dict) -> dict:
        data = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": "PinoCut/1.0",
        }
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                req = Request(url, data=data, headers=headers, method="POST")
                with urlopen(req, timeout=self.config.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except HTTPError as e:
                last_exc = e
                if e.code in (429, 500, 502, 503, 504):
                    delay = self.config.retry_base_delay * (2**attempt)
                    log.warn(f"HTTP {e.code}, retry {attempt + 1}/{self.config.max_retries} in {delay:.1f}s")
                    time.sleep(delay)
                else:
                    raise
            except (URLError, TimeoutError, OSError) as e:
                last_exc = e
                delay = self.config.retry_base_delay * (2**attempt)
                time.sleep(delay)
        raise RuntimeError(f"API failed after {self.config.max_retries} retries") from last_exc

    def _parse_response(self, data: dict) -> RomeoVisionData:
        return RomeoVisionData(
            scene_description=data.get("scene_description", ""),
            location_tag=data.get("location", data.get("location_tag", "")),
            mood=data.get("mood", ""),
            subjects=data.get("subjects", []),
            suggested_duration=data.get("suggested_duration"),
            quality_score=float(data.get("quality_score", 0.5)),
        )

    # ── Caching ──

    def _cache_key(self, path: Path) -> str:
        stat = path.stat() if path.exists() else None
        raw = f"{path.resolve()}:{stat.st_mtime if stat else 0}:{stat.st_size if stat else 0}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _load_disk_cache(self, key: str) -> RomeoVisionData | None:
        f = self.config.cache_dir / f"{key}.json"
        if f.exists():
            try:
                return self._parse_response(json.loads(f.read_text()))
            except Exception:
                return None
        return None

    def _save_disk_cache(self, key: str, data: RomeoVisionData) -> None:
        try:
            (self.config.cache_dir / f"{key}.json").write_text(json.dumps({
                "scene_description": data.scene_description, "location_tag": data.location_tag,
                "mood": data.mood, "subjects": data.subjects,
                "suggested_duration": data.suggested_duration, "quality_score": data.quality_score,
            }))
        except Exception:
            pass

    def clear_cache(self) -> None:
        self._cache.clear()
        if self.config.cache_enabled and self.config.cache_dir.exists():
            for f in self.config.cache_dir.glob("*.json"):
                f.unlink(missing_ok=True)


class MockVisionProvider:
    """Mock provider for testing."""

    def __init__(self, default_mood: str = "neutral"):
        self._mood = default_mood

    def analyze(self, video_path: Path) -> RomeoVisionData:
        return RomeoVisionData(
            scene_description=f"Scene from {video_path.stem}",
            location_tag="studio", mood=self._mood, quality_score=0.7,
        )

    def analyze_batch(self, video_paths: list[Path]) -> dict[str, RomeoVisionData]:
        return {str(p): self.analyze(p) for p in video_paths}

    @property
    def is_configured(self) -> bool:
        return True
