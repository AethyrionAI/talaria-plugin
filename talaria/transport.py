"""In-memory transport state for the Talaria platform adapter.

Ephemeral BY DESIGN (spec §1.2): parked drains, pending phone queries and
their futures live for the gateway process's lifetime only. A restart
drops parked queries and the tool answers "unreachable" — honest.

CROSS-LOOP CONTRACT (#263(b), 2026-08-07). This hub is touched from TWO
event loops in two threads and every hand-off between them must go through
``call_soon_threadsafe``. ``park()``/``take_queries()``/``resolve_query()``
run on the api_server's HTTP loop; ``enqueue_query()``/``wake()`` and the
awaiting future run on the loop ``model_tools._run_async`` creates for the
async tool (``tools/registry.py`` dispatches every ``is_async`` handler
there, and it is never the caller's loop). ``asyncio.Event.set()`` and
``Future.set_result()`` resolve their waiters with a plain
``loop.call_soon()``, which only validates the calling thread in DEBUG mode:
in production it queues the callback WITHOUT ``_write_to_self()``, so a loop
blocked in ``select()`` does not notice until its next timer. On a parked
drain that timer is the full hold — which is why every live query on
2026-08-06 completed at 25.00-25.01s, 8 for 8, while the same assertion on
one loop stayed green in the suite.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

logger = logging.getLogger("talaria")


def _schedule(loop: asyncio.AbstractEventLoop | None, fn, *args) -> bool:
    """Run ``fn(*args)`` on ``loop``, waking it even from another thread.

    ``call_soon_threadsafe`` is correct on the calling loop too, so this is
    unconditional — there is no "am I on the right loop" branch to get wrong.

    A closed or already-finished target loop is a NO-OP, not an error: the
    tool discards its query on every exit path (``tools.py``'s
    ``finally: hub.discard_query(...)``), so by the time a late answer
    arrives the waiter is gone and the caller has already given up. Raising
    here would turn that into a 500 on the phone's HTTP request.
    """
    if loop is None:
        return False
    try:
        loop.call_soon_threadsafe(fn, *args)
        return True
    except RuntimeError:
        # Loop closed or shutting down — the awaiting side is gone.
        return False


class TransportHub:
    def __init__(self, time_fn=time.monotonic):
        self._time = time_fn
        self._last_seen: dict[str, float] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._event_loops: dict[str, asyncio.AbstractEventLoop] = {}
        self._parked_counts: dict[str, int] = {}
        self._queries: dict[str, list[dict]] = {}
        self._futures: dict[str, tuple] = {}
        # #263-E counters — read by `hermes talaria status` so a transport
        # forensic is a CLI call instead of a log crawl.
        self.counters: dict[str, int] = {
            "queries_enqueued": 0,
            "queries_delivered": 0,
            "wakes_missed": 0,
            "full_cycle_deliveries": 0,
            "parks_woken": 0,
            "parks_timed_out": 0,
        }

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
        # Remember whose loop is waiting: wake() may arrive from the tool
        # loop and must schedule the set() over here. See CROSS-LOOP
        # CONTRACT at the top of this module.
        try:
            self._event_loops[device_id] = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover — park is always awaited
            pass
        self._parked_counts[device_id] = self._parked_counts.get(device_id, 0) + 1
        started = time.monotonic()
        outcome = "timed_out"
        try:
            # A wake() that landed before this park() started (the ordinary
            # long-poll case) must not be discarded — consume it and return
            # at once rather than clearing it away and waiting a full cycle.
            if event.is_set():
                event.clear()
                outcome = "pre_set"
                return
            try:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                pass
            else:
                event.clear()
                outcome = "woken"
        finally:
            elapsed = time.monotonic() - started
            if outcome == "woken":
                self.counters["parks_woken"] += 1
            elif outcome == "timed_out":
                self.counters["parks_timed_out"] += 1
                # A timed-out park that nonetheless has a query waiting means
                # the wake did not land in time — the #263(b) signature.
                if self._queries.get(device_id):
                    self.counters["wakes_missed"] += 1
            logger.debug(
                "park exit device=%s outcome=%s elapsed=%.3fs loop=%s parked=%d",
                device_id, outcome, elapsed,
                id(self._event_loops.get(device_id)), len(self._parked_counts),
            )
            remaining = self._parked_counts.get(device_id, 0) - 1
            if remaining <= 0:
                self._parked_counts.pop(device_id, None)
            else:
                self._parked_counts[device_id] = remaining

    def wake(self, device_id: str | None = None) -> None:
        # INVARIANT (#263(b)): a wake may cross event loops — always schedule
        # the set() onto the loop that is parked, never call it inline.
        if device_id is not None:
            self._wake_one(device_id)
            return
        for target in list(self._events):
            self._wake_one(target)

    def _wake_one(self, device_id: str) -> None:
        event = self._event(device_id)
        loop = self._event_loops.get(device_id)
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is None or loop is current:
            # Nobody has parked on a foreign loop yet (or we ARE that loop):
            # set it inline so a wake-before-park is still consumed by
            # park()'s pre-check above.
            event.set()
            return
        if not _schedule(loop, event.set):
            # Parked loop is gone; set it anyway so the next park on a live
            # loop consumes it rather than sleeping a full cycle.
            event.set()

    # -- phone queries --------------------------------------------------------
    def enqueue_query(self, device_id: str, kind: str, params: dict) -> tuple[str, asyncio.Future]:
        query_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
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
            {"id": query_id, "kind": kind, "params": safe_params,
             "_enqueued_at": self._time()}
        )
        # Owner AND owning loop travel with the future: resolve_query refuses
        # a different device's attempt to answer this query (fabricated data
        # injection) and schedules the result back onto the loop that is
        # awaiting it (#263(b) answer leg).
        self._futures[query_id] = (device_id, future, loop)
        self.counters["queries_enqueued"] += 1
        logger.debug(
            "query enqueued id=%s device=%s kind=%s loop=%s hub=%s",
            query_id, device_id, kind, id(loop), id(self),
        )
        self.wake(device_id)
        return query_id, future

    def take_queries(self, device_id: str) -> list[dict]:
        pending = self._queries.pop(device_id, [])
        now = self._time()
        out = []
        for query in pending:
            enqueued_at = query.pop("_enqueued_at", None)
            if enqueued_at is not None:
                waited = now - enqueued_at
                self.counters["queries_delivered"] += 1
                # "Delivery cost a full poll cycle" is THE #263(b) number
                # (2A-B's owed transport measurement). 20s is comfortably
                # past any healthy wake and short of the 25s hold.
                if waited >= 20.0:
                    self.counters["full_cycle_deliveries"] += 1
                logger.info(
                    "query delivered id=%s device=%s enqueue_to_drain=%.3fs",
                    query.get("id"), device_id, waited,
                )
            out.append(query)
        return out

    def resolve_query(self, query_id: str, result: dict | None = None, error: str | None = None,
                       device_id: str | None = None, error_detail: dict | None = None) -> bool:
        entry = self._futures.get(query_id)
        if entry is None:
            return False
        owner, future, loop = entry
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
        else:
            answer = result or {}
        return self._settle(future, loop, answer)

    def _settle(self, future: asyncio.Future, loop, answer: dict) -> bool:
        """Complete ``future`` on ITS loop (#263(b) answer leg).

        The future was created on the tool's loop (enqueue_query) and this
        runs on the HTTP loop, so a bare set_result() would queue the wakeup
        without waking the selector — the awaiting tool would not see the
        answer until its own 25s timeout fired.
        """
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is None or loop is current:
            if not future.done():
                future.set_result(answer)
            return True

        def _apply():
            if not future.done():
                future.set_result(answer)

        if _schedule(loop, _apply):
            return True
        # Awaiting loop is gone (the tool already timed out and discarded).
        # Report the honest outcome rather than claiming a delivery.
        return False

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

# #263-E module-load stamp. Two of these in one process IS the split hub
# (#263(a)) — printed, not inferred. Zero after a bounce means the plugin
# never loaded at all.
#
# `pid` is what makes "in one process" readable (#263 WATCH, the 2026-08-06
# 22:49 breadcrumb): a second stamp with different module/hub ids is benign
# when a second PROCESS wrote it and is the split shape when the SAME process
# re-executed the module. Without the pid those two are indistinguishable in
# the log, which is exactly the question that cost an evening.
logger.info(
    "transport module loaded pid=%s module=%s hub=%s",
    os.getpid(),
    id(__import__("sys").modules.get(__name__)),
    id(HUB),
)
