"""Adapter shell checks that don't need instantiation.

platform_adapter.py imports gateway modules (only importable under the
hermes venv, which is also this suite's runner — see tests/test_tools.py
and the plugin README). Instantiating TalariaPlatformAdapter needs a real
PlatformConfig, so this stays a signature check rather than a behavior
test — the import smoke command covers "does it load", this covers "is
send() actually callable by the gateway's real callers" (I1, coordinator
fix round: the prior send(self, chat_id, text, **kwargs) shape does not
match BasePlatformAdapter's abstract contract, and in-tree callers pass
content= as a keyword).
"""

import inspect

from .. import outbox, platform_adapter, store
from ..platform_adapter import TalariaPlatformAdapter


def test_send_signature_matches_base_platform_adapter_contract():
    sig = inspect.signature(TalariaPlatformAdapter.send)
    assert list(sig.parameters) == ["self", "chat_id", "content", "reply_to", "metadata"]


async def test_send_targets_exact_active_device(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_store_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(outbox, "_outbox_path", lambda: tmp_path / "outbox.json")
    wake_calls = []
    monkeypatch.setattr(platform_adapter.HUB, "wake", lambda device_id=None: wake_calls.append(device_id))
    phone_id, _ = store.create_paired_device("phone-install", "phone")
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    adapter = object.__new__(TalariaPlatformAdapter)

    result = await adapter.send(phone_id, "phone only")

    assert result.success is True
    assert [row["id"] for row in outbox.pending(phone_id)] == [result.message_id]
    assert outbox.pending(ipad_id) == []
    assert wake_calls == [phone_id]


async def test_send_unknown_or_inactive_target_fails_without_queueing(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_store_path", lambda: tmp_path / "devices.json")
    monkeypatch.setattr(outbox, "_outbox_path", lambda: tmp_path / "outbox.json")
    adapter = object.__new__(TalariaPlatformAdapter)
    inactive_id, _ = store.create_paired_device("old-install", "old phone")
    store.deactivate(inactive_id)

    for target in ("missing-device", inactive_id):
        result = await adapter.send(target, "must not queue")
        assert result.success is False
        assert "unknown or inactive" in result.error

    assert outbox.all_pending_for_diagnostics() == []
