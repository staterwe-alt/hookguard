"""Tests for hookguard CLI subcommands."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hookguard.cli import main
from hookguard.models import EventStatus, WebhookEvent
from hookguard.storage import EventStorage
from hookguard.verifier import SignatureVerifier


class TestCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "cli_test.db"

        storage = EventStorage(self.db_path)
        ev = WebhookEvent(
            id="evt-dlq-1",
            source="stripe",
            target_url="http://localhost/hook",
            payload="{}",
            status=EventStatus.DEAD,
            attempts=5,
            last_error="HTTP 500",
        )
        storage.enqueue(ev)
        storage.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_cli_stats_and_list(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["--db", str(self.db_path), "stats"])
        self.assertEqual(rc, 0)
        self.assertIn("Dead-Letter:    1", out.getvalue())

        out_list = io.StringIO()
        with redirect_stdout(out_list):
            rc2 = main(["--db", str(self.db_path), "list", "--status", "DEAD"])
        self.assertEqual(rc2, 0)
        self.assertIn("evt-dlq-1", out_list.getvalue())

    def test_cli_replay_all(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["--db", str(self.db_path), "replay", "--all"])
        self.assertEqual(rc, 0)
        self.assertIn("Reset 1 dead-letter events", out.getvalue())

    def test_cli_verify_command(self) -> None:
        sig = "sha256=" + SignatureVerifier.compute_sha256("hello", "mysecret")
        out = io.StringIO()
        with redirect_stdout(out):
            rc = main(["verify", "--secret", "mysecret", "--signature", sig, "--payload", "hello"])
        self.assertEqual(rc, 0)
        self.assertIn("Signature valid", out.getvalue())


if __name__ == "__main__":
    unittest.main()
