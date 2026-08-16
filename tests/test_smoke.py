from .. import outbox, store
from ..envelope import EnvelopeService
from ..platform_adapter import TalariaPlatformAdapter
from ..transport import TransportHub

API_KEY = "smoke-api-key-64chars-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


async def test_temporary_home_pair_target_drain_ack_redrain_unpair(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_store_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(outbox, "_outbox_path", lambda: tmp_path / "outbox.json")
    service = EnvelopeService(
        api_key_provider=lambda: API_KEY,
        hub=TransportHub(),
        store_mod=store,
        outbox_mod=outbox,
        hold_seconds=0.01,
        touch_throttle_seconds=0.0,
    )
    phone = await service.dispatch({
        "type": "pair",
        "auth": API_KEY,
        "install_id": "smoke-phone",
        "device_name": "phone",
    })
    ipad = await service.dispatch({
        "type": "pair",
        "auth": API_KEY,
        "install_id": "smoke-ipad",
        "device_name": "ipad",
    })
    adapter = object.__new__(TalariaPlatformAdapter)

    sent = await adapter.send(phone["device_id"], "smoke message")
    assert sent.success is True

    wrong_drain = await service.dispatch({
        "type": "drain",
        "auth": ipad["device_token"],
        "device_id": ipad["device_id"],
        "wait": False,
    })
    assert wrong_drain["items"] == []

    target_drain = await service.dispatch({
        "type": "drain",
        "auth": phone["device_token"],
        "device_id": phone["device_id"],
        "wait": False,
    })
    assert [item["id"] for item in target_drain["items"]] == [sent.message_id]

    acked = await service.dispatch({
        "type": "ack",
        "auth": phone["device_token"],
        "device_id": phone["device_id"],
        "item_ids": [sent.message_id],
    })
    assert acked == {"acked": [sent.message_id]}

    empty_redrain = await service.dispatch({
        "type": "drain",
        "auth": phone["device_token"],
        "device_id": phone["device_id"],
        "wait": False,
    })
    assert empty_redrain["items"] == []

    unpaired = await service.dispatch({
        "type": "unpair",
        "auth": phone["device_token"],
        "device_id": phone["device_id"],
    })
    assert unpaired == {"ok": True}
    assert store.active_device(phone["device_id"]) is None
