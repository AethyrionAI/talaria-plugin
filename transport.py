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
        self._parked_counts: dict[str, int] = {}
        self._queries: dict[str, list[dict]] = {}
        self._futures: dict[str, asyncio.Future] = {}

    # -- liveness ---------------------------------------------------------
    def touch(self, device_id: str) -> None:
        self._last_seen[device_id] = self._time()

    def is_live(self, window_seconds: float = 60.0) -> bool:
        if self._parked_counts:
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
        self._parked_counts[device_id] = self._parked_counts.get(device_id, 0) + 1
        try:
            # A wake() that landed before this park() started (the ordinary
            # long-poll case) must not be discarded — consume it and return
            # at once rather than clearing it away and waiting a full cycle.
            if event.is_set():
                event.clear()
                return
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            else:
                event.clear()
        finally:
            remaining = self._parked_counts.get(device_id, 0) - 1
            if remaining <= 0:
                self._parked_counts.pop(device_id, None)
            else:
                self._parked_counts[device_id] = remaining

    def wake(self, device_id: str | None = None) -> None:
        if device_id is not None:
            self._event(device_id).set()
            return
        for event in self._events.values():
            event.set()

    # -- phone queries --------------------------------------------------------
    def enqueue_query(self, device_id: str, kind: str, params: dict) -> tuple[str, asyncio.Future]:
        query_id = uuid.uuid4().hex[:12]
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # The app decodes a drained query's params strictly as
        # [String: String] — one non-string value (a model authoring
        # {"window_days": 3} instead of {"window_days": "3"}) fails the
        # WHOLE drain decode, not just this query, so the query dies to
        # the tool's 25s timeout even with the phone live and every other
        # item in that drain batch waits a cycle. This is the single choke
        # point every query passes through regardless of caller, so
        # coercion belongs here rather than at each call site (tools.py
        # already sends strings from the schema, but a future caller
        # forgetting to would silently reintroduce the same failure).
        # Keys are already strings from JSON; str()'d anyway in case a
        # caller ever hands us a non-JSON-sourced dict.
        safe_params = {str(k): str(v) for k, v in (params or {}).items()}
        self._queries.setdefault(device_id, []).append(
            {"id": query_id, "kind": kind, "params": safe_params}
        )
        # Owner travels with the future so resolve_query can refuse a
        # different device's attempt to answer this query (fabricated
        # data injection) — see resolve_query's device_id check.
        self._futures[query_id] = (device_id, future)
        self.wake(device_id)
        return query_id, future

    def take_queries(self, device_id: str) -> list[dict]:
        return self._queries.pop(device_id, [])

    def resolve_query(self, query_id: str, result: dict | None = None, error: str | None = None,
                       device_id: str | None = None, error_detail: dict | None = None) -> bool:
        entry = self._futures.get(query_id)
        if entry is None:
            return False
        owner, future = entry
        if device_id != owner:
            # Wrong device claiming to answer someone else's query: refuse
            # WITHOUT popping or resolving, so the rightful owner can still
            # resolve it afterward.
            return False
        self._futures.pop(query_id, None)
        if future.done():
            return False
        if error is not None:
            answer = {"error": error}
            # #260(B): denial-gate metadata rides the error answer so the
            # tool's prose can name the actual blocker. String-only and never
            # the "error" key itself — the detail explains the error, it does
            # not get to rewrite it.
            for key, value in (error_detail or {}).items():
                if key != "error" and isinstance(key, str) and isinstance(value, str):
                    answer[key] = value
            future.set_result(answer)
        else:
            future.set_result(result or {})
        return True

    def discard_query(self, query_id: str) -> None:
        """Drop a query that will never be answered (e.g. the tool gave up).

        Removes the ``_futures`` entry and any not-yet-drained pending
        payload from ``_queries`` so a timed-out query does not sit forever
        as a phantom entry a phone might still answer into, or a queued
        item a `drain` would keep handing out. Safe no-op if the query was
        already resolved (``resolve_query`` already popped ``_futures``)
        or already taken by a drain (already popped from ``_queries``).
        """
        self._futures.pop(query_id, None)
        for device_id, pending in list(self._queries.items()):
            remaining = [q for q in pending if q.get("id") != query_id]
            if len(remaining) != len(pending):
                if remaining:
                    self._queries[device_id] = remaining
                else:
                    self._queries.pop(device_id, None)


HUB = TransportHub()
