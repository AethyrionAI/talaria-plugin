"""Tool surface for the Talaria plugin.

Phase 1 registers the ``talaria_phone_query`` scaffold with a ``check_fn``
that reports the toolset unavailable until a live phone transport exists
(Phase 2's webhook adapter). The gate keeps the model from burning turns
on a tool that cannot succeed yet, while ``hermes tools`` already shows
the real shape. The handler stays honest if invoked anyway.
"""

from __future__ import annotations

from . import store

_SCHEMAS = {
    "talaria_phone_query": {
        "type": "function",
        "function": {
            "name": "talaria_phone_query",
            "description": (
                "Ask the paired Talaria iPhone a question about its own state "
                "(location, battery, health/motion snapshot, reminders). The "
                "phone answers at query time with its local brain — nothing "
                "is ingested or stored server-side. Fails honestly when no "
                "phone is reachable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Natural-language question for the phone, e.g. 'where is the phone right now?'",
                    },
                },
                "required": ["question"],
            },
        },
    },
}


def _transport_available() -> bool:
    """check_fn: True only when a paired device has a live transport.

    Phase 1 has no transport at all, so this is False whenever it is
    honest to say so — which is always. Phase 2's webhook adapter flips
    this by recording a fresh ``last_seen`` heartbeat.
    """
    return False


def phone_query(args: dict, **kwargs) -> str:
    question = ((args or {}).get("question") or "").strip()
    if not question:
        return "No question was given — nothing to ask the phone."
    if not store.active_devices():
        return (
            "Phone unreachable: no Talaria device is paired with this host. "
            "The user can pair one with `hermes talaria pair`. Do not retry this turn."
        )
    return (
        "Phone unreachable: a device is paired but no live transport to it is "
        "connected yet (Talaria plugin Phase 1 — the webhook adapter arrives in "
        "Phase 2). Do not retry this turn."
    )


def register_tools(ctx) -> None:
    for name, schema in _SCHEMAS.items():
        ctx.register_tool(
            name=name,
            toolset="talaria",
            schema=schema,
            handler=phone_query,
            check_fn=_transport_available,
            description=schema["function"]["description"],
            emoji="\U0001fabd",  # 🪽 — the closest thing to a winged sandal
        )
