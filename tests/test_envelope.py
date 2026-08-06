import asyncio

import pytest

from .. import outbox, store
from ..envelope import EnvelopeService
from ..transport import TransportHub

API_KEY = "test-api-key-64chars-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_store_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(outbox, "_outbox_path", lambda: tmp_path / "outbox.json")
    hub = TransportHub()
    service = EnvelopeService(
        api_key_provider=lambda: API_KEY,
        hub=hub,
        store_mod=store,
        outbox_mod=outbox,
        hold_seconds=0.05,
        touch_throttle_seconds=0.0,
    )
    return service, hub


def test_verify_accepts_api_key_and_device_token(env):
    service, _ = env
    assert service.verify(f"Bearer {API_KEY}") == (True, "")
    _, token = store.create_paired_device("i-1", "phone")
    assert service.verify(f"Bearer {token}") == (True, "")
    assert service.verify("Bearer wrong")[0] is False
    assert service.verify("")[0] is False


async def test_pair_requires_api_key_and_mints(env):
    service, _ = env
    refused = await service.dispatch({"type": "pair", "auth": "junk", "install_id": "i-1", "device_name": "p"})
    assert refused["code"] == "pair_requires_api_key"
    ok = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    assert ok["device_id"] and ok["device_token"]


async def test_drain_returns_backlog_immediately(env):
    service, _ = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    outbox.append("waiting for you")
    result = await service.dispatch({
        "type": "drain", "auth": paired["device_token"],
        "device_id": paired["device_id"], "wait": True,
    })
    assert [i["text"] for i in result["items"]] == ["waiting for you"]
    assert result["queries"] == []


async def test_drain_wrong_token_rejected(env):
    service, _ = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    other = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-2", "device_name": "q"})
    crossed = await service.dispatch({
        "type": "drain", "auth": other["device_token"],
        "device_id": paired["device_id"], "wait": False,
    })
    assert crossed["code"] == "device_auth_mismatch"


async def test_drain_longpoll_wakes_on_send(env):
    service, hub = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    service_hold = EnvelopeService(
        api_key_provider=lambda: API_KEY, hub=hub, store_mod=store,
        outbox_mod=outbox, hold_seconds=5.0, touch_throttle_seconds=0.0,
    )
    drain = asyncio.create_task(service_hold.dispatch({
        "type": "drain", "auth": paired["device_token"],
        "device_id": paired["device_id"], "wait": True,
    }))
    await asyncio.sleep(0.02)
    outbox.append("fresh")
    hub.wake(paired["device_id"])
    result = await asyncio.wait_for(drain, timeout=1.0)
    assert [i["text"] for i in result["items"]] == ["fresh"]


async def test_ack_marks_delivered(env):
    service, _ = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    item = outbox.append("one")
    result = await service.dispatch({
        "type": "ack", "auth": paired["device_token"],
        "device_id": paired["device_id"], "item_ids": [item["id"]],
    })
    assert result == {"acked": [item["id"]]}
    assert outbox.pending() == []


async def test_query_flows_through_drain_and_result(env):
    service, hub = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    qid, future = hub.enqueue_query(paired["device_id"], "location", {})
    drained = await service.dispatch({
        "type": "drain", "auth": paired["device_token"],
        "device_id": paired["device_id"], "wait": False,
    })
    assert drained["queries"] == [{"id": qid, "kind": "location", "params": {}}]
    resolved = await service.dispatch({
        "type": "query_result", "auth": paired["device_token"],
        "device_id": paired["device_id"], "query_id": qid,
        "result": {"text": "at home"},
    })
    assert resolved == {"ok": True}
    assert (await asyncio.wait_for(future, 0.5)) == {"text": "at home"}


async def test_unpair_deactivates(env):
    service, _ = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    result = await service.dispatch({
        "type": "unpair", "auth": paired["device_token"], "device_id": paired["device_id"],
    })
    assert result == {"ok": True}
    assert store.active_devices() == []


async def test_unknown_type_is_clean_error(env):
    service, _ = env
    result = await service.dispatch({"type": "surprise", "auth": API_KEY})
    assert result["code"] == "unknown_event_type"


# -- malformed-payload hardening (found in self-review: a JSON body is
# attacker-controlled, so field types are not guaranteed) ------------------

async def test_dispatch_non_dict_payload_is_clean_error(env):
    service, _ = env
    for bogus in (None, "just a string", [1, 2, 3], 42):
        result = await service.dispatch(bogus)
        assert result["code"] == "malformed_payload"


async def test_dispatch_unhashable_type_field_is_clean_error(env):
    service, _ = env
    result = await service.dispatch({"type": ["pair"], "auth": API_KEY})
    assert result["code"] == "unknown_event_type"


async def test_pair_non_string_auth_is_clean_error(env):
    service, _ = env
    result = await service.dispatch({"type": "pair", "auth": 123, "install_id": "i-1"})
    assert result["code"] == "pair_requires_api_key"
    result = await service.dispatch({"type": "pair", "auth": ["x"], "install_id": "i-1"})
    assert result["code"] == "pair_requires_api_key"


async def test_pair_non_string_install_id_and_device_name_is_clean_error(env):
    service, _ = env
    result = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": 5})
    assert result["code"] == "missing_install_id"
    result = await service.dispatch({
        "type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": ["a"],
    })
    assert result["device_id"] and result["device_token"]


async def test_device_ops_non_string_auth_is_clean_error(env):
    service, _ = env
    for kind, extra in [
        ("drain", {"wait": False}),
        ("ack", {"item_ids": ["x"]}),
        ("query_result", {"query_id": "q1", "result": {}}),
        ("unpair", {}),
    ]:
        result = await service.dispatch({"type": kind, "auth": 123, "device_id": "d1", **extra})
        assert result["code"] == "device_auth_mismatch"


async def test_ack_non_list_and_mixed_type_item_ids_is_clean_error(env):
    service, _ = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    item = outbox.append("one")
    result = await service.dispatch({
        "type": "ack", "auth": paired["device_token"],
        "device_id": paired["device_id"], "item_ids": 5,
    })
    assert result == {"acked": []}
    result = await service.dispatch({
        "type": "ack", "auth": paired["device_token"],
        "device_id": paired["device_id"], "item_ids": [item["id"], {"nested": "dict"}, 7],
    })
    assert result == {"acked": [item["id"]]}


async def test_query_result_non_string_query_id_is_clean_error(env):
    service, hub = env
    paired = await service.dispatch({"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"})
    hub.enqueue_query(paired["device_id"], "location", {})
    result = await service.dispatch({
        "type": "query_result", "auth": paired["device_token"],
        "device_id": paired["device_id"], "query_id": [1, 2], "result": {},
    })
    assert result == {"ok": False}
