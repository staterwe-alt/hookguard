# hookguard

A zero-dependency resilient webhook ingestion gateway and dead-letter queue (DLQ) built with Python standard library, SQLite WAL persistence, cryptographic HMAC signature verification, and exponential backoff retries.

## Why `hookguard`?

When downstream services experience downtime, restarts, or transient database locks, incoming webhooks from providers like Stripe, GitHub, or Twilio are easily lost. `hookguard` sits in front of your backend to absorb bursts, verify authenticity, persist payloads immediately, and guarantee eventual delivery.

### Key Capabilities
- **Fast Ingestion & `202 Accepted`:** Captures incoming payloads immediately without blocking on downstream service availability.
- **Cryptographic Signature Verification:** Supports GitHub (`X-Hub-Signature-256`), Stripe (`Stripe-Signature` with replay window tolerance), and generic HMAC-SHA256 headers using constant-time `hmac.compare_digest`.
- **SQLite WAL Persistence & Idempotency:** Stores events safely in SQLite Write-Ahead Logging mode with `X-Idempotency-Key` / `X-GitHub-Delivery` deduplication.
- **Exponential Backoff with Jitter:** Retries failed deliveries using jittered exponential intervals before moving exhausted events into the Dead-Letter Queue (`DEAD`).
- **DLQ Inspection & Replay CLI:** View queue stats, list failed payloads, and replay dead-letter events once your backend recovers.

---

## Architecture

```text
  Stripe / GitHub / Twilio
             │
             ▼  POST /webhook/{source}
┌─────────────────────────┐
│  WebhookGatewayServer   │──► Verifies HMAC-SHA256 + Replay Window
│  (HTTP 202 Accepted)    │──► Deduplicates via Idempotency Key
└────────────┬────────────┘
             │
             ▼
┌─────────────────────────┐
│   SQLite WAL Storage    │
│  (PENDING / RETRYING)   │
└────────────┬────────────┘
             │
             ▼  Polls & forwards
┌─────────────────────────┐     2xx OK
│   WebhookDispatcher     ├───────────────► DELIVERED
└────────────┬────────────┘
             │ 5xx / Connection Error
             ▼
  Exponential Backoff + Jitter
             │ (max attempts reached)
             ▼
     DEAD (Dead-Letter Queue) ──► CLI: `hookguard replay --all`
```

---

## Installation

`hookguard` uses only the Python standard library (Python 3.11+):

```bash
pip install -e .
```

---

## Quick Start

### 1. Start the Gateway
Forward verified webhooks from port `8080` to your local API on `http://localhost:3000/api/webhooks`:

```bash
hookguard start --port 8080 --target http://localhost:3000/api/webhooks --secret whsec_mysecret
```

Or run directly via module:
```bash
python -m hookguard start --port 8080 --target http://localhost:3000/api/webhooks
```

### 2. Inspect Queue & Dead-Letter Metrics
```bash
hookguard stats
hookguard list --status DEAD
```

### 3. Replay Failed Webhooks
Once your downstream service is healthy again, replay a single event or the entire dead-letter queue:
```bash
hookguard replay <event_id>
hookguard replay --all
```

### 4. Verify Signatures Locally
```bash
hookguard verify --secret mysecret --signature sha256=... --payload '{"ok":true}'
```

---

## Running Tests

```bash
python -m unittest discover -s tests -v
```

---

## License

MIT License — see [LICENSE](LICENSE).
