"""Result types and structured errors for PinoCut pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Generic, TypeVar, Union

T = TypeVar("T")
E = TypeVar("E")


class ErrorSeverity(str, Enum):
    """How critical is the error."""

    WARN = "warn"  # stage can continue
    ERROR = "error"  # stage failed, pipeline can try next
    FATAL = "fatal"  # pipeline must stop


@dataclass(frozen=True, slots=True)
class StageError:
    """Structured error from a pipeline stage."""

    stage: str
    message: str
    severity: ErrorSeverity = ErrorSeverity.ERROR
    clip_path: str | None = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: dict | None = None

    def __str__(self) -> str:
        loc = f" [{self.clip_path}]" if self.clip_path else ""
        return f"[{self.severity.value.upper()}] {self.stage}{loc}: {self.message}"


@dataclass(frozen=True, slots=True)
class Ok(Generic[T]):
    """Success wrapper."""

    value: T

    @property
    def is_ok(self) -> bool:
        return True

    @property
    def is_err(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class Err(Generic[E]):
    """Error wrapper."""

    error: E

    @property
    def is_ok(self) -> bool:
        return False

    @property
    def is_err(self) -> bool:
        return True


# Result[T, E] — either Ok(value) or Err(error)
Result = Union[Ok[T], Err[E]]


def ok(value: T) -> Ok[T]:
    """Create a success result."""
    return Ok(value=value)


def err(error: E) -> Err[E]:
    """Create an error result."""
    return Err(error=error)
