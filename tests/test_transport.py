import asyncio
import time

import pytest

from ..transport import TransportHub


def test_is_live_tracks_touch_within_window():
    now = [100.0]
    hub = TransportHub(time_fn=lambda: now[0])
    assert hub.is_live(60) is False
    hub.touch("dev1")
    assert hub.is_live(60) is True
    now[0] += 61
    assert hub.is_live(60) is False


def test_freshest_device_prefers_latest_touch():
    now = [100.0]
    hub = TransportHub(time_fn=lambda: now[0])
    hub.touch("old")
    now[0] += 5
    hub.touch("new")
    assert hub.freshest_device() == "new"


@pytest.mark.asyncio
async def test_park_returns_early_on_wake():
    hub = TransportHub()
    task = asyncio.create_task(hub.park("dev1", timeout=5.0))
    await asyncio.sleep(0.01)
    hub.wake("dev1")
    await asyncio.wait_for(task, timeout=0.5)  # returns well before 5s


@pytest.mark.asyncio
async def test_park_expires_on_timeout():
    hub = TransportHub()
    await asyncio.wait_for(hub.park("dev1", timeout=0.05), timeout=0.5)


@pytest.mark.asyncio
async def test_parked_device_counts_as_live():
    hub = TransportHub(time_fn=lambda: 100.0)
    task = asyncio.create_task(hub.park("dev1", timeout=0.2))
    await asyncio.sleep(0.01)
    assert hub.is_live(60) is True
    await task


@pytest.mark.asyncio
async def test_query_cycle_enqueue_take_resolve():
    hub = TransportHub()
    qid, future = hub.enqueue_query("dev1", "location", {})
    taken = hub.take_queries("dev1")
    assert taken == [{"id": qid, "kind": "location", "params": {}}]
    assert hub.take_queries("dev1") == []  # take drains
    assert hub.resolve_query(qid, result={"text": "here"}, device_id="dev1") is True
    assert (await asyncio.wait_for(future, 0.5)) == {"text": "here"}
    assert hub.resolve_query(qid, result={}, device_id="dev1") is False  # already resolved


@pytest.mark.asyncio
async def test_resolve_with_error_resolves_future_with_error_dict():
    hub = TransportHub()
    qid, future = hub.enqueue_query("dev1", "health", {"metric": "steps"})
    hub.resolve_query(qid, error="permission_denied", device_id="dev1")
    assert (await asyncio.wait_for(future, 0.5)) == {"error": "permission_denied"}


@pytest.mark.asyncio
async def test_wake_before_park_returns_immediately():
    # Regression: a wake() that arrived before park() started (the ordinary
    # long-poll case — a query lands between two polls) must not be
    # discarded by park()'s unconditional event.clear().
    hub = TransportHub()
    hub.enqueue_query("dev1", "location", {})  # enqueue wakes "dev1"
    start = time.monotonic()
    await asyncio.wait_for(hub.park("dev1", timeout=5.0), timeout=1.0)
    assert time.monotonic() - start < 1.0


@pytest.mark.asyncio
async def test_overlapping_parks_keep_liveness_until_last_exits():
    # Regression: two overlapping park() calls for one device must not
    # share a single set-membership entry — the shorter one finishing
    # first must not erase liveness for the longer one still parked.
    hub = TransportHub()
    task_a = asyncio.create_task(hub.park("dev1", timeout=2.0))
    task_b = asyncio.create_task(hub.park("dev1", timeout=0.05))
    await asyncio.wait_for(task_b, timeout=0.5)
    assert hub.is_live(60) is True  # A is still parked
    hub.wake("dev1")
    await asyncio.wait_for(task_a, timeout=0.5)
    assert hub.is_live(60) is False  # both parks have now exited


@pytest.mark.asyncio
async def test_resolve_with_empty_string_error_still_resolves_as_error():
    hub = TransportHub()
    qid, future = hub.enqueue_query("dev1", "health", {})
    hub.resolve_query(qid, error="", device_id="dev1")
    assert (await asyncio.wait_for(future, 0.5)) == {"error": ""}


@pytest.mark.asyncio
async def test_enqueue_query_stringifies_non_string_param_values():
    # The app decodes a drained query's params strictly as [String: String];
    # one non-string value (a model authoring {"window_days": 3} instead of
    # {"window_days": "3"}) must never poison the whole drain decode (#251
    # finding 1).
    hub = TransportHub()
    hub.enqueue_query("dev1", "calendar", {"window_days": 3, "flag": True})
    [taken] = hub.take_queries("dev1")
    assert taken["params"] == {"window_days": "3", "flag": "True"}
    assert all(isinstance(v, str) for v in taken["params"].values())


@pytest.mark.asyncio
async def test_resolve_query_refuses_wrong_device_then_owner_still_resolves():
    # Regression: a valid token for device B must not be able to answer
    # (or discard) a query that was enqueued to device A — that would be
    # fabricated data injection into A's pending tool call.
    hub = TransportHub()
    qid, future = hub.enqueue_query("dev-a", "location", {})
    assert hub.resolve_query(qid, result={"text": "spoofed"}, device_id="dev-b") is False
    assert future.done() is False
    assert hub.resolve_query(qid, result={"text": "real"}, device_id="dev-a") is True
    assert (await asyncio.wait_for(future, 0.5)) == {"text": "real"}


# -- #260(B): denial-gate detail rides the error answer ---------------------

async def test_error_detail_fields_ride_the_error_answer():
    hub = TransportHub(time_fn=lambda: 100.0)
    qid, future = hub.enqueue_query("dev1", "health", {})
    assert hub.resolve_query(
        qid, error="permission_denied",
        error_detail={"denied_gate": "stream", "denied_stream": "health"},
        device_id="dev1",
    )
    assert (await future) == {
        "error": "permission_denied",
        "denied_gate": "stream",
        "denied_stream": "health",
    }


async def test_error_detail_drops_non_strings_and_cannot_clobber_error():
    hub = TransportHub(time_fn=lambda: 100.0)
    qid, future = hub.enqueue_query("dev1", "health", {})
    assert hub.resolve_query(
        qid, error="permission_denied",
        error_detail={"denied_gate": 7, "denied_stream": None, "error": "spoofed"},
        device_id="dev1",
    )
    assert (await future) == {"error": "permission_denied"}


async def test_error_detail_is_ignored_on_result_answers():
    hub = TransportHub(time_fn=lambda: 100.0)
    qid, future = hub.enqueue_query("dev1", "health", {})
    assert hub.resolve_query(
        qid, result={"text": "Steps today: 42"},
        error_detail={"denied_gate": "master"},
        device_id="dev1",
    )
    assert (await future) == {"text": "Steps today: 42"}
