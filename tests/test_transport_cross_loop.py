"""#263(b): the hub is touched from TWO event loops, and it must survive that.

Production topology (Talaria-27 OPEN_ITEMS #263, scoped 2026-08-07):
``park()`` runs on the api_server's HTTP event loop, while ``enqueue_query()``
/ ``wake()`` / the awaiting tool run on a DIFFERENT loop in a DIFFERENT
thread — every async plugin tool is bridged there by
``tools/registry.py:773-775`` -> ``model_tools._run_async``, which never runs
the coroutine on the caller's loop.

``asyncio.Event.set()`` and ``Future.set_result()`` resolve their waiters with
a plain ``loop.call_soon()``, which only checks the calling thread in DEBUG
mode. In production it appends to the target loop's ``_ready`` queue WITHOUT
``_write_to_self()``, so a loop blocked in ``select()`` never notices until
its next scheduled timer — which, on a parked drain, is the full hold. That is
why every live query completed at 25.00-25.01s (8/8 on 2026-08-06) while
``tests/test_transport.py::test_park_returns_early_on_wake`` — the same
assertion on ONE loop — stayed green.

These tests are the two-loop arms. The one-loop test stays where it is,
unmodified, as the control.
"""

import asyncio
import threading
import time

from talaria.transport import TransportHub

# Deliberately generous: the fix should land in milliseconds. Anything near
# HOLD means the wake did not release the park.
HOLD = 5.0
FAST = 1.0


def _run_on_own_loop(coro_factory):
    """Run ``coro_factory()`` to completion on a fresh loop in a new thread.

    Mirrors model_tools._run_async's worker-loop branch, which is how the
    gateway actually executes this plugin's async tool.
    """
    box = {}

    def target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            box["result"] = loop.run_until_complete(coro_factory())
        except BaseException as exc:  # surfaced to the caller below
            box["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box


# -- 263-A: the delivery leg ------------------------------------------------

def test_cross_loop_wake_releases_a_parked_drain():
    """263-A. A wake() from a second loop must release the park promptly.

    RED before the fix: park returns at ~HOLD (the timer, not the wake).
    """
    hub = TransportHub()
    parked = {}

    async def park_side():
        started = time.monotonic()
        await hub.park("dev1", timeout=HOLD)
        parked["elapsed"] = time.monotonic() - started
        # The hub stays coherent either way — proving the defect is the WAKE,
        # not a lost query (that distinction is what separates #263(b) from
        # #263(a); see the 2026-08-07 scoping note).
        parked["queries"] = hub.take_queries("dev1")

    thread, box = _run_on_own_loop(park_side)

    async def tool_side():
        await asyncio.sleep(0.05)  # let the park settle into event.wait()
        hub.enqueue_query("dev1", "location", {})

    asyncio.run(tool_side())
    thread.join(timeout=HOLD + 5.0)
    assert not thread.is_alive(), "park side never finished"
    assert "error" not in box, box.get("error")

    assert len(parked["queries"]) == 1, "the query must survive the hand-off"
    assert parked["elapsed"] < FAST, (
        f"park slept {parked['elapsed']:.3f}s of a {HOLD}s hold — the "
        "cross-loop wake did not release it (#263(b) delivery leg)"
    )


# -- 263-B: the answer leg --------------------------------------------------

def test_cross_loop_resolve_completes_the_waiting_future():
    """263-B. resolve_query() from a second loop must land promptly.

    The future is created on the tool loop (transport.py:78) and resolved
    from the HTTP loop (transport.py:128/:130). RED before the fix: the
    awaiting side sees the answer only when its own timer fires, i.e. at
    ~HOLD — which in production is exactly the 25s _QUERY_TIMEOUT boundary.
    """
    hub = TransportHub()
    enqueued = threading.Event()
    handle = {}
    observed = {}

    async def tool_side():
        query_id, future = hub.enqueue_query("dev1", "location", {})
        handle["id"] = query_id
        enqueued.set()
        started = time.monotonic()
        try:
            observed["answer"] = await asyncio.wait_for(future, timeout=HOLD)
        except asyncio.TimeoutError:
            observed["answer"] = None
        observed["elapsed"] = time.monotonic() - started

    async def http_side():
        # Wait off-loop for the query to exist, then answer it.
        await asyncio.get_running_loop().run_in_executor(None, enqueued.wait)
        await asyncio.sleep(0.05)
        hub.resolve_query(handle["id"], result={"text": "here"}, device_id="dev1")
        # Keep this loop alive the way a real HTTP loop would be.
        await asyncio.sleep(0.2)

    thread, box = _run_on_own_loop(http_side)
    asyncio.run(tool_side())
    thread.join(timeout=HOLD + 5.0)
    assert "error" not in box, box.get("error")

    assert observed["answer"] == {"text": "here"}
    assert observed["elapsed"] < FAST, (
        f"the answer took {observed['elapsed']:.3f}s of a {HOLD}s wait — the "
        "cross-loop resolve did not wake the awaiting loop (#263(b) answer leg)"
    )


def test_cross_loop_resolve_of_an_error_answer_also_lands():
    """263-B, denial leg. The refusal path rides the same hand-off."""
    hub = TransportHub()
    enqueued = threading.Event()
    handle = {}
    observed = {}

    async def tool_side():
        query_id, future = hub.enqueue_query("dev1", "health", {})
        handle["id"] = query_id
        enqueued.set()
        started = time.monotonic()
        try:
            observed["answer"] = await asyncio.wait_for(future, timeout=HOLD)
        except asyncio.TimeoutError:
            observed["answer"] = None
        observed["elapsed"] = time.monotonic() - started

    async def http_side():
        await asyncio.get_running_loop().run_in_executor(None, enqueued.wait)
        await asyncio.sleep(0.05)
        hub.resolve_query(
            handle["id"], error="permission_denied",
            error_detail={"denied_gate": "master"}, device_id="dev1",
        )
        await asyncio.sleep(0.2)

    thread, box = _run_on_own_loop(http_side)
    asyncio.run(tool_side())
    thread.join(timeout=HOLD + 5.0)
    assert "error" not in box, box.get("error")

    assert observed["answer"] == {"error": "permission_denied", "denied_gate": "master"}
    assert observed["elapsed"] < FAST, (
        f"the denial took {observed['elapsed']:.3f}s of a {HOLD}s wait "
        "(#263(b) answer leg, denial)"
    )


def test_cross_loop_wake_all_devices_releases_a_parked_drain():
    """263-A, broadcast leg. platform_adapter.send() calls HUB.wake() with no
    device id (an outbox message for whoever is listening) — that path crosses
    loops too."""
    hub = TransportHub()
    parked = {}

    async def park_side():
        started = time.monotonic()
        await hub.park("dev1", timeout=HOLD)
        parked["elapsed"] = time.monotonic() - started

    thread, box = _run_on_own_loop(park_side)

    async def sender_side():
        await asyncio.sleep(0.05)
        hub.wake()

    asyncio.run(sender_side())
    thread.join(timeout=HOLD + 5.0)
    assert not thread.is_alive(), "park side never finished"
    assert "error" not in box, box.get("error")
    assert parked["elapsed"] < FAST, (
        f"broadcast wake left the park asleep {parked['elapsed']:.3f}s "
        f"of a {HOLD}s hold (#263(b), wake-all)"
    )


def test_a_dead_target_loop_does_not_raise_into_the_caller():
    """The tool loop can be gone by the time the phone answers — the tool
    discards on every exit path (tools.py:76-85), so a late resolve must be a
    quiet no-op, never an exception into the HTTP handler."""
    hub = TransportHub()
    handle = {}

    async def tool_side():
        query_id, _future = hub.enqueue_query("dev1", "location", {})
        handle["id"] = query_id

    # This loop is created, used, and CLOSED — exactly the stranded case.
    asyncio.run(tool_side())

    # No exception, and no false claim of success.
    hub.resolve_query(handle["id"], result={"text": "late"}, device_id="dev1")
