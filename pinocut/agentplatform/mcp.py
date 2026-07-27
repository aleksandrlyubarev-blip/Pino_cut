"""Partner MCP client: JSON-RPC over Streamable HTTP with a governed retry policy.

The partner MCP server specification is not fixed, so this client encodes the
contract assumption from the spec rather than a vendor SDK: JSON-RPC 2.0 on a
single ``/mcp`` endpoint, bearer auth injected by the gateway's auth manager,
idempotency keys on every mutating ``tools/call``, and a machine-readable error
model the agents can build a retry decision on.

The transport is injected, so the adapter can sit behind Agent Gateway in
production and behind a fake in tests without touching call semantics.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

from pinocut.utils.logging import StageLogger

#: Protocol revision this client negotiates.
MCP_PROTOCOL_VERSION = "2025-11-25"


class McpErrorCategory(str, Enum):
    """Coarse category an agent can act on without parsing vendor codes."""

    TRANSPORT = "transport"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    VALIDATION = "validation"
    SERVER = "server"
    PROTOCOL = "protocol"
    UNKNOWN = "unknown"


#: Categories worth another attempt. Validation and auth failures are not.
RETRYABLE_CATEGORIES: frozenset[McpErrorCategory] = frozenset(
    {McpErrorCategory.TRANSPORT, McpErrorCategory.RATE_LIMIT, McpErrorCategory.SERVER}
)


class McpError(Exception):
    """Structured MCP failure carrying its own retry decision."""

    def __init__(
        self,
        message: str,
        *,
        category: McpErrorCategory = McpErrorCategory.UNKNOWN,
        code: int | None = None,
        data: Any = None,
    ):
        super().__init__(message)
        self.message = message
        self.category = category
        self.code = code
        self.data = data

    @property
    def retryable(self) -> bool:
        return self.category in RETRYABLE_CATEGORIES

    def to_dict(self) -> dict:
        return {
            "message": self.message,
            "category": self.category.value,
            "code": self.code,
            "retryable": self.retryable,
        }

    def __str__(self) -> str:
        code = f" ({self.code})" if self.code is not None else ""
        return f"[{self.category.value}{code}] {self.message}"


def category_for_http(status: int) -> McpErrorCategory:
    if status in (401, 403):
        return McpErrorCategory.AUTH
    if status == 429:
        return McpErrorCategory.RATE_LIMIT
    if status == 400 or status == 422:
        return McpErrorCategory.VALIDATION
    if status >= 500:
        return McpErrorCategory.SERVER
    return McpErrorCategory.PROTOCOL


def category_for_rpc(code: int) -> McpErrorCategory:
    """Map JSON-RPC error codes onto the shared taxonomy."""
    if code in (-32600, -32601, -32602, -32700):
        return McpErrorCategory.VALIDATION
    if code == -32603:
        return McpErrorCategory.SERVER
    if code in (-32001, -32002):  # commonly used for auth/permission in MCP servers
        return McpErrorCategory.AUTH
    return McpErrorCategory.UNKNOWN


class Transport(Protocol):
    """Anything that can POST a JSON-RPC envelope and return ``(status, body)``."""

    def post(self, payload: dict, headers: dict[str, str]) -> tuple[int, dict]: ...


class HttpTransport:
    """Streamable-HTTP transport over :mod:`urllib`. No third-party deps."""

    def __init__(self, endpoint: str, *, timeout_sec: float = 30.0):
        self.endpoint = endpoint
        self.timeout_sec = timeout_sec

    def post(self, payload: dict, headers: dict[str, str]) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, method="POST")
        request.add_header("content-type", "application/json")
        request.add_header("accept", "application/json")
        for key, value in headers.items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_sec) as response:
                raw = response.read().decode("utf-8") or "{}"
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:  # server answered with an error status
            raw = exc.read().decode("utf-8") or "{}"
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {"error": {"code": exc.code, "message": raw[:500]}}
            return exc.code, parsed
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise McpError(str(exc), category=McpErrorCategory.TRANSPORT) from exc


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff. Deterministic — jitter is the caller's choice."""

    max_attempts: int = 4
    base_delay_sec: float = 2.0
    max_delay_sec: float = 16.0

    def delay_for(self, attempt: int) -> float:
        """Delay before ``attempt`` (1-based): 2s, 4s, 8s, 16s."""
        return min(self.base_delay_sec * (2 ** max(0, attempt - 1)), self.max_delay_sec)


@dataclass
class McpCallRecord:
    """One audited call to the partner. Feeds Cloud Logging / the audit trail."""

    method: str
    tool: str | None
    attempts: int
    ok: bool
    idempotency_key: str | None = None
    error: dict | None = None
    duration_ms: float = 0.0


@dataclass
class PartnerMcpClient:
    """Governed client for the partner MCP server.

    Args:
        transport: Injected transport. Use :class:`HttpTransport` in production
            behind Agent Gateway; a fake in tests.
        token_provider: Returns the current bearer token. Called per attempt so a
            refreshed token is picked up on retry.
        retry: Backoff policy for retryable categories only.
        sleeper: Injected sleep, so tests do not wait.
    """

    transport: Transport
    token_provider: Callable[[], str | None] = lambda: None
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    client_name: str = "pinocut-media-agent-platform"
    client_version: str = "0.1.0"
    sleeper: Callable[[float], None] = time.sleep
    calls: list[McpCallRecord] = field(default_factory=list)
    _request_id: int = 0
    _initialized: bool = False

    def __post_init__(self) -> None:
        self.log = StageLogger("mcp_client")

    # -- public API ----------------------------------------------------------

    def initialize(self) -> dict:
        """Negotiate the session. Must precede ``tools/*``."""
        result = self._rpc(
            "initialize",
            {
                "clientInfo": {"name": self.client_name, "version": self.client_version},
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            },
        )
        self._initialized = True
        return result

    def list_tools(self) -> list[dict]:
        result = self._rpc("tools/list", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            raise McpError("tools/list did not return a list", category=McpErrorCategory.PROTOCOL)
        return tools

    def call_tool(
        self,
        name: str,
        arguments: dict,
        *,
        idempotency_key: str | None = None,
    ) -> dict:
        """Invoke a partner tool.

        An ``idempotency_key`` is required for anything that mutates partner
        state; the same key must be reused across retries so a double delivery
        does not create a second schedule, task or version.
        """
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if idempotency_key:
            params["_meta"] = {"idempotencyKey": idempotency_key}
        return self._rpc("tools/call", params, tool=name, idempotency_key=idempotency_key)

    def ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    # -- internals -----------------------------------------------------------

    def _next_id(self) -> str:
        self._request_id += 1
        return f"{self.client_name}-{self._request_id}"

    def _headers(self, idempotency_key: str | None) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = self.token_provider()
        if token:
            headers["authorization"] = f"Bearer {token}"
        if idempotency_key:
            headers["x-idempotency-key"] = idempotency_key
        return headers

    def _rpc(
        self,
        method: str,
        params: dict,
        *,
        tool: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        started = time.monotonic()
        last_error: McpError | None = None
        max_attempts = max(1, self.retry.max_attempts)
        attempt = 0

        for attempt in range(1, max_attempts + 1):
            try:
                status, body = self.transport.post(payload, self._headers(idempotency_key))
                result = self._unwrap(status, body)
            except McpError as exc:
                last_error = exc
            else:
                self.calls.append(
                    McpCallRecord(
                        method=method,
                        tool=tool,
                        attempts=attempt,
                        ok=True,
                        idempotency_key=idempotency_key,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                )
                return result

            if not last_error.retryable or attempt == max_attempts:
                break
            delay = self.retry.delay_for(attempt)
            self.log.warn(f"{method} failed ({last_error}); retry {attempt} in {delay:.0f}s")
            self.sleeper(delay)

        if last_error is None:  # unreachable unless the loop never ran
            raise McpError("no MCP attempt was made", category=McpErrorCategory.PROTOCOL)
        self.calls.append(
            McpCallRecord(
                method=method,
                tool=tool,
                attempts=attempt,
                ok=False,
                idempotency_key=idempotency_key,
                error=last_error.to_dict(),
                duration_ms=(time.monotonic() - started) * 1000,
            )
        )
        raise last_error

    @staticmethod
    def _unwrap(status: int, body: dict) -> dict:
        if status >= 400:
            error = body.get("error") if isinstance(body, dict) else None
            message = (error or {}).get("message", f"HTTP {status}")
            raise McpError(message, category=category_for_http(status), code=status, data=error)
        if not isinstance(body, dict):
            raise McpError("non-object JSON-RPC response", category=McpErrorCategory.PROTOCOL)
        if "error" in body and body["error"]:
            error = body["error"]
            code = error.get("code", -32603)
            raise McpError(
                error.get("message", "MCP error"),
                category=category_for_rpc(code),
                code=code,
                data=error.get("data"),
            )
        result = body.get("result")
        if result is None:
            raise McpError("MCP returned no result", category=McpErrorCategory.PROTOCOL)
        if not isinstance(result, dict):
            return {"value": result}
        return result
