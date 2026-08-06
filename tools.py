"""Tool surface for the Talaria plugin.

2A: the structured phone-query catalog goes LIVE over the platform
adapter's drain transport. check_fn flips on transport liveness so the
model never burns a turn on a dead transport (Phase 1 rule, kept).
"""

from __future__ import annotations

import asyncio

from . import store

_QUERY_TIMEOUT = 25.0
_LIVE_WINDOW_SECONDS = 60.0

_KINDS = ["location", "health", "motion", "weather", "calendar", "reminders", "deviceStatus"]

_SCHEMAS = {
    "talaria_phone_query": {
        "type": "function",
        "function": {
            "name": "talaria_phone_query",
            "description": (
                "Ask the paired Talaria iPhone for its own data at query time "
                "(nothing is ingested or stored server-side). Kinds: location, "
                "health (params.metric: steps|calories|heartRate|sleep|summary), "
                "motion, weather, calendar (params.window_days), reminders, "
                "deviceStatus. Fails honestly when no phone is reachable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": _KINDS},
                    "params": {
                        "type": "object",
                        "description": "Kind-specific string parameters, e.g. {\"metric\": \"steps\"}.",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["kind"],
            },
        },
    },
}


def _hub():
    from .transport import HUB
    return HUB


def _transport_available() -> bool:
    return _hub().is_live(_LIVE_WINDOW_SECONDS)


async def phone_query(args: dict, **kwargs) -> str:
    kind = ((args or {}).get("kind") or "").strip()
    if kind not in _KINDS:
        return f"Unknown query kind \"{kind}\" — supported: {', '.join(_KINDS)}."
    hub = _hub()
    if not hub.is_live(_LIVE_WINDOW_SECONDS):
        if not store.active_devices():
            return (
                "Phone unreachable: no Talaria device is paired with this host. "
                "The user can pair by opening the Talaria app. Do not retry this turn."
            )
        return (
            "Phone unreachable: the paired phone is not connected right now "
            "(the app is probably closed). Do not retry this turn."
        )
    device_id = hub.freshest_device()
    query_id, future = hub.enqueue_query(device_id, kind, (args or {}).get("params") or {})
    try:
        answer = await asyncio.wait_for(future, timeout=_QUERY_TIMEOUT)
    except asyncio.TimeoutError:
        return "The phone did not answer in time — it may have just gone to background. Do not retry this turn."
    finally:
        # Always discard, not just on the timeout path: a resolved future
        # was already popped from hub._futures by resolve_query, and any
        # already-drained _queries entry was already popped by
        # take_queries, so this is a safe no-op there. Calling it
        # unconditionally means there is no second "did this actually time
        # out" branch to get wrong.
        hub.discard_query(query_id)
    if isinstance(answer, dict) and answer.get("error"):
        if answer["error"] == "permission_denied":
            return _declined_prose(answer)
        return f"The phone could not answer: {str(answer['error'])[:200]}. Do not retry this turn."
    if isinstance(answer, dict) and isinstance(answer.get("text"), str):
        return answer["text"]
    return "The phone sent an unreadable answer."


def _declined_prose(answer: dict) -> str:
    """#260(B): name the gate that actually refused, when the app says.

    Three shapes: master (one switch gates everything), stream (a specific
    sensor toggle, possibly not the one the query kind suggests — weather is
    gated by Location), and the bare pre-#260 denial, which keeps the generic
    prose byte-identical so old apps degrade to shipped behavior.
    """
    gate = answer.get("denied_gate")
    if gate == "master":
        return (
            'The phone declined: the master "Share Sensors with Hermes" switch is '
            "off in Talaria's privacy settings. That one switch gates ALL sensor "
            "sharing — streams and queries alike — so flipping an individual "
            "sensor toggle will not unblock this."
        )
    stream = answer.get("denied_stream")
    if gate == "stream" and isinstance(stream, str) and stream:
        label = stream.capitalize()
        return (
            f"The phone declined: the {label} sensor toggle is off in Talaria's "
            f"privacy settings. The master sensor switch is on, so enabling "
            f"{label} is what unblocks this."
        )
    return (
        "The phone declined: permission for that data stream is disabled in "
        "Talaria's privacy settings."
    )


def register_tools(ctx) -> None:
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="talaria",
            schema=schema,
            handler=phone_query,
            check_fn=_transport_available,
            is_async=True,
            description=schema["function"]["description"],
            emoji="\U0001fabd",
        )
