"""Talaria dashboard plugin — the read-only status backend for the desktop
pane (#270 v0). Mounted at ``/api/plugins/talaria/`` by
``web_server._mount_plugin_api_routes()`` and served by the desktop-spawned
headless ``hermes serve`` (and the ``:9119`` dashboard, where the tab stays
hidden — v0 is a desktop surface).

Security (bar 270-E): every route sits behind ``web_server.auth_middleware``
— the session bearer token — like all ``/api/plugins/*`` routes, and the
device projection is a CLOSED field list (id/name/active/last_seen).
``token_sha256`` is an at-rest artifact; it does not belong on any wire.

The LIVE verdict is a cross-process fact: this backend runs in the desktop's
``serve`` process, but the phone talks to the GATEWAY process on the
api_server port — so "is the adapter live" is answered by probing the
gateway's own events route unauthenticated (401 = adapter registered, and
auth rejection precedes verb dispatch so nothing drains; 503 = platform
absent) — the same verified seam the iOS app's #269-A probe classifies.
Reading this process's own registry instead would be the in-process-counters
trap (see ``talaria/admin.py``) wearing a hat.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter

# The loader imports this file by PATH (no package context), so the plugin
# root goes on sys.path and the packaged implementation is imported the same
# way the root shim's bare-module branch does. No pip distribution of
# ``talaria`` exists (#351-K), so this cannot bind anything else.
_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(_PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_ROOT))

from talaria import store  # noqa: E402

router = APIRouter()

# 270-E: the closed projection. Everything else stays off the wire.
_DEVICE_FIELDS = ("id", "name", "active", "last_seen")

_PROBE_TIMEOUT_SECONDS = 3.0


def _plugin_version() -> str:
    """Line-scan ``plugin.yaml`` for the version — no YAML dependency, no
    parsing surprises, honest ``unknown`` when unreadable."""
    manifest = _PLUGIN_ROOT / "plugin.yaml"
    try:
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def device_projection(rows: list[dict]) -> list[dict]:
    return [{field: row.get(field) for field in _DEVICE_FIELDS} for row in rows]


def classify_probe_status(status: int) -> str:
    """The #269-A classification, verbatim: only 401 and 503 license a
    verdict; any other status is honestly indeterminate."""
    if status == 401:
        return "live"
    if status == 503:
        return "absent"
    return "indeterminate"


def _gateway_events_url() -> str:
    port = os.environ.get("API_SERVER_PORT", "8642")
    return f"http://127.0.0.1:{port}/api/platforms/talaria/events"


def _probe_once(target: str) -> dict:
    """Unauthenticated POST to the gateway's events route. Side-effect-free:
    auth rejection precedes verb dispatch, so a registered adapter answers
    401 without draining or acking anything."""
    request = urllib.request.Request(
        target,
        data=b"{}",
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_PROBE_TIMEOUT_SECONDS) as response:
            status = int(response.status)
    except urllib.error.HTTPError as error:
        status = int(error.code)
    except (urllib.error.URLError, OSError, TimeoutError):
        return {"observation": "unreachable", "status": None, "url": target}
    return {"observation": classify_probe_status(status), "status": status, "url": target}


# #362 3D-F: the pane polls /status every 5 s, and every uncached poll fired
# one probe POST — ~17k access-log 401 lines/day in the gateway's agent.log,
# drowning any GENUINE auth failure on the same route. Verdicts (401 live /
# 503 absent) hold for 60 s; non-verdicts (unreachable, indeterminate) retry
# after 10 s so a recovered gateway isn't reported down for a minute. Keyed
# per URL; a racing double-probe under the FastAPI threadpool is harmless
# (both writes store an equally fresh result), so there is no lock.
_PROBE_CACHE_OK_SECONDS = 60.0
_PROBE_CACHE_FAIL_SECONDS = 10.0
_PROBE_VERDICTS = frozenset({"live", "absent"})
_probe_cache: dict[str, tuple[float, dict]] = {}


def probe_gateway_adapter(url: str | None = None, *, now=time.monotonic) -> dict:
    target = url or _gateway_events_url()
    stamp = now()
    cached = _probe_cache.get(target)
    if cached is not None:
        cached_at, cached_result = cached
        floor = (
            _PROBE_CACHE_OK_SECONDS
            if cached_result.get("observation") in _PROBE_VERDICTS
            else _PROBE_CACHE_FAIL_SECONDS
        )
        if stamp - cached_at < floor:
            return cached_result
    result = _probe_once(target)
    _probe_cache[target] = (stamp, result)
    return result


@router.get("/status")
def status() -> dict:
    """Everything the pane renders, one read-only round trip.

    Sync on purpose: FastAPI runs sync handlers in its threadpool, so the
    SQLite read and the 3-second probe never block the backend's event loop
    (the #351-E discipline, inherited).
    """
    return {
        "plugin": {"name": "talaria", "version": _plugin_version()},
        "adapter": probe_gateway_adapter(),
        "devices": device_projection(store.devices()),
    }
