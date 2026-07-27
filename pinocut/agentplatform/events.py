"""Signed webhook ingress for partner events.

MCP's push/trigger story is still settling, so production events arrive on an
explicit side channel: partner -> signed webhook -> Eventarc/Pub/Sub -> workers.
This module owns the part that must be exactly right before anything reaches an
agent: signature verification, envelope validation, and de-duplication.

At-least-once delivery is assumed. Every handler downstream must therefore be
idempotent, and this ingress drops replays by ``eventId``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

#: Event taxonomy the platform understands. Unknown types are accepted but
#: routed to a dead-letter path rather than an agent.
KNOWN_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "asset.version.created",
        "asset.version.approved",
        "review.status.changed",
        "review.annotation.added",
        "schedule.changed",
        "schedule.published",
        "legal.approval.granted",
        "script.version.published",
    }
)


class IngressStatus(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    UNKNOWN_TYPE = "unknown_type"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """Validated partner event."""

    event_id: str
    event_type: str
    occurred_at: str
    tenant_id: str
    project_id: str
    entity: dict
    actor: dict
    payload: dict

    @classmethod
    def from_payload(cls, body: dict) -> EventEnvelope:
        """Validate and build. Raises :class:`ValueError` on a bad envelope."""
        required = ("eventId", "eventType", "occurredAt", "tenantId", "projectId")
        missing = [key for key in required if not body.get(key)]
        if missing:
            raise ValueError(f"missing required fields: {', '.join(missing)}")
        occurred_at = str(body["occurredAt"])
        try:
            datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"occurredAt is not ISO-8601: {occurred_at}") from exc
        return cls(
            event_id=str(body["eventId"]),
            event_type=str(body["eventType"]),
            occurred_at=occurred_at,
            tenant_id=str(body["tenantId"]),
            project_id=str(body["projectId"]),
            entity=dict(body.get("entity") or {}),
            actor=dict(body.get("actor") or {}),
            payload=dict(body.get("payload") or {}),
        )


def sign_body(secret: str, raw_body: bytes) -> str:
    """Produce the ``X-Signature`` value for a body. Used by tests and senders."""
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, raw_body: bytes, header_value: str | None) -> bool:
    """Constant-time HMAC-SHA256 check of the raw request body."""
    if not header_value:
        return False
    return hmac.compare_digest(sign_body(secret, raw_body), header_value)


class DedupeStore(Protocol):
    """Replay guard. Back it with Firestore/Spanner in production."""

    def seen(self, event_id: str) -> bool: ...

    def remember(self, event_id: str) -> None: ...


class InMemoryDedupeStore:
    """Bounded LRU replay guard — enough for a single worker or a test."""

    def __init__(self, capacity: int = 10_000):
        self._capacity = capacity
        self._ids: OrderedDict[str, None] = OrderedDict()

    def seen(self, event_id: str) -> bool:
        return event_id in self._ids

    def remember(self, event_id: str) -> None:
        self._ids[event_id] = None
        self._ids.move_to_end(event_id)
        while len(self._ids) > self._capacity:
            self._ids.popitem(last=False)


@dataclass(frozen=True, slots=True)
class IngressResult:
    status: IngressStatus
    event: EventEnvelope | None = None
    reason: str | None = None

    @property
    def should_process(self) -> bool:
        return self.status is IngressStatus.ACCEPTED


class EventIngress:
    """Verify, parse, de-duplicate. The only entry point for partner events."""

    def __init__(self, secret: str, store: DedupeStore | None = None):
        self._secret = secret
        self._store = store or InMemoryDedupeStore()

    def handle(self, raw_body: bytes, headers: dict[str, str]) -> IngressResult:
        lowered = {key.lower(): value for key, value in headers.items()}
        if not verify_signature(self._secret, raw_body, lowered.get("x-signature")):
            return IngressResult(IngressStatus.REJECTED, reason="signature mismatch")

        try:
            body = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return IngressResult(IngressStatus.REJECTED, reason=f"malformed body: {exc}")

        try:
            event = EventEnvelope.from_payload(body)
        except ValueError as exc:
            return IngressResult(IngressStatus.REJECTED, reason=str(exc))

        header_id = lowered.get("x-event-id")
        if header_id and header_id != event.event_id:
            return IngressResult(
                IngressStatus.REJECTED, event=event, reason="X-Event-Id does not match body"
            )

        if self._store.seen(event.event_id):
            return IngressResult(IngressStatus.DUPLICATE, event=event, reason="already processed")
        self._store.remember(event.event_id)

        if event.event_type not in KNOWN_EVENT_TYPES:
            return IngressResult(
                IngressStatus.UNKNOWN_TYPE, event=event, reason="no handler registered"
            )
        return IngressResult(IngressStatus.ACCEPTED, event=event)
