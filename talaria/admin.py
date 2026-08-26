"""Admin CLI for the Talaria plugin: ``hermes talaria pair-qr|status|unpair``.

Replaces the legacy venv pairing CLIs. Device tokens are minted only by the
wire ``pair`` verb (the app's own handshake) and only ever persist as
hashes; see store.py.

**The manual ``pair`` subcommand is gone (#309 Lane D / #412).** It minted a
device row with no ``install_id``, so nothing could rotate it and the app
had no redemption path for its token — a flow the Pairing & Devices screen
advertised and that dead-ended at both ends. ``pair-qr`` replaces it: it
mints nothing, and hands the phone the gateway credentials the host
already has.
"""

from __future__ import annotations

from . import store
from .database import database_path


def setup_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="talaria_cmd")
    pair_qr = subs.add_parser(
        "pair-qr",
        help="Print this host's gateway credentials as a QR for the Talaria iOS app",
    )
    pair_qr.add_argument(
        "--gateway",
        help="Gateway URL to advertise (default: derived from api_server config + this host's tailnet address)",
    )
    pair_qr.add_argument(
        "--name", help="Host label the app shows for the profile (default: this machine's hostname)",
    )
    pair_qr.add_argument("--png", help="Also write the QR to this PNG path")
    pair_qr.add_argument(
        "--no-color", dest="no_color", action="store_true",
        help="Draw with plain half-blocks instead of ANSI colours (for piped output)",
    )
    subs.add_parser("status", help="Show paired devices and plugin phase state")
    unpair = subs.add_parser("unpair", help="Deactivate a paired device (records are kept, never deleted)")
    unpair.add_argument(
        "device_id", nargs="?", default=None,
        help="Device id from `hermes talaria status` (omit to deactivate all)",
    )
    prune = subs.add_parser(
        "prune",
        help="Scrub/expire artifact outbox rows past the 7-day retention (rows are kept, never deleted)",
    )
    prune.add_argument(
        "--dry-run", dest="dry_run", action="store_true",
        help="Report what the sweep would touch without writing anything",
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
    if cmd == "pair-qr":
        return _handle_pair_qr(args)
    elif cmd == "unpair":
        count = store.deactivate(getattr(args, "device_id", None))
        if count:
            print(f"Deactivated {count} device record(s). Records are kept for rollback.")
            return 0
        print("No matching active device record.")
        return 1
    elif cmd == "prune":
        from . import hygiene
        dry_run = bool(getattr(args, "dry_run", False))
        counts = hygiene.sweep(dry_run=dry_run)
        verb = "Would scrub" if dry_run else "Scrubbed"
        print(
            f"{verb} {counts['scrubbed']} delivered and "
            f"{'would expire' if dry_run else 'expired'} {counts['expired']} undelivered "
            f"artifact row(s) past {hygiene.RETENTION_DAYS:g}-day retention. "
            "Rows are kept, never deleted."
        )
        return 0
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
            print("No paired devices. Run `hermes talaria pair-qr` and scan it with the app.")
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


def _handle_pair_qr(args) -> int:
    """Print this host's gateway credentials as a scannable QR.

    Nothing is minted and nothing is stored: the payload carries the
    ``API_SERVER_KEY`` the gateway already serves and the URL the phone
    should reach it at. The key is printed only INSIDE the QR — the prose
    around it is masked, and this path writes no log record at all, so the
    credential cannot survive in a log the way a printed token would.
    """
    from . import pairing_qr

    try:
        gateway_url, provenance = pairing_qr.resolve_gateway_url(getattr(args, "gateway", None))
        name = pairing_qr.resolve_host_name(getattr(args, "name", None))
        api_key = pairing_qr.resolve_api_key()
        payload = pairing_qr.build_payload(gateway_url=gateway_url, api_key=api_key, name=name)
        text = pairing_qr.encode_payload(payload)
        art = pairing_qr.render_ansi(text, color=not getattr(args, "no_color", False))
    except pairing_qr.PairingQRError as exc:
        print(f"Cannot build a pairing QR: {exc}")
        return 1

    png_path = getattr(args, "png", None)
    written = None
    if png_path:
        try:
            written = pairing_qr.write_png(text, png_path)
        except pairing_qr.PairingQRError as exc:
            print(f"Cannot write the PNG: {exc}. Nothing was written.")
            return 1
        except OSError as exc:
            print(f"Cannot write the PNG: {exc}. Nothing was written.")
            return 1

    print("Scan this with the Talaria app (Settings → Connect Host):")
    print()
    print(art)
    print()
    print(f"  gateway  {gateway_url}   [{provenance}]")
    print(f"  name     {name}")
    print(f"  key      {pairing_qr.mask_key(api_key)}   [read at print time, not stored]")
    if written is not None:
        print(f"  png      {written}")
    print()
    print("The QR carries this host's API_SERVER_KEY. Treat it like the key")
    print("itself: do not photograph it for anyone else, and rotate the key in")
    print("HERMES_HOME's .env (then restart the gateway) if it leaks.")
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
        help="Talaria phone bridge admin (pair-qr, status, unpair, send)",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="Pairing and status admin for the Talaria iOS app's Hermes bridge.",
    )
