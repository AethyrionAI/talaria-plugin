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


def handle_cli(args) -> None:
    cmd = getattr(args, "talaria_cmd", None)
    if cmd == "pair":
        device_id, token = store.create_pairing()
        print(f"Paired device record created: {device_id}")
        print()
        print("One-time pairing token (not stored — copy it into the Talaria app now):")
        print(f"  {token}")
        print()
        print("Note: the app-side handshake that consumes this token ships with the")
        print("Phase 2 webhook adapter. This record marks the host side as ready.")
    elif cmd == "unpair":
        count = store.deactivate(getattr(args, "device_id", None))
        if count:
            print(f"Deactivated {count} device record(s). Records are kept for rollback.")
        else:
            print("No matching active device record.")
    else:  # status is also the default
        records = store.devices()
        active = [d for d in records if d.get("active")]
        print("Talaria plugin — Phase 1 (tools + admin; webhook transport lands in Phase 2)")
        print(f"Store: {store._store_path()}")
        if not records:
            print("No paired devices. Run `hermes talaria pair` to create one.")
            return
        print(f"{len(active)} active / {len(records)} total device record(s):")
        for device in records:
            state = "active" if device.get("active") else "inactive"
            last_seen = device.get("last_seen") or "—"
            print(f"  {device['id']}  {state:8}  created {device.get('created', '—')}  last seen {last_seen}")


def register_cli(ctx) -> None:
    ctx.register_cli_command(
        name="talaria",
        help="Talaria phone bridge admin (pair, status, unpair)",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Pairing and status admin for the Talaria iOS app's Hermes bridge.",
    )
