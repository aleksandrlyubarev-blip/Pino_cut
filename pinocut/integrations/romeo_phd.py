"""Romeo PhD Analytics — pipeline performance tracking and reporting.

Collects metrics from each stage, generates performance reports,
and provides data for the Romeo PhD companion agent.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pinocut.utils.logging import StageLogger

log = StageLogger("romeo_phd")


@dataclass(slots=True)
class StageMetric:
    """Performance metric for a single stage execution."""

    stage: str
    start_time: float
    end_time: float = 0.0
    clips_processed: int = 0
    errors: int = 0
    warnings: int = 0
    details: dict = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000

    @property
    def duration_human(self) -> str:
        d = self.duration_ms
        if d < 1000:
            return f"{d:.0f}ms"
        return f"{d / 1000:.1f}s"


@dataclass(slots=True)
class PipelineMetrics:
    """Aggregated metrics for a full pipeline run."""

    run_id: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    total_clips: int = 0
    output_duration: float = 0.0
    output_file_size: int = 0
    render_preset: str = ""
    stages: list[StageMetric] = field(default_factory=list)
    success: bool = False

    @property
    def total_duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000

    @property
    def realtime_ratio(self) -> float:
        """How many seconds of video per second of processing."""
        if self.total_duration_ms <= 0:
            return 0.0
        return self.output_duration / (self.total_duration_ms / 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "total_duration_ms": round(self.total_duration_ms),
            "total_clips": self.total_clips,
            "output_duration_sec": round(self.output_duration, 1),
            "output_file_size_mb": round(self.output_file_size / 1024 / 1024, 2),
            "render_preset": self.render_preset,
            "realtime_ratio": round(self.realtime_ratio, 2),
            "success": self.success,
            "stages": [
                {
                    "stage": s.stage,
                    "duration_ms": round(s.duration_ms),
                    "clips_processed": s.clips_processed,
                    "errors": s.errors,
                    "warnings": s.warnings,
                    **s.details,
                }
                for s in self.stages
            ],
        }


class MetricsCollector:
    """Collects and aggregates pipeline metrics.

    Usage:
        collector = MetricsCollector()
        collector.start_pipeline("run_001")
        with collector.track_stage("ingest") as m:
            m.clips_processed = 30
        collector.end_pipeline(success=True)
        report = collector.report()
    """

    def __init__(self, *, persist_path: Path | None = None):
        self._metrics = PipelineMetrics()
        self._current_stage: StageMetric | None = None
        self._persist_path = persist_path

    def start_pipeline(self, run_id: str = "") -> None:
        """Mark pipeline start."""
        self._metrics = PipelineMetrics(
            run_id=run_id or f"run_{int(time.time())}",
            start_time=time.perf_counter(),
        )

    def end_pipeline(
        self,
        *,
        success: bool,
        output_path: Path | None = None,
        output_duration: float = 0.0,
        total_clips: int = 0,
        render_preset: str = "",
    ) -> None:
        """Mark pipeline end and finalize metrics."""
        self._metrics.end_time = time.perf_counter()
        self._metrics.success = success
        self._metrics.output_duration = output_duration
        self._metrics.total_clips = total_clips
        self._metrics.render_preset = render_preset

        if output_path and output_path.exists():
            self._metrics.output_file_size = output_path.stat().st_size

        if self._persist_path:
            self._save(self._persist_path)

        self._log_summary()

    def start_stage(self, name: str) -> StageMetric:
        """Start tracking a stage."""
        metric = StageMetric(stage=name, start_time=time.perf_counter())
        self._current_stage = metric
        return metric

    def end_stage(self, metric: StageMetric) -> None:
        """Finish tracking a stage."""
        metric.end_time = time.perf_counter()
        self._metrics.stages.append(metric)
        self._current_stage = None

    class _StageContext:
        def __init__(self, collector: MetricsCollector, name: str):
            self._collector = collector
            self._name = name
            self._metric: StageMetric | None = None

        def __enter__(self) -> StageMetric:
            self._metric = self._collector.start_stage(self._name)
            return self._metric

        def __exit__(self, *args):
            if self._metric:
                self._collector.end_stage(self._metric)

    def track_stage(self, name: str) -> _StageContext:
        """Context manager for stage tracking."""
        return self._StageContext(self, name)

    def report(self) -> dict[str, Any]:
        """Generate full pipeline report."""
        return self._metrics.to_dict()

    def report_text(self) -> str:
        """Generate human-readable text report."""
        m = self._metrics
        lines = [
            f"{'=' * 50}",
            f"  PinoCut Pipeline Report — {m.run_id}",
            f"{'=' * 50}",
            f"  Status:         {'SUCCESS' if m.success else 'FAILED'}",
            f"  Total time:     {m.total_duration_ms / 1000:.1f}s",
            f"  Clips:          {m.total_clips}",
            f"  Output:         {m.output_duration:.1f}s video",
            f"  File size:      {m.output_file_size / 1024 / 1024:.1f} MB",
            f"  Realtime ratio: {m.realtime_ratio:.2f}x",
            f"  Preset:         {m.render_preset}",
            f"{'─' * 50}",
            f"  Stage Breakdown:",
        ]
        for s in m.stages:
            status = f" ({s.errors}err)" if s.errors else ""
            lines.append(f"    {s.stage:<16} {s.duration_human:>8}  {s.clips_processed} clips{status}")
        lines.append(f"{'=' * 50}")
        return "\n".join(lines)

    @property
    def metrics(self) -> PipelineMetrics:
        return self._metrics

    def _log_summary(self) -> None:
        m = self._metrics
        log.info(
            f"Pipeline {'OK' if m.success else 'FAILED'}: "
            f"{m.total_duration_ms / 1000:.1f}s, "
            f"{m.total_clips} clips, "
            f"{m.output_duration:.1f}s output, "
            f"{m.realtime_ratio:.2f}x realtime"
        )

    def _save(self, path: Path) -> None:
        """Persist metrics to JSON file."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text())
                except Exception:
                    existing = []
            existing.append(self._metrics.to_dict())
            path.write_text(json.dumps(existing, indent=2))
        except Exception as exc:
            log.warn(f"Failed to persist metrics: {exc}")
