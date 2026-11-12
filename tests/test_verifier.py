"""Tests for cryptographic HMAC signature verification and replay protection."""

import time
import unittest

from hookguard.verifier import SignatureVerifier


class TestSignatureVerifier(unittest.TestCase):
    def test_github_sha256_verification(self) -> None:
        secret = "super_secret_key"
        payload = '{"event":"push"}'
        sig = "sha256=" + SignatureVerifier.compute_sha256(payload, secret)

        self.assertTrue(
            SignatureVerifier.verify_request({"X-Hub-Signature-256": sig}, payload, secret)
        )
        self.assertFalse(
            SignatureVerifier.verify_request(
                {"X-Hub-Signature-256": "sha256=deadbeef"}, payload, secret
            )
        )

    def test_stripe_signature_and_replay_window(self) -> None:
        secret = "whsec_test123"
        payload = b'{"id":"evt_1"}'
        now = 1700000000.0
        ts = str(int(now))
        signed_payload = f"{ts}.".encode("utf-8") + payload
        v1 = SignatureVerifier.compute_sha256(signed_payload, secret)
        header = f"t={ts},v1={v1}"

        # Within tolerance window -> valid
        self.assertTrue(
            SignatureVerifier.verify_stripe_style(
                payload, secret, header, tolerance_seconds=300, current_time=now + 60
            )
        )
        # Outside tolerance window -> rejected as replay attack
        self.assertFalse(
            SignatureVerifier.verify_stripe_style(
                payload, secret, header, tolerance_seconds=300, current_time=now + 600
            )
        )

    def test_missing_signature_headers_returns_false(self) -> None:
        self.assertFalse(
            SignatureVerifier.verify_request({"Content-Type": "application/json"}, "{}", "sec")
        )


if __name__ == "__main__":
    unittest.main()
