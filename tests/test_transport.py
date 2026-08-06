import asyncio

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
    assert hub.resolve_query(qid, result={"text": "here"}) is True
    assert (await asyncio.wait_for(future, 0.5)) == {"text": "here"}
    assert hub.resolve_query(qid, result={}) is False  # already resolved


@pytest.mark.asyncio
async def test_resolve_with_error_resolves_future_with_error_dict():
    hub = TransportHub()
    qid, future = hub.enqueue_query("dev1", "health", {"metric": "steps"})
    hub.resolve_query(qid, error="permission_denied")
    assert (await asyncio.wait_for(future, 0.5)) == {"error": "permission_denied"}
