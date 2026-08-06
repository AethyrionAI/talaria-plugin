"""In-memory transport state for the Talaria platform adapter.

Ephemeral BY DESIGN (spec §1.2): parked drains, pending phone queries and
their futures live for the gateway process's lifetime only. A restart
drops parked queries and the tool answers "unreachable" — honest.
"""

from __future__ import annotations

import asyncio
import time
import uuid


class TransportHub:
    def __init__(self, time_fn=time.monotonic):
        self._time = time_fn
        self._last_seen: dict[str, float] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._parked: set[str] = set()
        self._queries: dict[str, list[dict]] = {}
        self._futures: dict[str, asyncio.Future] = {}

    # -- liveness ---------------------------------------------------------
    def touch(self, device_id: str) -> None:
        self._last_seen[device_id] = self._time()

    def is_live(self, window_seconds: float = 60.0) -> bool:
        if self._parked:
            return True
        now = self._time()
        return any(now - seen <= window_seconds for seen in self._last_seen.values())

    def freshest_device(self) -> str | None:
        if not self._last_seen:
            return None
        return max(self._last_seen, key=self._last_seen.get)

    # -- long-poll parking --------------------------------------------------
    def _event(self, device_id: str) -> asyncio.Event:
        if device_id not in self._events:
            self._events[device_id] = asyncio.Event()
        return self._events[device_id]

    async def park(self, device_id: str, timeout: float = 25.0) -> None:
        event = self._event(device_id)
        event.clear()
        self._parked.add(device_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            self._parked.discard(device_id)

    def wake(self, device_id: str | None = None) -> None:
        if device_id is not None:
            self._event(device_id).set()
            return
        for event in self._events.values():
            event.set()

    # -- phone queries --------------------------------------------------------
    def enqueue_query(self, device_id: str, kind: str, params: dict) -> tuple[str, asyncio.Future]:
        query_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._queries.setdefault(device_id, []).append(
            {"id": query_id, "kind": kind, "params": params or {}}
        )
        self._futures[query_id] = future
        self.wake(device_id)
        return query_id, future

    def take_queries(self, device_id: str) -> list[dict]:
        return self._queries.pop(device_id, [])

    def resolve_query(self, query_id: str, result: dict | None = None, error: str | None = None) -> bool:
        future = self._futures.pop(query_id, None)
        if future is None or future.done():
            return False
        future.set_result({"error": error} if error else (result or {}))
        return True


HUB = TransportHub()
