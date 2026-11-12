"""Cryptographic webhook signature verification with timing-safe comparison."""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping


class SignatureVerificationError(Exception):
    """Raised when webhook signature verification fails."""


class SignatureVerifier:
    """Verifies incoming webhook payloads using HMAC-SHA256 with replay protection."""

    @staticmethod
    def compute_sha256(payload: bytes | str, secret: str) -> str:
        """Compute raw hex HMAC-SHA256 signature."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()

    @classmethod
    def verify_sha256(
        cls,
        payload: bytes | str,
        secret: str,
        signature: str,
        prefix: str = "sha256=",
    ) -> bool:
        """Verify standard HMAC-SHA256 signature in constant time."""
        clean_sig = signature.strip()
        if prefix and clean_sig.startswith(prefix):
            clean_sig = clean_sig[len(prefix) :]

        expected = cls.compute_sha256(payload, secret)
        return hmac.compare_digest(expected, clean_sig)

    @classmethod
    def verify_stripe_style(
        cls,
        payload: bytes | str,
        secret: str,
        header_value: str,
        tolerance_seconds: int = 300,
        current_time: float | None = None,
    ) -> bool:
        """Verify Stripe-style signatures (t=timestamp,v1=signature) with replay window protection."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")

        parts = dict(
            item.split("=", 1)
            for item in header_value.split(",")
            if "=" in item
        )

        ts_str = parts.get("t")
        sig = parts.get("v1")
        if not ts_str or not sig:
            return False

        try:
            timestamp = float(ts_str)
        except ValueError:
            return False

        now = current_time if current_time is not None else time.time()
        if abs(now - timestamp) > tolerance_seconds:
            return False  # Replay attack protection window exceeded

        signed_payload = f"{ts_str}.".encode("utf-8") + payload
        expected = cls.compute_sha256(signed_payload, secret)
        return hmac.compare_digest(expected, sig)

    @classmethod
    def verify_request(
        cls,
        headers: Mapping[str, str],
        payload: bytes | str,
        secret: str,
    ) -> bool:
        """Auto-detect signature header (GitHub, Stripe, standard) and verify."""
        lowered = {k.lower(): v for k, v in headers.items()}

        # 1. GitHub scheme: X-Hub-Signature-256
        if "x-hub-signature-256" in lowered:
            return cls.verify_sha256(
                payload, secret, lowered["x-hub-signature-256"], prefix="sha256="
            )

        # 2. Stripe scheme: Stripe-Signature
        if "stripe-signature" in lowered:
            return cls.verify_stripe_style(payload, secret, lowered["stripe-signature"])

        # 3. Standard generic scheme: X-Signature-256 or X-Signature
        for h in ("x-signature-256", "x-signature", "x-webhook-signature"):
            if h in lowered:
                return cls.verify_sha256(payload, secret, lowered[h], prefix="")

        return False
