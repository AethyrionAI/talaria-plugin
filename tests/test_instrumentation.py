"""#263-E: the counters and log lines that make a transport forensic one grep.

Tonight's forensic (2026-08-06) cost an hour of log archaeology to answer four
questions: which hub the adapter attached, which hub the check_fn read, how
many module-load passes ran, and how long each query waited between enqueue
and drain. Each of those is now a line. These tests keep them honest — an
instrument that silently stops reporting is worse than none.
"""

import asyncio
import logging

from .. import admin, tools
from ..transport import TransportHub


def test_counters_start_at_zero_and_are_all_named():
    hub = TransportHub()
    assert set(hub.counters) == {
        "queries_enqueued", "queries_delivered", "wakes_missed",
        "full_cycle_deliveries", "parks_woken", "parks_timed_out",
    }
    assert not any(hub.counters.values())


async def test_enqueue_and_drain_move_the_counters():
    hub = TransportHub()
    hub.enqueue_query("dev1", "location", {})
    assert hub.counters["queries_enqueued"] == 1
    assert hub.counters["queries_delivered"] == 0
    hub.take_queries("dev1")
    assert hub.counters["queries_delivered"] == 1


async def test_delivery_latency_is_logged_and_the_stamp_never_reaches_the_phone(caplog):
    """The enqueue->drain delta is THE #263(b) number (2A-B's owed transport
    measurement) — but the bookkeeping key must not ride out to the app,
    whose decoder is strict about a query's shape."""
    clock = [100.0]
    hub = TransportHub(time_fn=lambda: clock[0])
    hub.enqueue_query("dev1", "calendar", {"window_days": "3"})
    clock[0] += 24.5  # a full poll cycle: the wake-miss signature
    with caplog.at_level(logging.INFO, logger="talaria"):
        [query] = hub.take_queries("dev1")

    assert set(query) == {"id", "kind", "params"}, "internal stamp leaked to the phone"
    assert "enqueue_to_drain=24.500s" in caplog.text


async def test_a_full_cycle_delivery_is_counted_but_a_prompt_one_is_not():
    clock = [100.0]
    hub = TransportHub(time_fn=lambda: clock[0])

    hub.enqueue_query("dev1", "location", {})
    clock[0] += 0.05                      # healthy wake
    hub.take_queries("dev1")
    assert hub.counters["full_cycle_deliveries"] == 0

    hub.enqueue_query("dev1", "location", {})
    clock[0] += 24.9                      # one full hold
    hub.take_queries("dev1")
    assert hub.counters["full_cycle_deliveries"] == 1


async def test_park_records_woken_versus_timed_out():
    hub = TransportHub()

    task = asyncio.create_task(hub.park("dev1", timeout=5.0))
    await asyncio.sleep(0.01)
    hub.wake("dev1")
    await asyncio.wait_for(task, timeout=1.0)
    assert hub.counters["parks_woken"] == 1
    assert hub.counters["parks_timed_out"] == 0

    await asyncio.wait_for(hub.park("dev2", timeout=0.05), timeout=1.0)
    assert hub.counters["parks_timed_out"] == 1


async def test_a_park_that_times_out_on_a_waiting_query_counts_a_missed_wake():
    """The #263(b) signature: the hold expired while a query sat undelivered.
    Zero here is the assertion that the fix holds in production."""
    hub = TransportHub()
    # Enqueue directly into the pending map without waking, to model a wake
    # that was sent but never landed.
    hub._queries.setdefault("dev1", []).append({"id": "q1", "kind": "location", "params": {}})
    await asyncio.wait_for(hub.park("dev1", timeout=0.05), timeout=1.0)
    assert hub.counters["wakes_missed"] == 1


async def test_check_fn_logs_the_hub_it_read_and_the_liveness_inputs(caplog, monkeypatch):
    """A hub id here that differs from the adapter-attach stamp is #263(a).
    The SAME id with live=False is an honestly dead transport — which is what
    2026-08-06 20:51 actually was. These two are indistinguishable without
    this line."""
    hub = TransportHub(time_fn=lambda: 100.0)
    monkeypatch.setattr(tools, "_hub", lambda: hub)
    hub.touch("dev1")
    with caplog.at_level(logging.DEBUG, logger="talaria"):
        assert tools._transport_available() is True
    assert f"check_fn hub={id(hub)}" in caplog.text
    assert "live=True" in caplog.text


async def test_module_load_stamp_names_the_hub_instance():
    """Two of these in one process IS the split hub, printed rather than
    inferred. Pinned by content so a refactor can't quietly drop it."""
    from .. import transport

    source = (transport.__file__ or "")
    assert source
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "transport module loaded" in text
    assert "id(HUB)" in text


def test_status_reports_the_hub_and_says_when_counters_are_process_local(capsys):
    admin._print_transport_counters()
    out = capsys.readouterr().out
    assert "Transport hub" in out
    assert "no transport activity in this process" in out


def test_status_surfaces_a_missed_wake_when_one_happened(capsys, monkeypatch):
    from .. import transport

    hub = TransportHub()
    hub.counters["wakes_missed"] = 2
    hub.counters["full_cycle_deliveries"] = 2
    monkeypatch.setattr(transport, "HUB", hub)
    admin._print_transport_counters()
    out = capsys.readouterr().out
    assert "wakes MISSED                  2" in out
    assert "#263(b)" in out
