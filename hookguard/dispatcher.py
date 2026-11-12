"""Background HTTP webhook dispatcher with exponential backoff and full jitter."""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.error
import urllib.request
from typing import Callable

from hookguard.models import WebhookEvent
from hookguard.storage import EventStorage

logger = logging.getLogger("hookguard.dispatcher")

HOP_BY_HOP_HEADERS = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def calculate_backoff(
    attempt: int,
    base_delay: float = 2.0,
    max_delay: float = 3600.0,
    jitter: bool = True,
) -> float:
    """Calculate exponential backoff delay in seconds with optional full jitter."""
    exp = min(max_delay, base_delay * (2 ** max(0, attempt - 1)))
    if not jitter:
        return float(exp)
    return random.uniform(exp * 0.5, exp)


HttpSender = Callable[[str, bytes, dict[str, str], float], tuple[int, str]]


def default_http_sender(
    url: str, body: bytes, headers: dict[str, str], timeout: float
) -> tuple[int, str]:
    """Send HTTP POST using Python stdlib urllib without external dependencies."""
    req = urllib.request.Request(url=url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}: {exc.reason}"
    except urllib.error.URLError as exc:
        return 0, f"Connection error: {exc.reason}"
    except Exception as exc:
        return 0, str(exc)


class WebhookDispatcher:
    """Polls due events from storage and dispatches them to downstream targets."""

    def __init__(
        self,
        storage: EventStorage,
        poll_interval: float = 1.0,
        request_timeout: float = 10.0,
        base_delay: float = 2.0,
        max_delay: float = 3600.0,
        http_sender: HttpSender | None = None,
    ) -> None:
        self.storage = storage
        self.poll_interval = poll_interval
        self.request_timeout = request_timeout
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._http_sender = http_sender or default_http_sender
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def dispatch_one(self, event: WebhookEvent) -> bool:
        """Attempt delivery of a single event and update storage state."""
        forward_headers = {
            k: v
            for k, v in event.headers.items()
            if k.lower() not in HOP_BY_HOP_HEADERS
        }
        forward_headers.setdefault("Content-Type", "application/json")
        forward_headers["X-Hookguard-Event-Id"] = event.id
        forward_headers["X-Hookguard-Attempt"] = str(event.attempts + 1)

        status_code, err_msg = self._http_sender(
            event.target_url,
            event.payload.encode("utf-8"),
            forward_headers,
            self.request_timeout,
        )

        if 200 <= status_code < 300:
            self.storage.mark_delivered(event.id)
            logger.info("Delivered event %s to %s", event.id, event.target_url)
            return True

        new_attempts = event.attempts + 1
        error_text = err_msg or f"Unexpected HTTP status {status_code}"

        if new_attempts >= event.max_attempts:
            self.storage.mark_dead(event.id, new_attempts, error_text)
            logger.warning(
                "Event %s exhausted %d attempts -> moved to DLQ (%s)",
                event.id,
                new_attempts,
                error_text,
            )
        else:
            delay = calculate_backoff(
                new_attempts, base_delay=self.base_delay, max_delay=self.max_delay
            )
            next_ts = time.time() + delay
            self.storage.mark_retry(event.id, new_attempts, next_ts, error_text)
            logger.info(
                "Event %s failed (attempt %d/%d), retrying in %.1fs: %s",
                event.id,
                new_attempts,
                event.max_attempts,
                delay,
                error_text,
            )
        return False

    def process_once(self, batch_size: int = 20) -> int:
        """Process all currently due events. Returns count of attempted deliveries."""
        due = self.storage.fetch_due_events(limit=batch_size)
        for event in due:
            self.dispatch_one(event)
        return len(due)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processed = self.process_once()
                if processed == 0:
                    self._stop_event.wait(self.poll_interval)
            except Exception:
                logger.exception("Unhandled error in dispatcher loop")
                self._stop_event.wait(self.poll_interval)

    def start(self) -> None:
        """Start the background dispatcher thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="hookguard-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the dispatcher thread to stop cleanly."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
