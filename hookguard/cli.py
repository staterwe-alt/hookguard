"""Command-line interface for the hookguard gateway and dead-letter queue."""

from __future__ import annotations

import argparse
import sys
import time

from hookguard import __version__
from hookguard.dispatcher import WebhookDispatcher
from hookguard.models import EventStatus
from hookguard.server import GatewayConfig, WebhookGatewayServer
from hookguard.storage import EventStorage
from hookguard.verifier import SignatureVerifier


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="hookguard",
        description="Resilient webhook gateway with HMAC verification and dead-letter queue.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"hookguard {__version__}")
    parser.add_argument("--db", default="hookguard.db", help="Path to SQLite database file.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: start
    p_start = subparsers.add_parser("start", help="Start the webhook gateway receiver and background dispatcher.")
    p_start.add_argument("--host", default="0.0.0.0", help="Binding host address (default: 0.0.0.0).")
    p_start.add_argument("--port", type=int, default=8080, help="Binding port (default: 8080).")
    p_start.add_argument(
        "--target",
        required=True,
        help="Downstream target URL to forward webhooks to.",
    )
    p_start.add_argument("--secret", default=None, help="HMAC secret for signature verification.")
    p_start.add_argument(
        "--max-attempts",
        type=int,
        default=5,
        help="Max delivery attempts before moving to DLQ (default: 5).",
    )
    p_start.add_argument(
        "--no-worker",
        action="store_true",
        help="Do not start the background dispatcher (capture-only mode).",
    )

    # Subcommand: stats
    subparsers.add_parser("stats", help="Display summary metrics of the event queue.")

    # Subcommand: list
    p_list = subparsers.add_parser("list", help="List captured events filtered by status.")
    p_list.add_argument(
        "--status",
        choices=["PENDING", "RETRYING", "DELIVERED", "DEAD"],
        default=None,
        help="Filter by status (default: all).",
    )
    p_list.add_argument("--limit", type=int, default=20, help="Maximum events to return.")

    # Subcommand: replay
    p_replay = subparsers.add_parser("replay", help="Replay dead-letter events for redelivery.")
    p_replay.add_argument("event_id", nargs="?", default=None, help="Specific event ID to replay.")
    p_replay.add_argument("--all", action="store_true", help="Replay all DEAD events.")

    # Subcommand: verify
    p_verify = subparsers.add_parser("verify", help="Test verify a signature against a payload and secret.")
    p_verify.add_argument("--secret", required=True, help="HMAC secret.")
    p_verify.add_argument("--signature", required=True, help="Signature to verify.")
    p_verify.add_argument("--payload", required=True, help="Payload string.")

    return parser


def cmd_stats(storage: EventStorage) -> int:
    st = storage.stats()
    sys.stdout.write("\n=== HookGuard Queue Summary ===\n")
    sys.stdout.write(f"Total Captured: {st.total_events:,}\n")
    sys.stdout.write(f"Pending:        {st.pending:,}\n")
    sys.stdout.write(f"Retrying:       {st.retrying:,}\n")
    sys.stdout.write(f"Delivered:      {st.delivered:,}\n")
    sys.stdout.write(f"Dead-Letter:    {st.dead_letter:,}\n")
    sys.stdout.write("===============================\n\n")
    return 0


def cmd_list(storage: EventStorage, status_str: str | None, limit: int) -> int:
    status_enum = EventStatus(status_str) if status_str else None
    events = storage.list_events(status=status_enum, limit=limit)
    if not events:
        sys.stdout.write("No matching events found.\n")
        return 0

    sys.stdout.write(f"{'ID':<38} {'SOURCE':<12} {'STATUS':<11} {'ATTEMPTS':<10} {'LAST ERROR'}\n")
    sys.stdout.write("-" * 90 + "\n")
    for ev in events:
        err = (ev.last_error or "")[:30]
        sys.stdout.write(f"{ev.id:<38} {ev.source:<12} {ev.status.value:<11} {ev.attempts:<10} {err}\n")
    return 0


def cmd_replay(storage: EventStorage, event_id: str | None, replay_all: bool) -> int:
    if replay_all:
        count = storage.replay_all_dead()
        sys.stdout.write(f"Reset {count} dead-letter events back to PENDING.\n")
        return 0
    if not event_id:
        sys.stderr.write("Error: Specify an event_id or --all.\n")
        return 1

    ok = storage.replay_event(event_id)
    if ok:
        sys.stdout.write(f"Event {event_id} reset to PENDING.\n")
        return 0
    sys.stderr.write(f"Error: Event {event_id} not found.\n")
    return 1


def cmd_verify(secret: str, signature: str, payload: str) -> int:
    valid = SignatureVerifier.verify_sha256(payload, secret, signature)
    if valid:
        sys.stdout.write("Signature valid (HMAC-SHA256 OK).\n")
        return 0
    sys.stderr.write("Signature INVALID!\n")
    return 1


def cmd_start(args, storage: EventStorage) -> int:
    cfg = GatewayConfig(
        storage=storage,
        target_url=args.target,
        secret=args.secret,
        max_attempts=args.max_attempts,
    )
    server = WebhookGatewayServer((args.host, args.port), cfg)

    dispatcher = None
    if not args.no_worker:
        dispatcher = WebhookDispatcher(storage=storage)
        dispatcher.start()

    sys.stdout.write(f"HookGuard gateway listening on http://{args.host}:{args.port}\n")
    sys.stdout.write(f"Forwarding target: {args.target}\n")
    sys.stdout.write(f"Worker dispatcher: {'DISABLED' if args.no_worker else 'ACTIVE'}\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stdout.write("\nShutting down HookGuard...\n")
    finally:
        server.server_close()
        if dispatcher is not None:
            dispatcher.stop()
        storage.close()

    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI execution entrypoint."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    storage = EventStorage(db_path=args.db)

    try:
        if args.command == "stats":
            return cmd_stats(storage)
        if args.command == "list":
            return cmd_list(storage, args.status, args.limit)
        if args.command == "replay":
            return cmd_replay(storage, args.event_id, args.all)
        if args.command == "verify":
            return cmd_verify(args.secret, args.signature, args.payload)
        if args.command == "start":
            return cmd_start(args, storage)
    finally:
        if args.command != "start":
            storage.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
