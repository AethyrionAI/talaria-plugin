import json
import sqlite3

import pytest

from .. import database, outbox, store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def _write_legacy(tmp_path):
    devices = {
        "devices": [
            {
                "id": "phone-1",
                "token_sha256": "a" * 64,
                "install_id": "install-phone",
                "name": "Owen's iPhone",
                "created": "2026-08-01T01:02:03+00:00",
                "active": True,
                "last_seen": "2026-08-02T01:02:03+00:00",
            },
            {
                "id": "old-ipad",
                "token_sha256": "b" * 64,
                "install_id": "install-ipad",
                "name": "iPad",
                "created": "2026-07-01T01:02:03+00:00",
                "active": False,
                "last_seen": None,
                "deactivated": "2026-08-03T01:02:03+00:00",
            },
        ]
    }
    items = {
        "items": [
            {
                "id": "pending-1",
                "kind": "message",
                "text": "pending legacy",
                "created_at": "2026-08-04T01:02:03+00:00",
                "meta": {"source": "legacy"},
                "delivered_at": None,
                "active": True,
            },
            {
                "id": "delivered-1",
                "kind": "message",
                "text": "delivered legacy",
                "created_at": "2026-08-03T01:02:03+00:00",
                "meta": {"source": "legacy"},
                "delivered_at": "2026-08-03T02:02:03+00:00",
                "active": True,
            },
        ]
    }
    (tmp_path / "devices.json").write_text(json.dumps(devices), encoding="utf-8")
    (tmp_path / "outbox.json").write_text(json.dumps(items), encoding="utf-8")
    return devices, items


def test_first_use_migrates_devices_and_outbox_in_one_transaction(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    legacy_devices, legacy_items = _write_legacy(tmp_path)

    assert {device["id"]: device for device in store.devices()} == {
        device["id"]: device for device in legacy_devices["devices"]
    }
    assert [item["id"] for item in outbox.pending("phone-1")] == ["pending-1"]

    connection = sqlite3.connect(tmp_path / "talaria.db")
    try:
        delivered = connection.execute(
            "SELECT delivered_at, delivery_scope FROM outbox_items WHERE id = 'delivered-1'"
        ).fetchone()
        marker = connection.execute(
            "SELECT value FROM schema_metadata WHERE key = 'legacy_json_migration'"
        ).fetchone()
    finally:
        connection.close()
    # 351-B: with exactly one active migrated device, legacy rows resolve
    # to a real target instead of the claimable legacy_any scope.
    assert delivered == ("2026-08-03T02:02:03+00:00", "target_device")
    assert marker == ("1",)
    assert (tmp_path / "devices.json").exists()
    assert (tmp_path / "outbox.json").exists()
    assert json.loads((tmp_path / "outbox.json").read_text()) == legacy_items


def test_migration_is_idempotent_when_database_exists_without_marker(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    legacy_devices, _ = _write_legacy(tmp_path)

    # A database that already holds one of the legacy rows but carries no
    # migration marker (e.g. an interrupted earlier import).
    connection = sqlite3.connect(tmp_path / "talaria.db")
    try:
        connection.execute(
            """
            CREATE TABLE devices (
                id TEXT PRIMARY KEY,
                token_sha256 TEXT NOT NULL,
                install_id TEXT,
                name TEXT,
                created TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                last_seen TEXT,
                deactivated TEXT
            )
            """
        )
        first = legacy_devices["devices"][0]
        connection.execute(
            """
            INSERT INTO devices (
                id, token_sha256, install_id, name, created, active, last_seen, deactivated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                first["id"], first["token_sha256"], first["install_id"], first["name"],
                first["created"], 1, first["last_seen"], None,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    assert len(store.devices()) == 2
    assert len(store.devices()) == 2
    assert [item["id"] for item in outbox.pending("phone-1")] == ["pending-1"]


def test_corrupt_legacy_input_quarantines_and_keeps_serving(monkeypatch, tmp_path, caplog):
    _redirect(monkeypatch, tmp_path)
    corrupt = "{ definitely not valid json"
    (tmp_path / "devices.json").write_text(corrupt, encoding="utf-8")

    with caplog.at_level("WARNING", logger="talaria"):
        assert store.devices() == []

    rejected = tmp_path / "devices.json.rejected"
    assert rejected.read_text(encoding="utf-8") == corrupt
    assert not (tmp_path / "devices.json").exists()
    assert any("quarantined" in record.message for record in caplog.records)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("token_sha256", "not-a-digest"),
        ("active", "false"),
        ("created", "not-a-time"),
        ("name", 7),
    ],
)
def test_semantically_invalid_device_file_quarantines_whole_file(
    monkeypatch, tmp_path, field, value
):
    _redirect(monkeypatch, tmp_path)
    devices, _ = _write_legacy(tmp_path)
    devices["devices"][0][field] = value
    (tmp_path / "devices.json").write_text(json.dumps(devices), encoding="utf-8")

    assert store.devices() == []          # device file quarantined...
    assert (tmp_path / "devices.json.rejected").exists()
    # ...while the valid outbox file still imported (per-file atomicity).
    assert [item["id"] for item in outbox.all_pending_for_diagnostics()] == ["pending-1"]


def test_invalid_outbox_file_does_not_block_device_import(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    legacy_devices, _ = _write_legacy(tmp_path)
    (tmp_path / "outbox.json").write_text('{"items": [{"id": 1}]}', encoding="utf-8")

    migrated = store.devices()
    assert {device["id"] for device in migrated} == {
        device["id"] for device in legacy_devices["devices"]
    }
    assert (tmp_path / "outbox.json.rejected").exists()
    assert outbox.all_pending_for_diagnostics() == []


def test_fresh_install_writes_no_marker_and_imports_late_json(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    assert store.devices() == []          # fresh install: no legacy files, DB created

    legacy_devices, _ = _write_legacy(tmp_path)
    # Simulate the next process: initialize() is cached per-process (#351-C).
    database._INITIALIZED.clear()
    assert {device["id"] for device in store.devices()} == {
        device["id"] for device in legacy_devices["devices"]
    }


def test_corrupt_database_is_quarantined_and_rebuilt_from_json(monkeypatch, tmp_path, caplog):
    _redirect(monkeypatch, tmp_path)
    legacy_devices, _ = _write_legacy(tmp_path)
    (tmp_path / "talaria.db").write_bytes(b"garbage that is not sqlite")

    with caplog.at_level("WARNING", logger="talaria"):
        migrated = store.devices()
    assert {device["id"] for device in migrated} == {
        device["id"] for device in legacy_devices["devices"]
    }
    assert list(tmp_path.glob("talaria.db.corrupt-*"))


def test_migrated_row_with_chat_id_targets_that_device_only(monkeypatch, tmp_path):
    """351-B RED->GREEN: the reproduced cross-device disclosure. The baseline
    adapter wrote meta.chat_id on every row it created; migration must honor
    it, so devB can neither drain nor ack devA's row."""
    _redirect(monkeypatch, tmp_path)
    devices = {"devices": [
        {"id": "dev-a", "token_sha256": "a" * 64, "install_id": "ia",
         "name": "phone", "created": "2026-08-01T01:02:03+00:00",
         "active": True, "last_seen": None},
        {"id": "dev-b", "token_sha256": "b" * 64, "install_id": "ib",
         "name": "ipad", "created": "2026-08-01T01:02:03+00:00",
         "active": True, "last_seen": None},
    ]}
    items = {"items": [{
        "id": "secret-for-a", "kind": "message", "text": "private answer",
        "created_at": "2026-08-04T01:02:03+00:00",
        "meta": {"chat_id": "dev-a"}, "delivered_at": None, "active": True,
    }]}
    (tmp_path / "devices.json").write_text(json.dumps(devices), encoding="utf-8")
    (tmp_path / "outbox.json").write_text(json.dumps(items), encoding="utf-8")

    assert outbox.pending("dev-b") == []
    assert [item["id"] for item in outbox.pending("dev-a")] == ["secret-for-a"]
    assert outbox.mark_delivered(["secret-for-a"], device_id="dev-b") == []
    assert outbox.mark_delivered(["secret-for-a"], device_id="dev-a") == ["secret-for-a"]


def test_migrated_row_without_chat_id_targets_the_single_active_device(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    _write_legacy(tmp_path)   # one active device (phone-1), one inactive
    assert len(store.devices()) == 2   # force the migration

    connection = sqlite3.connect(tmp_path / "talaria.db")
    try:
        row = connection.execute(
            "SELECT target_device_id, delivery_scope FROM outbox_items WHERE id = 'pending-1'"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("phone-1", "target_device")
