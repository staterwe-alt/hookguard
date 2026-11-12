"""Thread-safe SQLite WAL storage backend with dead-letter queue and idempotency tracking."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from hookguard.models import EventStatus, StorageStats, WebhookEvent


class EventStorage:
    """Persistent queue storage using SQLite in Write-Ahead Logging (WAL) mode."""

    def __init__(self, db_path: str | Path = "hookguard.db") -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            if self.db_path != ":memory:":
                cur.execute("PRAGMA journal_mode=WAL;")
                cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA busy_timeout=5000;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS webhook_events (
                    id TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    target_url TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    headers_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    next_retry_at REAL NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT,
                    idempotency_key TEXT
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_due ON webhook_events (status, next_retry_at)"
            )
            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_events_idempotency
                ON webhook_events (idempotency_key)
                WHERE idempotency_key IS NOT NULL
                """
            )
            self._conn.commit()

    def close(self) -> None:
        """Close the database connection cleanly."""
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> WebhookEvent:
        return WebhookEvent(
            id=row["id"],
            source=row["source"],
            target_url=row["target_url"],
            payload=row["payload"],
            headers=json.loads(row["headers_json"]),
            status=EventStatus(row["status"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            next_retry_at=row["next_retry_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_error=row["last_error"],
            idempotency_key=row["idempotency_key"],
        )

    def enqueue(self, event: WebhookEvent) -> tuple[WebhookEvent, bool]:
        """Insert a new webhook event. Returns (event, True) if created or (existing, False) if duplicate."""
        now = time.time()
        with self._lock:
            if event.idempotency_key:
                existing = self._conn.execute(
                    "SELECT * FROM webhook_events WHERE idempotency_key = ?",
                    (event.idempotency_key,),
                ).fetchone()
                if existing:
                    return self._row_to_event(existing), False

            self._conn.execute(
                """
                INSERT INTO webhook_events (
                    id, source, target_url, payload, headers_json, status,
                    attempts, max_attempts, next_retry_at, created_at, updated_at,
                    last_error, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.id,
                    event.source,
                    event.target_url,
                    event.payload,
                    json.dumps(event.headers),
                    event.status.value,
                    event.attempts,
                    event.max_attempts,
                    event.next_retry_at,
                    event.created_at or now,
                    event.updated_at or now,
                    event.last_error,
                    event.idempotency_key,
                ),
            )
            self._conn.commit()
            return event, True

    def fetch_due_events(self, limit: int = 20, now: float | None = None) -> list[WebhookEvent]:
        """Retrieve events ready for immediate dispatch."""
        ts = now if now is not None else time.time()
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM webhook_events
                WHERE status IN (?, ?) AND next_retry_at <= ?
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT ?
                """,
                (EventStatus.PENDING.value, EventStatus.RETRYING.value, ts, limit),
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def mark_delivered(self, event_id: str) -> None:
        """Mark an event as successfully delivered."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE webhook_events
                SET status = ?, attempts = attempts + 1, updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (EventStatus.DELIVERED.value, now, event_id),
            )
            self._conn.commit()

    def mark_retry(
        self, event_id: str, attempts: int, next_retry_at: float, error: str
    ) -> None:
        """Schedule an event for future retry."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE webhook_events
                SET status = ?, attempts = ?, next_retry_at = ?, updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (EventStatus.RETRYING.value, attempts, next_retry_at, now, error, event_id),
            )
            self._conn.commit()

    def mark_dead(self, event_id: str, attempts: int, error: str) -> None:
        """Move an exhausted event into the Dead-Letter Queue (DEAD)."""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                UPDATE webhook_events
                SET status = ?, attempts = ?, updated_at = ?, last_error = ?
                WHERE id = ?
                """,
                (EventStatus.DEAD.value, attempts, now, error, event_id),
            )
            self._conn.commit()

    def replay_event(self, event_id: str) -> bool:
        """Reset an event back to PENDING for immediate redelivery."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE webhook_events
                SET status = ?, attempts = 0, next_retry_at = 0, updated_at = ?, last_error = NULL
                WHERE id = ?
                """,
                (EventStatus.PENDING.value, now, event_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def replay_all_dead(self) -> int:
        """Reset all DEAD events back to PENDING."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE webhook_events
                SET status = ?, attempts = 0, next_retry_at = 0, updated_at = ?, last_error = NULL
                WHERE status = ?
                """,
                (EventStatus.PENDING.value, now, EventStatus.DEAD.value),
            )
            self._conn.commit()
            return cur.rowcount

    def get_event(self, event_id: str) -> WebhookEvent | None:
        """Fetch a single event by ID."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM webhook_events WHERE id = ?", (event_id,)
            ).fetchone()
            return self._row_to_event(row) if row else None

    def list_events(
        self, status: EventStatus | None = None, limit: int = 50
    ) -> list[WebhookEvent]:
        """List events optionally filtered by lifecycle status."""
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM webhook_events WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM webhook_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def stats(self) -> StorageStats:
        """Compute aggregate counts per lifecycle state."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) as cnt FROM webhook_events GROUP BY status"
            ).fetchall()
            counts = {r["status"]: r["cnt"] for r in rows}
            pending = counts.get(EventStatus.PENDING.value, 0)
            retrying = counts.get(EventStatus.RETRYING.value, 0)
            delivered = counts.get(EventStatus.DELIVERED.value, 0)
            dead = counts.get(EventStatus.DEAD.value, 0)
            return StorageStats(
                total_events=pending + retrying + delivered + dead,
                pending=pending,
                retrying=retrying,
                delivered=delivered,
                dead_letter=dead,
            )
