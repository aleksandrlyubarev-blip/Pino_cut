"""Structured logger for PinoCut pipeline stages."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON-structured log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "stage": getattr(record, "stage", "pinocut"),
            "msg": record.getMessage(),
        }
        if hasattr(record, "clip"):
            entry["clip"] = record.clip
        if hasattr(record, "duration_ms"):
            entry["duration_ms"] = record.duration_ms
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        return json.dumps(entry, ensure_ascii=False)


def get_logger(stage: str, *, structured: bool = False) -> logging.Logger:
    """Get a logger for a pipeline stage.

    Args:
        stage: Stage name used as logger namespace.
        structured: If True, output JSON lines. Otherwise human-readable.
    """
    logger = logging.getLogger(f"pinocut.{stage}")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        if structured:
            handler.setFormatter(StructuredFormatter())
        else:
            handler.setFormatter(
                logging.Formatter(f"%(asctime)s | %(levelname)-7s | {stage} | %(message)s")
            )
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

    return logger


class StageLogger:
    """Convenience wrapper that auto-attaches stage context."""

    def __init__(self, stage: str, *, structured: bool = False):
        self._log = get_logger(stage, structured=structured)
        self._stage = stage

    def info(self, msg: str, **extra: Any) -> None:
        self._log.info(msg, extra={"stage": self._stage, **extra})

    def debug(self, msg: str, **extra: Any) -> None:
        self._log.debug(msg, extra={"stage": self._stage, **extra})

    def warn(self, msg: str, **extra: Any) -> None:
        self._log.warning(msg, extra={"stage": self._stage, **extra})

    def error(self, msg: str, **extra: Any) -> None:
        self._log.error(msg, extra={"stage": self._stage, **extra})

    def exception(self, msg: str, **extra: Any) -> None:
        self._log.exception(msg, extra={"stage": self._stage, **extra})
