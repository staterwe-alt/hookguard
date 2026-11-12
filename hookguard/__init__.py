"""hookguard: Resilient webhook gateway with HMAC verification, SQLite WAL dead-letter queue, and exponential backoff dispatch."""

__version__ = "0.1.0"

from hookguard.models import EventStatus, WebhookEvent
from hookguard.storage import EventStorage
from hookguard.verifier import SignatureVerifier

__all__ = [
    "EventStatus",
    "EventStorage",
    "SignatureVerifier",
    "WebhookEvent",
    "__version__",
]
