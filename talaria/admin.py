"""Admin CLI for the Talaria plugin: ``hermes talaria pair|status|unpair``.

Replaces the legacy venv pairing CLIs. Pairing tokens print exactly once;
only hashes persist (see store.py).
"""

from __future__ import annotations

from . import store
from .database import database_path


def setup_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="talaria_cmd")
    subs.add_parser("pair", help="Create a one-time pairing token for the Talaria iOS app")
    subs.add_parser("status", help="Show paired devices and plugin phase state")
    unpair = subs.add_parser("unpair", help="Deactivate a paired device (records are kept, never deleted)")
    unpair.add_argument(
        "device_id", nargs="?", default=None,
        help="Device id from `hermes talaria status` (omit to deactivate all)",
    )
    send = subs.add_parser("send", help="Queue a message for a specific active Talaria device")
    send.add_argument("text", nargs="*", help="Message text to queue for the device")
    target = send.add_mutually_exclusive_group()
    target.add_argument("--device", help="Target one active device id")
    target.add_argument(
        "--all", dest="send_all", action="store_true",
        help="Fan out one independently acknowledged delivery per active device",
    )


def handle_cli(args) -> int:
    cmd = getattr(args, "talaria_cmd", None)
    if cmd == "pair":
        device_id, token = store.create_pairing()
        print(f"Paired device record created: {device_id}")
        print()
        print("One-time pairing token (not stored — copy it into the Talaria app now):")
        print(f"  {token}")
        print()
        print("Note: the Talaria app can also pair itself directly against the")
        print("platform adapter — this command remains the manual fallback path.")
        print("CLI-paired records do not auto-rotate when the app re-pairs;")
        print("unpair this id manually if the app later pairs itself.")
        return 0
    elif cmd == "unpair":
        count = store.deactivate(getattr(args, "device_id", None))
        if count:
            print(f"Deactivated {count} device record(s). Records are kept for rollback.")
            return 0
        print("No matching active device record.")
        return 1
    elif cmd == "send":
        from . import outbox
        text = " ".join(getattr(args, "text", []) or []).strip()
        if not text:
            print("Usage: hermes talaria send [--device <id> | --all] <text>")
            return 1

        active = store.active_devices()
        requested_device = getattr(args, "device", None)
        if getattr(args, "send_all", False):
            if not active:
                print("No active Talaria devices. Pair a device before sending.")
                return 1
            try:
                items = outbox.append_for_devices(
                    text,
                    [device["id"] for device in active],
                    meta={"source": "cli"},
                )
            except outbox.UnknownTargetError as exc:
                print(f"Send failed: {exc}. No message was queued.")
                return 1
            print(f"Queued {len(items)} targeted outbox item(s), one per active device.")
            return 0

        if requested_device is None:
            if not active:
                print("No active Talaria devices. Pair a device before sending.")
                return 1
            if len(active) > 1:
                print("Multiple active devices; choose --device <id> or --all. No message was queued.")
                return 1
            requested_device = active[0]["id"]

        try:
            item = outbox.append(
                text,
                meta={"source": "cli"},
                target_device_id=requested_device,
            )
        except outbox.UnknownTargetError as exc:
            print(f"Send failed: {exc}. No message was queued.")
            return 1
        print(f"Queued outbox item {item['id']} for device {requested_device}.")
        return 0
    else:  # status is also the default
        records = store.devices()
        active = [d for d in records if d.get("active")]
        print("Talaria plugin — tools + admin + webhook platform adapter")
        print(f"Store: {database_path()}")
        if not records:
            print("No paired devices. Run `hermes talaria pair` to create one.")
            _print_transport_counters()
            return 0
        print(f"{len(active)} active / {len(records)} total device record(s):")
        for device in records:
            state = "active" if device.get("active") else "inactive"
            last_seen = device.get("last_seen") or "—"
            name = device.get("name") or "—"
            install = device.get("install_id") or "—"
            print(
                f"  {device['id']}  {state:8}  {name}  install {install}  "
                f"created {device.get('created', '—')}  last seen {last_seen}"
            )
        _print_transport_counters()
        return 0


def _print_transport_counters() -> None:
    """#263-E: make a transport forensic a CLI call instead of a log crawl.

    These are IN-PROCESS counters. The bare CLI is a separate process from
    the gateway, so it reports no activity; the numbers are live when this
    runs inside the gateway. Say which is which rather than printing a
    misleading row of zeros.
    """
    from .transport import HUB

    counters = HUB.counters
    print()
    print(f"Transport hub {id(HUB)} (this process):")
    if not any(counters.values()):
        print("  no transport activity in this process — expected from the")
        print("  bare CLI; the gateway process holds the live hub.")
        return
    print(f"  queries enqueued / delivered  {counters['queries_enqueued']} / {counters['queries_delivered']}")
    print(f"  parks woken / timed out       {counters['parks_woken']} / {counters['parks_timed_out']}")
    print(f"  wakes MISSED                  {counters['wakes_missed']}")
    print(f"  full-cycle deliveries         {counters['full_cycle_deliveries']}")
    if counters["wakes_missed"] or counters["full_cycle_deliveries"]:
        print("  ^ nonzero means a wake failed to release a parked drain (#263(b))")


def register_cli(ctx) -> None:
    ctx.register_cli_command(
        name="talaria",
        help="Talaria phone bridge admin (pair, status, unpair, send)",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Pairing and status admin for the Talaria iOS app's Hermes bridge.",
    )
