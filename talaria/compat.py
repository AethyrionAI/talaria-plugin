"""Compatibility floor (#308's ruled full stack, Talaria-27 tracker).

The floor names the oldest Hermes version this plugin is LIVE-verified
on — not the oldest it might work on. It warns loudly and loads anyway:
raising here would violate register()'s own "never break gateway load"
contract, and a partial registration with a named cause beats a refusal.
"""

# Oldest host version the plugin is live-verified on (measured on the reference host
# 0.20.3 with the plugin serving, 2026-08-18 — Talaria-27 #347/#349).
HERMES_FLOOR = (0, 20, 3)


def check_hermes_floor(version_str=None):
    """Compare the running Hermes version against HERMES_FLOOR.

    Returns the warning line it printed when below the floor, else None.
    Total by design: unreadable or unparseable versions skip, never raise.
    """
    try:
        if version_str is None:
            from hermes_cli import __version__ as version_str
        parts = tuple(int(piece) for piece in version_str.split(".")[:3])
        if parts < HERMES_FLOOR:
            floor = ".".join(str(piece) for piece in HERMES_FLOOR)
            warning = (
                f"[talaria] COMPATIBILITY FLOOR: running Hermes "
                f"{version_str} is below the oldest live-verified version "
                f"{floor}; loading anyway, but registration failures below "
                f"this line are expected — update Hermes or install an "
                f"older plugin ref"
            )
            print(warning)
            return warning
    except Exception as exc:
        print(f"[talaria] hermes version check skipped: {exc}")
    return None
