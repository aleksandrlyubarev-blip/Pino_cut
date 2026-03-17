"""ROMA agent registration for Andrew Swarm — Phase 3 implementation.

Real HTTP registration with:
- Health check endpoint
- Capability-based routing
- Heartbeat loop
- Graceful deregistration
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pinocut.utils.logging import StageLogger

log = StageLogger("swarm_registry")


@dataclass(frozen=True)
class AgentCard:
    """ROMA Agent Card — declares capabilities for Andrew Swarm routing."""

    agent_id: str = "pinocut_v1"
    version: str = "1.0.0"
    description: str = "Modular AI video assembly agent"
    capabilities: list[str] = field(
        default_factory=lambda: [
            "video_assembly", "audio_ducking", "transitions",
            "titling", "color_grading", "ffmpeg_render",
        ]
    )
    input_schema: str = "PinoCutRequest"
    output_schema: str = "PinoCutResult"
    routing_weight: float = 0.85
    sandbox: str = "e2b"
    runtime: str = "moltis"
    health_endpoint: str = "/health"
    tags: list[str] = field(default_factory=lambda: ["video", "editing", "ai"])

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "version": self.version,
            "description": self.description,
            "capabilities": self.capabilities,
            "schemas": {"input": self.input_schema, "output": self.output_schema},
            "routing": {"weight": self.routing_weight},
            "execution": {"sandbox": self.sandbox, "runtime": self.runtime},
            "health_endpoint": self.health_endpoint,
            "tags": self.tags,
        }


@dataclass
class SwarmConfig:
    """Andrew Swarm coordinator configuration."""

    swarm_url: str = field(
        default_factory=lambda: os.getenv("SWARM_URL", "http://localhost:9000")
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("SWARM_API_KEY", "")
    )
    heartbeat_interval: int = 30  # seconds
    timeout: int = 10
    max_retries: int = 3


class SwarmRegistry:
    """Client for Andrew Swarm's ROMA agent registry.

    Handles:
    - Agent registration with capability card
    - Periodic heartbeat
    - Graceful deregistration
    - Status reporting
    """

    def __init__(self, config: SwarmConfig | None = None):
        self.config = config or SwarmConfig()
        self._registered = False
        self._card: AgentCard | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop = threading.Event()
        self._last_heartbeat: float = 0
        self._status: dict[str, Any] = {}

    @property
    def is_registered(self) -> bool:
        return self._registered

    @property
    def is_configured(self) -> bool:
        return bool(self.config.api_key)

    def register(self, card: AgentCard | None = None) -> bool:
        """Register PinoCut in the Swarm agent registry."""
        self._card = card or AgentCard()

        if not self.is_configured:
            log.info(f"Swarm not configured — local mode. Card: {self._card.agent_id}")
            self._registered = True
            return True

        try:
            resp = self._api_call("POST", "/agents/register", self._card.to_dict())
            self._registered = True
            self._status = resp
            log.info(f"Registered in Swarm: {self._card.agent_id} v{self._card.version}")

            # Start heartbeat
            self._start_heartbeat()
            return True

        except Exception as exc:
            log.warn(f"Registration failed: {exc}")
            # Register locally anyway
            self._registered = True
            return False

    def deregister(self, agent_id: str = "pinocut_v1") -> bool:
        """Remove PinoCut from the registry."""
        self._stop_heartbeat()

        if self.is_configured:
            try:
                self._api_call("DELETE", f"/agents/{agent_id}")
                log.info(f"Deregistered: {agent_id}")
            except Exception as exc:
                log.warn(f"Deregistration failed: {exc}")

        self._registered = False
        return True

    def heartbeat(self) -> bool:
        """Send health check to coordinator."""
        if not self.is_configured:
            return self._registered

        try:
            resp = self._api_call("POST", f"/agents/{self._card.agent_id}/heartbeat", {
                "status": "healthy",
                "timestamp": time.time(),
                "metrics": self._status.get("metrics", {}),
            })
            self._last_heartbeat = time.time()
            return True
        except Exception:
            return False

    def update_status(self, metrics: dict[str, Any]) -> None:
        """Update agent status with pipeline metrics."""
        self._status["metrics"] = metrics
        if self.is_configured and self._registered:
            try:
                self._api_call("PATCH", f"/agents/{self._card.agent_id}/status", {
                    "metrics": metrics,
                })
            except Exception:
                pass

    def _start_heartbeat(self) -> None:
        """Start background heartbeat thread."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return

        self._heartbeat_stop.clear()

        def loop():
            while not self._heartbeat_stop.wait(self.config.heartbeat_interval):
                self.heartbeat()

        self._heartbeat_thread = threading.Thread(target=loop, daemon=True, name="swarm-heartbeat")
        self._heartbeat_thread.start()

    def _stop_heartbeat(self) -> None:
        """Stop heartbeat thread."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)
            self._heartbeat_thread = None

    def _api_call(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Make an API call to the Swarm coordinator."""
        url = f"{self.config.swarm_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": "PinoCut/1.0",
        }

        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries):
            try:
                req = Request(url, data=data, headers=headers, method=method)
                with urlopen(req, timeout=self.config.timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body) if body.strip() else {}
            except (HTTPError, URLError, OSError) as e:
                last_exc = e
                if attempt < self.config.max_retries - 1:
                    time.sleep(1.0 * (attempt + 1))

        raise RuntimeError(f"Swarm API failed after retries") from last_exc

    def shutdown(self) -> None:
        """Deregister and cleanup."""
        if self._registered:
            self.deregister()
        self._stop_heartbeat()
