"""Base class for all PinoCut pipeline stages."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from pinocut.utils.errors import ErrorSeverity, StageError
from pinocut.utils.logging import StageLogger

if TYPE_CHECKING:
    from pinocut.state import PinoCutState


class BaseStage(ABC):
    """Abstract base class for pipeline stages.

    Every stage follows the same contract:
        state_in → process(state) → state_out

    Stages MUST NOT raise exceptions during normal operation.
    Instead they append StageError to state["errors"] and return.
    """

    name: str = "base"

    def __init__(self, *, structured_logs: bool = False):
        self.log = StageLogger(self.name, structured=structured_logs)

    def __call__(self, state: PinoCutState) -> PinoCutState:
        """LangGraph-compatible callable. Wraps process() with timing and error handling."""
        self.log.info(f"Starting {self.name}")
        start = time.perf_counter()

        try:
            result = self.process(state)
        except Exception as exc:
            self.log.exception(f"Unhandled exception in {self.name}")
            errors = list(state.get("errors", []))
            errors.append(
                StageError(
                    stage=self.name,
                    message=str(exc),
                    severity=ErrorSeverity.ERROR,
                )
            )
            state["errors"] = errors
            return state

        elapsed_ms = round((time.perf_counter() - start) * 1000)
        self.log.info(f"Completed {self.name} in {elapsed_ms}ms")
        return result

    @abstractmethod
    def process(self, state: PinoCutState) -> PinoCutState:
        """Process state and return updated state.

        Implementations should:
        - Read only the keys they need
        - Write only the keys they own
        - Append errors to state["errors"] instead of raising
        """
        ...
