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
