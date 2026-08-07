import asyncio

import pytest

from .. import tools
from ..transport import TransportHub


@pytest.fixture
def hub(monkeypatch):
    hub = TransportHub(time_fn=lambda: 100.0)
    monkeypatch.setattr(tools, "_hub", lambda: hub)
    return hub


def test_schema_params_declares_string_additional_properties():
    # Well-behaved models should send strings in the first place; the
    # transport-layer coercion (transport.enqueue_query) is the backstop
    # for the ones that don't (#251 finding 1).
    params_schema = tools._SCHEMAS["talaria_phone_query"]["function"]["parameters"]["properties"]["params"]
    assert params_schema["additionalProperties"] == {"type": "string"}


def test_check_fn_false_when_no_device(hub):
    assert tools._transport_available() is False


def test_check_fn_true_when_recent_drain(hub):
    hub.touch("dev1")
    assert tools._transport_available() is True


async def test_phone_query_unreachable_when_dead(hub):
    text = await tools.phone_query({"kind": "location"})
    assert "unreachable" in text.lower()
    assert "retry" in text.lower()


async def test_phone_query_round_trip(hub, monkeypatch):
    monkeypatch.setattr(tools, "_QUERY_TIMEOUT", 1.0)
    hub.touch("dev1")

    async def answer_soon():
        await asyncio.sleep(0.02)
        [q] = hub.take_queries("dev1")
        # Task-4 fix round bound queries to their owner device.
        hub.resolve_query(q["id"], result={"text": "Currently at: Home"}, device_id="dev1")

    answering = asyncio.create_task(answer_soon())
    text = await tools.phone_query({"kind": "location", "params": {}})
    await answering
    assert text == "Currently at: Home"


async def test_phone_query_timeout_is_honest(hub, monkeypatch):
    monkeypatch.setattr(tools, "_QUERY_TIMEOUT", 0.05)
    hub.touch("dev1")
    text = await tools.phone_query({"kind": "health", "params": {"metric": "steps"}})
    assert "did not answer" in text.lower()


async def test_phone_query_timeout_discards_the_query_from_the_hub(hub, monkeypatch):
    # A timed-out query must not linger forever as a phantom future a late
    # phone answer could still resolve, or a queued item a drain would keep
    # handing out (I2, coordinator fix round).
    monkeypatch.setattr(tools, "_QUERY_TIMEOUT", 0.05)
    hub.touch("dev1")
    await tools.phone_query({"kind": "health", "params": {"metric": "steps"}})
    assert hub._futures == {}
    assert hub._queries.get("dev1", []) == []


async def test_phone_query_error_result_reported_plainly(hub, monkeypatch):
    monkeypatch.setattr(tools, "_QUERY_TIMEOUT", 1.0)
    hub.touch("dev1")

    async def deny_soon():
        await asyncio.sleep(0.02)
        [q] = hub.take_queries("dev1")
        # Task-4 fix round bound queries to their owner device.
        hub.resolve_query(q["id"], error="permission_denied", device_id="dev1")

    denying = asyncio.create_task(deny_soon())
    text = await tools.phone_query({"kind": "health"})
    await denying
    assert "permission" in text.lower()


# -- #260(B): the declined prose names the actual blocker -------------------

async def _denied_query(hub, monkeypatch, kind, **detail):
    monkeypatch.setattr(tools, "_QUERY_TIMEOUT", 1.0)
    hub.touch("dev1")

    async def deny_soon():
        await asyncio.sleep(0.02)
        [q] = hub.take_queries("dev1")
        hub.resolve_query(q["id"], error="permission_denied",
                          error_detail=detail or None, device_id="dev1")

    denying = asyncio.create_task(deny_soon())
    text = await tools.phone_query({"kind": kind})
    await denying
    return text


async def test_master_denial_names_the_master_switch(hub, monkeypatch):
    text = await _denied_query(hub, monkeypatch, "health", denied_gate="master")
    assert text == (
        'The phone declined: the master "Share Sensors with Hermes" switch is '
        "off in Talaria's privacy settings. That one switch gates ALL sensor "
        "sharing — streams and queries alike — so flipping an individual "
        "sensor toggle will not unblock this."
    )


async def test_stream_denial_names_the_actual_toggle(hub, monkeypatch):
    # kind=weather but the blocking toggle is LOCATION — the prose must name
    # the toggle a user can actually flip, exactly the #260(B) defect.
    text = await _denied_query(hub, monkeypatch, "weather",
                               denied_gate="stream", denied_stream="location")
    assert text == (
        "The phone declined: the Location sensor toggle is off in Talaria's "
        "privacy settings. The master sensor switch is on, so enabling "
        "Location is what unblocks this."
    )


async def test_bare_denial_keeps_the_generic_prose(hub, monkeypatch):
    # Pre-#260 apps send no gate fields — the prose must stay byte-identical
    # to what shipped, so old app + new plugin degrades to today's behavior.
    text = await _denied_query(hub, monkeypatch, "health")
    assert text == (
        "The phone declined: permission for that data stream is disabled in "
        "Talaria's privacy settings."
    )


async def test_unknown_gate_value_falls_back_to_generic_prose(hub, monkeypatch):
    text = await _denied_query(hub, monkeypatch, "health", denied_gate="future_gate")
    assert text == (
        "The phone declined: permission for that data stream is disabled in "
        "Talaria's privacy settings."
    )


async def test_stream_denial_without_stream_name_falls_back_to_generic(hub, monkeypatch):
    text = await _denied_query(hub, monkeypatch, "health", denied_gate="stream")
    assert text == (
        "The phone declined: permission for that data stream is disabled in "
        "Talaria's privacy settings."
    )


# -- #263-C: the tool must outlast one drain hold --------------------------

def test_query_timeout_exceeds_the_drain_hold_by_a_margin():
    """263-C. _QUERY_TIMEOUT must be STRICTLY greater than the drain hold.

    They were equal (25.0 == 25.0), so a query enqueued just after a park
    started could only be answered at the exact instant the tool gave up —
    every live query on 2026-08-06 completed at 25.00-25.01s and the answer
    won or lost by milliseconds. With a margin, a wake regression degrades to
    a slow answer instead of a user-visible failure.
    """
    import inspect

    from ..envelope import EnvelopeService

    hold = inspect.signature(EnvelopeService.__init__).parameters["hold_seconds"].default
    assert tools._QUERY_TIMEOUT >= hold + 10.0, (
        f"_QUERY_TIMEOUT={tools._QUERY_TIMEOUT} leaves no margin over a "
        f"{hold}s drain hold — a delivery that costs one full cycle races "
        "the tool's own timeout (#263-C)"
    )
