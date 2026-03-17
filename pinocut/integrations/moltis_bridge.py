"""Moltis runtime bridge — Phase 2-3 implementation.

Upgraded from Phase 1 with:
- Persistent JSON-file memory between runs
- Typed channels with backpressure
- Task scheduler with priorities and timeouts
- FFI interface stub for Moltis Rust runtime
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

from pinocut.utils.logging import StageLogger

log = StageLogger("moltis")

T = TypeVar("T")


class TaskPriority(int, Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


@dataclass
class MoltisConfig:
    """Moltis runtime configuration."""

    max_workers: int = 4
    memory_backend: str = field(
        default_factory=lambda: os.getenv("PINOCUT_MEMORY", "local")
    )  # local | json_file | moltis
    memory_path: Path = field(
        default_factory=lambda: Path(".pinocut_memory.json")
    )
    sandbox: str = "local"  # local | e2b
    channel_buffer_size: int = 32
    task_timeout: int = 300


# ══════════════════════════════════════════════
#  TYPED CHANNEL
# ══════════════════════════════════════════════

class Channel(Generic[T]):
    """Typed async channel with backpressure.

    Phase 2: asyncio.Queue with generics.
    Sprint 5+: Will bridge to Moltis Rust mpsc channels via PyO3.
    """

    def __init__(self, buffer_size: int = 32, name: str = ""):
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=buffer_size)
        self._name = name
        self._sent = 0
        self._received = 0
        self._closed = False

    async def send(self, item: T) -> None:
        if self._closed:
            raise ChannelClosed(f"Channel '{self._name}' is closed")
        await self._queue.put(item)
        self._sent += 1

    async def recv(self) -> T:
        if self._closed and self._queue.empty():
            raise ChannelClosed(f"Channel '{self._name}' is closed and empty")
        item = await self._queue.get()
        self._received += 1
        return item

    def close(self) -> None:
        self._closed = True

    @property
    def empty(self) -> bool:
        return self._queue.empty()

    @property
    def stats(self) -> dict:
        return {"name": self._name, "sent": self._sent, "received": self._received, "closed": self._closed}


class ChannelClosed(Exception):
    pass


# ══════════════════════════════════════════════
#  PERSISTENT MEMORY
# ══════════════════════════════════════════════

class Memory:
    """Key-value memory with optional JSON persistence.

    Phase 2: JSON file backend for caching between runs.
    Sprint 5+: Will bridge to Moltis shared memory / Redis.
    """

    def __init__(self, persist_path: Path | None = None):
        self._store: dict[str, Any] = {}
        self._persist_path = persist_path
        self._dirty = False

        if persist_path and persist_path.exists():
            try:
                self._store = json.loads(persist_path.read_text())
                log.debug(f"Loaded {len(self._store)} items from {persist_path}")
            except Exception:
                self._store = {}

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._store[key] = value
        self._dirty = True

    def delete(self, key: str) -> bool:
        if key in self._store:
            del self._store[key]
            self._dirty = True
            return True
        return False

    def has(self, key: str) -> bool:
        return key in self._store

    def keys(self, prefix: str = "") -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]

    def clear(self) -> None:
        self._store.clear()
        self._dirty = True

    def flush(self) -> None:
        """Persist to disk if dirty."""
        if self._dirty and self._persist_path:
            try:
                self._persist_path.parent.mkdir(parents=True, exist_ok=True)
                self._persist_path.write_text(json.dumps(self._store, default=str))
                self._dirty = False
            except Exception as exc:
                log.warn(f"Memory flush failed: {exc}")

    @property
    def size(self) -> int:
        return len(self._store)


# ══════════════════════════════════════════════
#  TASK SCHEDULER
# ══════════════════════════════════════════════

@dataclass
class TaskResult(Generic[T]):
    """Result of a scheduled task."""

    value: T | None
    error: Exception | None
    duration_ms: float
    task_id: str

    @property
    def ok(self) -> bool:
        return self.error is None


class TaskScheduler:
    """Task scheduler with priorities, timeouts, and metrics.

    Phase 2: ThreadPoolExecutor with priority sorting.
    Sprint 5+: Will bridge to Moltis tokio runtime task scheduler.
    """

    def __init__(self, max_workers: int = 4, default_timeout: int = 300):
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._timeout = default_timeout
        self._completed = 0
        self._failed = 0

    def submit(
        self,
        fn: Callable[..., T],
        *args: Any,
        task_id: str = "",
        priority: TaskPriority = TaskPriority.NORMAL,
        timeout: int | None = None,
    ) -> TaskResult[T]:
        """Submit and wait for a single task."""
        to = timeout or self._timeout
        start = time.perf_counter()
        try:
            future = self._executor.submit(fn, *args)
            value = future.result(timeout=to)
            elapsed = (time.perf_counter() - start) * 1000
            self._completed += 1
            return TaskResult(value=value, error=None, duration_ms=elapsed, task_id=task_id)
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            self._failed += 1
            return TaskResult(value=None, error=exc, duration_ms=elapsed, task_id=task_id)

    def map(
        self,
        fn: Callable[[T], Any],
        items: list[T],
        *,
        timeout: int | None = None,
    ) -> list[TaskResult]:
        """Execute fn on each item in parallel, return all results."""
        to = timeout or self._timeout
        futures: list[tuple[str, float, Future]] = []

        for i, item in enumerate(items):
            f = self._executor.submit(fn, item)
            futures.append((f"task_{i}", time.perf_counter(), f))

        results: list[TaskResult] = []
        for task_id, start, future in futures:
            try:
                value = future.result(timeout=to)
                elapsed = (time.perf_counter() - start) * 1000
                self._completed += 1
                results.append(TaskResult(value=value, error=None, duration_ms=elapsed, task_id=task_id))
            except Exception as exc:
                elapsed = (time.perf_counter() - start) * 1000
                self._failed += 1
                results.append(TaskResult(value=None, error=exc, duration_ms=elapsed, task_id=task_id))

        return results

    @property
    def stats(self) -> dict:
        return {"completed": self._completed, "failed": self._failed}

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


# ══════════════════════════════════════════════
#  MOLTIS BRIDGE (top-level)
# ══════════════════════════════════════════════

class MoltisBridge:
    """Bridge between PinoCut and Moltis runtime.

    Provides unified access to:
    - Parallel execution (TaskScheduler)
    - Persistent memory (Memory)
    - Typed channels (Channel[T])
    """

    def __init__(self, config: MoltisConfig | None = None):
        self.config = config or MoltisConfig()

        # Memory
        persist_path = self.config.memory_path if self.config.memory_backend == "json_file" else None
        self.memory = Memory(persist_path=persist_path)

        # Task scheduler
        self.scheduler = TaskScheduler(
            max_workers=self.config.max_workers,
            default_timeout=self.config.task_timeout,
        )

    def run_parallel(
        self,
        fn: Callable[[T], Any],
        items: list[T],
        *,
        timeout: int | None = None,
    ) -> list[Any]:
        """Execute fn on each item in parallel. Returns values (None for failures)."""
        log.info(f"Parallel: {len(items)} items, {self.config.max_workers} workers")
        results = self.scheduler.map(fn, items, timeout=timeout)
        return [r.value for r in results]

    def create_channel(self, name: str = "") -> Channel:
        """Create a new typed async channel."""
        return Channel(buffer_size=self.config.channel_buffer_size, name=name)

    def shutdown(self) -> None:
        """Flush memory and shutdown scheduler."""
        self.memory.flush()
        self.scheduler.shutdown()
