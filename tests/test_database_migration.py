import json
import sqlite3

import pytest

from .. import database, outbox, store
from ..database import MigrationError


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
    assert delivered == ("2026-08-03T02:02:03+00:00", "legacy_any")
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


def test_corrupt_legacy_input_fails_loudly_and_is_preserved(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    corrupt = "{ definitely not valid json"
    (tmp_path / "devices.json").write_text(corrupt, encoding="utf-8")

    with pytest.raises(MigrationError, match="devices.json"):
        store.devices()

    assert (tmp_path / "devices.json").read_text(encoding="utf-8") == corrupt
    assert not (tmp_path / "devices.json.corrupt").exists()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("token_sha256", "not-a-digest", "64-character SHA-256 digest"),
        ("active", "false", "must be a boolean"),
        ("created", "not-a-time", "ISO-8601 timestamp"),
        ("name", 7, "must be a string"),
    ],
)
def test_semantically_invalid_device_state_fails_before_marking_migration_complete(
    monkeypatch, tmp_path, field, value, message
):
    _redirect(monkeypatch, tmp_path)
    devices, _ = _write_legacy(tmp_path)
    devices["devices"][0][field] = value
    (tmp_path / "devices.json").write_text(json.dumps(devices), encoding="utf-8")

    with pytest.raises(MigrationError, match=message):
        store.devices()

    connection = sqlite3.connect(tmp_path / "talaria.db")
    try:
        has_metadata = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_metadata'"
        ).fetchone()
        marker = (
            connection.execute(
                "SELECT 1 FROM schema_metadata WHERE key = 'legacy_json_migration'"
            ).fetchone()
            if has_metadata else None
        )
    finally:
        connection.close()
    assert marker is None


def test_semantically_invalid_outbox_metadata_fails_loudly(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    _, items = _write_legacy(tmp_path)
    items["items"][0]["meta"] = {"attempt": 1}
    (tmp_path / "outbox.json").write_text(json.dumps(items), encoding="utf-8")

    with pytest.raises(MigrationError, match="keys and values must be strings"):
        outbox.pending("phone-1")



def test_failed_second_file_rolls_back_then_retries_without_duplication(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    legacy_devices, _ = _write_legacy(tmp_path)
    invalid_outbox = {
        "items": [
            {
                "id": "invalid-item",
                "kind": "message",
                "text": 7,
                "created_at": "2026-08-04T01:02:03+00:00",
                "meta": {},
                "delivered_at": None,
                "active": True,
            }
        ]
    }
    (tmp_path / "outbox.json").write_text(json.dumps(invalid_outbox), encoding="utf-8")

    with pytest.raises(MigrationError, match="field 'text' must be a string"):
        store.devices()

    assert json.loads((tmp_path / "outbox.json").read_text(encoding="utf-8")) == invalid_outbox
    connection = sqlite3.connect(tmp_path / "talaria.db")
    try:
        has_devices_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'devices'"
        ).fetchone()
        imported_count = (
            connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
            if has_devices_table else 0
        )
    finally:
        connection.close()
    assert imported_count == 0

    (tmp_path / "outbox.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    migrated = store.devices()
    assert {device["id"] for device in migrated} == {
        device["id"] for device in legacy_devices["devices"]
    }
    assert len(store.devices()) == len(legacy_devices["devices"])
