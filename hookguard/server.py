"""Lightweight threaded HTTP webhook gateway using Python stdlib."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hookguard.models import WebhookEvent
from hookguard.storage import EventStorage
from hookguard.verifier import SignatureVerifier


class GatewayConfig:
    """Runtime configuration for the webhook ingestion gateway."""

    def __init__(
        self,
        storage: EventStorage,
        target_url: str,
        secret: str | None = None,
        max_attempts: int = 5,
    ) -> None:
        self.storage = storage
        self.target_url = target_url
        self.secret = secret
        self.max_attempts = max_attempts


class WebhookRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for receiving, verifying, and enqueuing webhooks."""

    protocol_version = "HTTP/1.1"
    server: WebhookGatewayServer  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:
        """Silence noisy default stderr access logging during library use."""
        pass

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return

        if self.path == "/stats":
            st = self.server.gateway_config.storage.stats()
            self._send_json(HTTPStatus.OK, asdict(st))
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self.path.startswith("/webhook"):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        content_len = int(self.headers.get("Content-Length", 0))
        if content_len <= 0 or content_len > 5 * 1024 * 1024:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid or oversized payload"})
            return

        raw_body = self.rfile.read(content_len)
        headers_dict = {k: v for k, v in self.headers.items()}
        cfg = self.server.gateway_config

        # Verify cryptographic HMAC signature if configured
        if cfg.secret:
            if not SignatureVerifier.verify_request(headers_dict, raw_body, cfg.secret):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"error": "Invalid webhook signature"},
                )
                return

        parts = self.path.strip("/").split("/")
        source = parts[1] if len(parts) > 1 else "default"

        # Detect common idempotency headers
        lowered = {k.lower(): v for k, v in headers_dict.items()}
        idem_key = (
            lowered.get("x-idempotency-key")
            or lowered.get("x-github-delivery")
            or lowered.get("x-request-id")
        )

        event = WebhookEvent(
            id=str(uuid.uuid4()),
            source=source,
            target_url=cfg.target_url,
            payload=raw_body.decode("utf-8", errors="replace"),
            headers=headers_dict,
            max_attempts=cfg.max_attempts,
            idempotency_key=idem_key,
        )

        stored_event, created = cfg.storage.enqueue(event)
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "id": stored_event.id,
                "source": stored_event.source,
                "status": stored_event.status.value,
                "duplicate": not created,
            },
        )


class WebhookGatewayServer(ThreadingHTTPServer):
    """Multi-threaded HTTP server bound to a GatewayConfig."""

    def __init__(
        self,
        server_address: tuple[str, int],
        gateway_config: GatewayConfig,
    ) -> None:
        self.gateway_config = gateway_config
        super().__init__(server_address, WebhookRequestHandler)
