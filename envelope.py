"""Envelope core for the Talaria platform adapter (spec §1.1).

Pure logic, dependency-injected for tests; platform_adapter.py wraps it. The
route verifies the HEADER (authentication — bad creds 401 before
dispatch); dispatch authorizes from the payload's `auth` field (spec
Addendum): pair requires the API key, device ops require the device's
own token bound to the claimed device_id. Every failure is a clean
error dict — the route 500s on raised exceptions, so nothing raises.

Payload fields arrive as parsed JSON from an untrusted HTTP body, so
their Python types are not guaranteed to match the documented shape
(a client can send `"auth": 123` or `"item_ids": "not-a-list"`).
Every payload.get() that feeds a type-sensitive call — hmac.compare_
digest, str.strip, dict hashing/pop, set() — is guarded so a
malformed type degrades to a clean error instead of an unhandled
exception; see `_text()` and the isinstance checks below.
"""

from __future__ import annotations

import hmac
import time


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

    # -- route-level authentication ---------------------------------------
    def verify(self, auth_header: str) -> tuple[bool, str]:
        token = _bearer(auth_header)
        if not token:
            return False, "missing_bearer"
        key = self._api_key() or ""
        if key and _ct_equal(token, key):
            return True, ""
        if self._store.device_for_token(token) is not None:
            return True, ""
        return False, "invalid_talaria_auth"

    # -- per-type authorization helpers ------------------------------------
    def _is_api_key(self, value) -> bool:
        if not isinstance(value, str):
            return False
        key = self._api_key() or ""
        return bool(key) and _ct_equal(value, key)

    def _device_authorized(self, payload: dict) -> dict | None:
        auth = payload.get("auth")
        if not isinstance(auth, str):
            return None
        device = self._store.device_for_token(auth)
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
        }.get(event_type) if isinstance(event_type, str) else None
        if handler is None:
            return {"error": "Unknown event type", "code": "unknown_event_type"}
        return await handler(payload)

    async def _pair(self, payload: dict) -> dict:
        if not self._is_api_key(payload.get("auth")):
            return {"error": "Pairing requires the gateway API key", "code": "pair_requires_api_key"}
        install_id = _text(payload.get("install_id"))
        if not install_id:
            return {"error": "install_id is required", "code": "missing_install_id"}
        device_id, token = self._store.create_paired_device(
            install_id, _text(payload.get("device_name"))
        )
        return {"device_id": device_id, "device_token": token}

    def _touch(self, device_id: str) -> None:
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
            self._store.touch_device(device_id)

    async def _drain(self, payload: dict) -> dict:
        device = self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        device_id = device["id"]
        self._touch(device_id)
        items = self._outbox.pending()
        queries = self._hub.take_queries(device_id)
        if not items and not queries and payload.get("wait"):
            await self._hub.park(device_id, timeout=self._hold)
            self._touch(device_id)
            items = self._outbox.pending()
            queries = self._hub.take_queries(device_id)
        return {"items": items, "queries": queries}

    async def _ack(self, payload: dict) -> dict:
        if self._device_authorized(payload) is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        raw_ids = payload.get("item_ids")
        item_ids = [i for i in raw_ids if isinstance(i, str)] if isinstance(raw_ids, list) else []
        return {"acked": self._outbox.mark_delivered(item_ids)}

    async def _query_result(self, payload: dict) -> dict:
        device = self._device_authorized(payload)
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
        device = self._device_authorized(payload)
        if device is None:
            return {"error": "Token does not authorize this device", "code": "device_auth_mismatch"}
        self._store.deactivate(device["id"])
        return {"ok": True}
