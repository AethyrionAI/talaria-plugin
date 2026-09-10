"""3D artifact mirror (#362): mirror agent-written files to the phone.

The ``/v1/runs`` event stream drops tool ``args`` entirely, so the phone's
Tier-1 file reconstruction (#21) has no content source on the runs plane.
This module is the replacement source: a ``pre_tool_call`` observer that
sees ``write_file``/``create_file`` args on ANY lane and appends the
content to the outbox as a ``kind="artifact"`` item the app correlates by
``session_id`` + ``path`` (turn_id rides along for bookkeeping only — the
app can never learn it from the runs stream).

Two contracts, both pinned by tests:

- **Fail-closed.** No row unless every gate passes: a write tool, a
  non-empty session_id, an API-plane turn (the phone's plane — CLI,
  Discord and desktop turns must not queue their files at the phone), a
  parseable ``{path, content}``, and at least one active device. Each
  active device gets its own independently acknowledged targeted row
  (#366 — the v0 exactly-one gate silently dropped every artifact on a
  two-device host, measured on a two-device host, 2026-08-18).
- **Never raise, never block.** The hook runs synchronously on the tool
  dispatch hot path. Local SQLite append only; any exception is swallowed
  to a debug log line. The gateway would swallow a raise anyway, but this
  module does not rely on that.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import hygiene, outbox, store
from .transport import HUB

logger = logging.getLogger("talaria")

_WRITE_TOOLS = {"write_file", "create_file"}

# The app's WrittenFileArgs tolerates this key drift; the mirror must not
# be stricter than the sessions plane it replaces.
_PATH_KEYS = ("path", "file_path", "filename")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _api_plane() -> bool:
    """True only for turns entering through the api_server (the phone's
    plane — sessions chat and ``/v1/runs`` both bind
    ``platform="api_server"`` into ``gateway.session_context``). Import
    errors and unset context both read False: fail-closed."""
    try:
        from gateway.session_context import get_session_env

        return get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server"
    except Exception:
        return False


def _active_device_ids() -> list[str]:
    return [device["id"] for device in store.active_devices()]


def _on_pre_tool_call(
    tool_name: str = "",
    args=None,
    task_id: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    turn_id: str = "",
    **_kwargs,
) -> None:
    try:
        if tool_name not in _WRITE_TOOLS or not session_id or not _api_plane():
            return None
        if not isinstance(args, dict):
            return None
        path = next((args[k] for k in _PATH_KEYS if args.get(k)), None)
        content = args.get("content")
        if not path or not isinstance(content, str):
            return None
        device_ids = _active_device_ids()
        if not device_ids:
            return None
        outbox.append_for_devices(
            content,
            device_ids,
            meta={
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_call_id": tool_call_id,
                "path": str(path),
                "ts": _utc_now_iso(),
                "type": "written_file",
            },
            kind="artifact",
        )
        for device_id in device_ids:
            HUB.wake(device_id)
        # #363: the growth source pays for its own hygiene — throttled to
        # one sweep per 6 h, and maybe_sweep itself never raises.
        hygiene.maybe_sweep()
    except Exception:
        logger.debug("artifact mirror skipped a write", exc_info=True)
    return None


def register_hooks(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
