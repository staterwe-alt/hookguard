"""End-to-end tests for the HTTP gateway server and ingestion endpoint."""

import json
import threading
import time
import unittest
import urllib.request
from http import HTTPStatus

from hookguard.server import GatewayConfig, WebhookGatewayServer
from hookguard.storage import EventStorage
from hookguard.verifier import SignatureVerifier


class TestWebhookServer(unittest.TestCase):
    def setUp(self) -> None:
        self.storage = EventStorage(":memory:")
        self.cfg = GatewayConfig(
            storage=self.storage,
            target_url="http://localhost:9999/dummy",
            secret="whsec_123",
        )
        self.server = WebhookGatewayServer(("127.0.0.1", 0), self.cfg)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.storage.close()

    def test_health_check(self) -> None:
        url = f"http://127.0.0.1:{self.port}/health"
        with urllib.request.urlopen(url) as resp:
            self.assertEqual(resp.status, HTTPStatus.OK)
            data = json.loads(resp.read().decode())
            self.assertTrue(data["ok"])

    def test_webhook_ingest_with_valid_signature_returns_202(self) -> None:
        url = f"http://127.0.0.1:{self.port}/webhook/github"
        body = b'{"action":"opened"}'
        sig = "sha256=" + SignatureVerifier.compute_sha256(body, "whsec_123")

        req = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            self.assertEqual(resp.status, HTTPStatus.ACCEPTED)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["source"], "github")
            self.assertEqual(data["status"], "PENDING")

        events = self.storage.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].source, "github")

    def test_webhook_ingest_invalid_signature_returns_401(self) -> None:
        url = f"http://127.0.0.1:{self.port}/webhook/github"
        body = b'{"action":"opened"}'

        req = urllib.request.Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=invalid"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req)
            self.fail("Should have raised HTTPError 401")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, HTTPStatus.UNAUTHORIZED)


if __name__ == "__main__":
    unittest.main()
