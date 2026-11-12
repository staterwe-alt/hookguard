"""Tests for exponential backoff retry logic and dead-letter queue transitions."""

import unittest

from hookguard.dispatcher import WebhookDispatcher, calculate_backoff
from hookguard.models import EventStatus, WebhookEvent
from hookguard.storage import EventStorage


class TestWebhookDispatcher(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = EventStorage(":memory:")

    def tearDown(self) -> None:
        self.storage.close()

    def test_exponential_backoff_progression(self) -> None:
        d1 = calculate_backoff(1, base_delay=2.0, jitter=False)
        d2 = calculate_backoff(2, base_delay=2.0, jitter=False)
        d3 = calculate_backoff(3, base_delay=2.0, jitter=False)
        self.assertEqual(d1, 2.0)
        self.assertEqual(d2, 4.0)
        self.assertEqual(d3, 8.0)

    def test_successful_dispatch_marks_delivered(self) -> None:
        sent_payloads = []

        def fake_sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
            sent_payloads.append((url, body.decode("utf-8"), headers))
            return 200, ""

        ev = WebhookEvent(
            id="evt-ok",
            source="test",
            target_url="http://downstream.local/hooks",
            payload='{"ok": true}',
        )
        self.storage.enqueue(ev)

        dispatcher = WebhookDispatcher(storage=self.storage, http_sender=fake_sender)
        processed = dispatcher.process_once()

        self.assertEqual(processed, 1)
        self.assertEqual(len(sent_payloads), 1)
        stored = self.storage.get_event("evt-ok")
        self.assertEqual(stored.status, EventStatus.DELIVERED)

    def test_exhausted_retries_move_to_dlq(self) -> None:
        def failing_sender(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
            return 503, "Service Unavailable"

        ev = WebhookEvent(
            id="evt-fail",
            source="test",
            target_url="http://downstream.local/hooks",
            payload="{}",
            max_attempts=2,
        )
        self.storage.enqueue(ev)

        dispatcher = WebhookDispatcher(
            storage=self.storage, base_delay=0.0, http_sender=failing_sender
        )

        # Attempt 1 -> should enter RETRYING
        dispatcher.process_once()
        after_1 = self.storage.get_event("evt-fail")
        self.assertEqual(after_1.status, EventStatus.RETRYING)
        self.assertEqual(after_1.attempts, 1)

        # Force due immediately and attempt 2 -> should enter DEAD
        after_1.next_retry_at = 0.0
        dispatcher.dispatch_one(after_1)
        after_2 = self.storage.get_event("evt-fail")
        self.assertEqual(after_2.status, EventStatus.DEAD)
        self.assertEqual(after_2.attempts, 2)


if __name__ == "__main__":
    unittest.main()
