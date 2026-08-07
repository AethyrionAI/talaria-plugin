"""Admin CLI for the Talaria plugin: ``hermes talaria pair|status|unpair``.

Replaces the legacy venv pairing CLIs. Pairing tokens print exactly once;
only hashes persist (see store.py).
"""

from __future__ import annotations

from . import store


def setup_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="talaria_cmd")
    subs.add_parser("pair", help="Create a one-time pairing token for the Talaria iOS app")
    subs.add_parser("status", help="Show paired devices and plugin phase state")
    unpair = subs.add_parser("unpair", help="Deactivate a paired device (records are kept, never deleted)")
    unpair.add_argument(
        "device_id", nargs="?", default=None,
        help="Device id from `hermes talaria status` (omit to deactivate all)",
    )
    send = subs.add_parser("send", help="Queue a message for the phone's next drain")
    send.add_argument("text", nargs="*", help="Message text to queue for the phone")


def handle_cli(args) -> None:
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
    elif cmd == "unpair":
        count = store.deactivate(getattr(args, "device_id", None))
        if count:
            print(f"Deactivated {count} device record(s). Records are kept for rollback.")
        else:
            print("No matching active device record.")
    elif cmd == "send":
        from . import outbox
        text = " ".join(getattr(args, "text", []) or []).strip()
        if not text:
            print("Usage: hermes talaria send <text>")
            return
        item = outbox.append(text, meta={"source": "cli"})
        print(f"Queued outbox item {item['id']} — delivered on the phone's next drain.")
    else:  # status is also the default
        records = store.devices()
        active = [d for d in records if d.get("active")]
        print("Talaria plugin — tools + admin + webhook platform adapter (2A)")
        print(f"Store: {store._store_path()}")
        if not records:
            print("No paired devices. Run `hermes talaria pair` to create one.")
            _print_transport_counters()
            return
        print(f"{len(active)} active / {len(records)} total device record(s):")
        for device in records:
            state = "active" if device.get("active") else "inactive"
            last_seen = device.get("last_seen") or "—"
            print(f"  {device['id']}  {state:8}  created {device.get('created', '—')}  last seen {last_seen}")
        _print_transport_counters()


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
