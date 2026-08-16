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
import sqlite3

from .. import database, outbox, platform_adapter, store
from ..platform_adapter import TalariaPlatformAdapter


def test_send_signature_matches_base_platform_adapter_contract():
    sig = inspect.signature(TalariaPlatformAdapter.send)
    assert list(sig.parameters) == ["self", "chat_id", "content", "reply_to", "metadata"]


async def test_send_targets_exact_active_device(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
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
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
    adapter = object.__new__(TalariaPlatformAdapter)
    inactive_id, _ = store.create_paired_device("old-install", "old phone")
    store.deactivate(inactive_id)

    for target in ("missing-device", inactive_id):
        result = await adapter.send(target, "must not queue")
        assert result.success is False
        assert "unknown or inactive" in result.error

    assert outbox.all_pending_for_diagnostics() == []


async def test_send_addressed_by_install_id_survives_repair(monkeypatch, tmp_path):
    """351-F RED->GREEN: install_id is the rotation-proof address. A send
    addressed by install_id lands on the CURRENT device after a re-pair."""
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
    monkeypatch.setattr(platform_adapter.HUB, "wake", lambda device_id=None: None)
    adapter = object.__new__(TalariaPlatformAdapter)
    store.create_paired_device("stable-install", "phone")
    new_id, _ = store.create_paired_device("stable-install", "phone")  # rotation

    result = await adapter.send("stable-install", "hello after re-pair")
    assert result.success is True
    assert [row["id"] for row in outbox.pending(new_id)] == [result.message_id]


async def test_send_never_raises_on_storage_failure(monkeypatch, tmp_path):
    """351-G: core call sites are written against the SendResult contract —
    a storage failure must come back as a failed result, not a raise."""
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
    adapter = object.__new__(TalariaPlatformAdapter)

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(outbox, "append", boom)
    result = await adapter.send("any-device", "content")
    assert result.success is False
    assert "database is locked" in result.error
