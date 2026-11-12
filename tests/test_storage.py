"""Tests for SQLite WAL event storage, dead-letter transitions, and replay."""

import tempfile
import time
import unittest
from pathlib import Path

from hookguard.models import EventStatus, WebhookEvent
from hookguard.storage import EventStorage


class TestEventStorage(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.storage = EventStorage(self.db_path)

    def tearDown(self) -> None:
        self.storage.close()
        self.temp_dir.cleanup()

    def test_enqueue_and_idempotency(self) -> None:
        ev1 = WebhookEvent(
            id="evt-1",
            source="stripe",
            target_url="http://localhost/hook",
            payload='{"charge": 100}',
            idempotency_key="idem-key-1",
        )
        stored, created = self.storage.enqueue(ev1)
        self.assertTrue(created)
        self.assertEqual(stored.id, "evt-1")

        # Duplicate idempotency key should return existing and False
        ev2 = WebhookEvent(
            id="evt-2",
            source="stripe",
            target_url="http://localhost/hook",
            payload='{"charge": 100}',
            idempotency_key="idem-key-1",
        )
        stored2, created2 = self.storage.enqueue(ev2)
        self.assertFalse(created2)
        self.assertEqual(stored2.id, "evt-1")

    def test_due_events_and_transitions(self) -> None:
        now = time.time()
        ev = WebhookEvent(
            id="evt-due",
            source="github",
            target_url="http://localhost/hook",
            payload="{}",
            next_retry_at=now - 10,
        )
        self.storage.enqueue(ev)

        due = self.storage.fetch_due_events(now=now)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].id, "evt-due")

        # Mark delivered
        self.storage.mark_delivered("evt-due")
        ev_after = self.storage.get_event("evt-due")
        self.assertIsNotNone(ev_after)
        self.assertEqual(ev_after.status, EventStatus.DELIVERED)

        # Mark dead & replay
        self.storage.mark_dead("evt-due", attempts=5, error="Connection refused")
        dead_ev = self.storage.get_event("evt-due")
        self.assertEqual(dead_ev.status, EventStatus.DEAD)

        self.storage.replay_event("evt-due")
        replayed = self.storage.get_event("evt-due")
        self.assertEqual(replayed.status, EventStatus.PENDING)
        self.assertEqual(replayed.attempts, 0)


if __name__ == "__main__":
    unittest.main()
