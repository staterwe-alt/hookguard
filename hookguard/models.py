"""Domain models representing webhook events and gateway states."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


class EventStatus(str, enum.Enum):
    """Lifecycle statuses for captured webhook events."""

    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    RETRYING = "RETRYING"
    DEAD = "DEAD"


@dataclass
class WebhookEvent:
    """Represents a webhook event captured by the gateway."""

    id: str
    source: str
    target_url: str
    payload: str
    headers: dict[str, str] = field(default_factory=dict)
    status: EventStatus = EventStatus.PENDING
    attempts: int = 0
    max_attempts: int = 5
    next_retry_at: float = 0.0  # Unix timestamp
    created_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now(timezone.utc).timestamp())
    last_error: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class StorageStats:
    """Aggregated metrics across the event queue."""

    total_events: int
    pending: int
    retrying: int
    delivered: int
    dead_letter: int
