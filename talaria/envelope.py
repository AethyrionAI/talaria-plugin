"""Envelope core for the Talaria platform adapter (spec §1.1).

Pure logic, dependency-injected for tests; platform_adapter.py wraps it. The
route verifies the HEADER (authentication — bad creds 401 before
dispatch); dispatch authorizes from the payload's `auth` field (spec
Addendum): pair requires the API key, device ops require the device's
own token bound to the claimed device_id. Every failure is a clean
error dict — the route 500s on raised exceptions, so dispatch() wraps
its handlers in a catch-all and verify() guards its storage call
(#351-A): the promise is enforced here, not assumed of storage.

Payload fields arrive as parsed JSON from an untrusted HTTP body, so
their Python types are not guaranteed to match the documented shape
(a client can send `"auth": 123` or `"item_ids": "not-a-list"`).
Every payload.get() that feeds a type-sensitive call — hmac.compare_
digest, str.strip, dict hashing/pop, set() — is guarded so a
malformed type degrades to a clean error instead of an unhandled
exception; see `_text()` and the isinstance checks below.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from datetime import datetime, timezone

_logger = logging.getLogger("talaria")


def _bearer(auth_header: str) -> str:
    if not isinstance(auth_header, str) or not auth_header.startswith("Bearer "):
        return ""
    return auth_header[7:].strip()


def _ct_equal(a: str, b: str) -> bool:
    """Constant-time string compare that never raises on non-ASCII.

    hmac.compare_digest requires both str operands to be ASCII-only —
    a non-ASCII bearer token or a non-ASCII configured API key raises
    TypeError on the str/str path, which on the UNAUTHENTICATED verify()
    surface means a 500 instead of a clean 401 (and a non-ASCII key
    would make every verify() call raise — total outage). The bytes/
    bytes path has no such restriction and is still constant-time.
    """
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def _text(value) -> str:
    """Best-effort string field: strips a real string, else empty.

    Guards against a JSON payload supplying a non-string for a field
    that's normally text (e.g. `"install_id": 5`) — `.strip()` on a
    non-string would raise AttributeError otherwise.
    """
    return value.strip() if isinstance(value, str) else ""


class EnvelopeService:
    def __init__(self, api_key_provider, hub, store_mod, outbox_mod,
                 hold_seconds: float = 25.0, touch_throttle_seconds: float = 60.0):
        self._api_key = api_key_provider
        self._hub = hub
        self._store = store_mod
        self._outbox = outbox_mod
        self._hold = hold_seconds
        self._touch_throttle = touch_throttle_seconds
        self._last_store_touch: dict[str, float] = {}
        # #383: in-process only, and deliberately so — a voice session is
        # meaningful for the minutes it is live, and the ephemeral secret
        # the phone holds expires on its own. Persisting it would build a
        # store whose only reader is its own cleanup.
        self._voice_sessions: dict[str, str] = {}

    # -- route-level authentication ---------------------------------------
    def verify(self, auth_header: str) -> tuple[bool, str]:
        token = _bearer(auth_header)
        if not token:
            return False, "missing_bearer"
        key = self._api_key() or ""
        if key and _ct_equal(token, key):
            return True, ""
        try:
            device = self._store.device_for_token(token)
        except Exception:
            # #351-A: a storage failure on the UNAUTHENTICATED surface must
            # fail closed as a clean 401, never as a raise into the route.
            _logger.exception("talaria: verify failed on a storage error")
            return False, "storage_error"
        if device is not None:
            return True, ""
        return False, "invalid_talaria_auth"

    # -- per-type authorization helpers ------------------------------------
    def _is_api_key(self, value) -> bool:
        if not isinstance(value, str):
            return False
        key = self._api_key() or ""
        return bool(key) and _ct_equal(value, key)

    async def _device_authorized(self, payload: dict) -> dict | None:
        auth = payload.get("auth")
        if not isinstance(auth, str):
            return None
        device = await asyncio.to_thread(self._store.device_for_token, auth)
        if device is None:
            return None
        device_id = device.get("id")
        if not device_id or device_id != payload.get("device_id"):
            return None
        return device

    # -- dispatch -----------------------------------------------------------
    async def dispatch(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            return {"error": "Malformed event payload", "code": "malformed_payload"}
        event_type = payload.get("type")
        handler = {
            "pair": self._pair,
            "drain": self._drain,
            "ack": self._ack,
            "query_result": self._query_result,
            "unpair": self._unpair,
            # #383: the realtime voice bootstrap, re-homed off the retired
            # relay/connector pair. Additive — the five verbs above are
            # untouched, so a half-deployed plugin still serves chat and
            # sensors normally.
            "talk_readiness": self._talk_readiness,
            "talk_session_create": self._talk_session_create,
            "talk_session_end": self._talk_session_end,
        }.get(event_type) if isinstance(event_type, str) else None
        if handler is None:
            return {"error": "Unknown event type", "code": "unknown_event_type"}
        try:
            return await handler(payload)
        except Exception:
            # #351-A: storage failures degrade to a clean error dict; the
            # docstring's "nothing raises" promise is enforced here rather
            # than assumed of every storage call.
            _logger.exception("talaria: %s handler failed on a storage error", event_type)
            return {"error": "Internal storage failure", "code": "storage_error"}

    # -- #383: realtime voice ------------------------------------------------
    #
    # `talk_turn_append` is deliberately ABSENT. Investigated 2026-08-22: the
    # app never reads voice turns back (the POST had no GET), #1's
    # `postVoiceTranscriptsToHermes` already posts transcripts as normal
    # Sessions-API turns, and the relay verb bypassed the user's own
    # "post voice transcripts" setting. Porting it would rebuild a
    # toggle-bypassing transcript path on purpose. Owen's call; not built.

    async def _talk_readiness(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        from . import voice
        return await asyncio.to_thread(voice.readiness)

    async def _talk_session_create(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        from . import voice

        api_key = await asyncio.to_thread(voice.resolve_openai_api_key)
        if not api_key:
            # A clean, NAMED refusal rather than a 500: the app surfaces
            # `blockedReason` to the user, and "not configured" is a state a
            # user can act on (#180 — degrade honestly, never silently).
            return {
                "error": "OpenAI Realtime is not configured on this Hermes host.",
                "code": "talk_not_configured",
            }

        instructions = await asyncio.to_thread(voice.build_voice_instructions)
        # #396: the coarse picker's tuning rides the payload. Like every other
        # payload field its type is untrusted — the guard here is that
        # `resolve_turn_detection` is the sanitizer: only the exact vetted
        # names select a preset, and any junk value (wrong type included)
        # logs once and yields the env-resolved default, so a malformed field
        # degrades to today's behaviour rather than an error.
        turn_detection = await asyncio.to_thread(
            voice.resolve_turn_detection, tuning=payload.get("tuning")
        )
        try:
            session_payload, model = await asyncio.to_thread(
                voice.create_realtime_session,
                api_key=api_key,
                instructions=instructions,
                turn_detection=turn_detection,
            )
        except RuntimeError as error:
            # Every candidate model refused. The message is the provider's own
            # and is worth forwarding — a bare "failed" here is what makes a
            # voice bootstrap undebuggable from the phone.
            return {"error": str(error), "code": "talk_session_create_failed"}

        bootstrap = voice.normalize_bootstrap(
            session_payload, model, voice.DEFAULT_REALTIME_VOICE
        )
        # DASHED, not .hex: the app decodes this into a Swift `UUID`, whose
        # `UUID(uuidString:)` rejects an undashed 32-char string. A hex id
        # would fail to decode on a shipped client that cannot be changed.
        voice_session_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        self._voice_sessions[voice_session_id] = started_at
        # The app's decode target is frozen by a shipped client, so this shape
        # matches the relay's `serialize_voice_session` rather than inventing
        # a cleaner one.
        return {
            "voiceSession": {
                "id": voice_session_id,
                "status": "active",
                "model": bootstrap.get("model"),
                "voice": bootstrap.get("voice"),
                "startedAt": started_at,
                "endedAt": None,
                "lastError": None,
            },
            "bootstrap": bootstrap,
        }

    async def _talk_session_end(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        voice_session_id = _text(payload.get("voice_session_id"))
        # An end for a session this process does not remember still ACKS.
        # Two ordinary things produce that: a gateway restart between create
        # and end, and #383's compensating end for a bootstrap abandoned by
        # supersession — which by definition raced the record. Refusing here
        # would turn "clean up after yourself" into an error the app must
        # then decide to ignore.
        self._voice_sessions.pop(voice_session_id, None)
        return {"ended": True, "voiceSessionId": voice_session_id or None}

    async def _pair(self, payload: dict) -> dict:
        if not self._is_api_key(payload.get("auth")):
            return {"error": "Pairing requires the gateway API key", "code": "pair_requires_api_key"}
        install_id = _text(payload.get("install_id"))
        if not install_id:
            return {"error": "install_id is required", "code": "missing_install_id"}
        device_id, token = await asyncio.to_thread(
            self._store.create_paired_device,
            install_id, _text(payload.get("device_name")),
        )
        return {"device_id": device_id, "device_token": token}

    async def _touch(self, device_id: str) -> None:
        self._hub.touch(device_id)
        now = time.monotonic()
        # -inf, not 0.0: time.monotonic() is seconds-since-an-arbitrary-
        # epoch (often boot), so a first drain at t=30s with 0.0 as the
        # "never touched" sentinel would see 30 - 0 = 30 < throttle and
        # skip the very first store touch — last_seen would stay None
        # until t >= throttle. -inf guarantees the first touch always writes.
        last = self._last_store_touch.get(device_id, float("-inf"))
        if now - last >= self._touch_throttle:
            self._last_store_touch[device_id] = now
            await asyncio.to_thread(self._store.touch_device, device_id)

    async def _drain(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        device_id = device["id"]
        await self._touch(device_id)
        items = await asyncio.to_thread(self._outbox.pending, device_id)
        queries = self._hub.take_queries(device_id)
        if not items and not queries and payload.get("wait"):
            await self._hub.park(device_id, timeout=self._hold)
            await self._touch(device_id)
            items = await asyncio.to_thread(self._outbox.pending, device_id)
            queries = self._hub.take_queries(device_id)
        return {"items": items, "queries": queries}

    async def _ack(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        raw_ids = payload.get("item_ids")
        item_ids = [i for i in raw_ids if isinstance(i, str)] if isinstance(raw_ids, list) else []
        acked = await asyncio.to_thread(
            self._outbox.mark_delivered, item_ids, device_id=device["id"]
        )
        return {"acked": acked}

    async def _query_result(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        query_id = payload.get("query_id")
        # #260(B): the app may name WHICH gate refused; forward the (string)
        # fields so the tool's prose can relay them. Anything non-string is
        # dropped here, and an old app that sends neither key changes nothing.
        error_detail = {
            key: payload.get(key)
            for key in ("denied_gate", "denied_stream")
            if isinstance(payload.get(key), str)
        }
        resolved = self._hub.resolve_query(
            query_id if isinstance(query_id, str) else "",
            result=payload.get("result"),
            error=payload.get("error"),
            device_id=device["id"],
            error_detail=error_detail or None,
        )
        return {"ok": bool(resolved)}

    async def _unpair(self, payload: dict) -> dict:
        device = await self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        await asyncio.to_thread(self._store.deactivate, device["id"])
        return {"ok": True}
