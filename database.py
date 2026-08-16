"""SQLite schema, connection, and legacy JSON migration for Talaria."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import cast

_BUSY_TIMEOUT_MS = 30_000
_LOCK_RETRIES = 100
_MIGRATION_KEY = "legacy_json_migration"


class MigrationError(RuntimeError):
    """Raised when durable legacy state cannot be migrated safely."""


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS schema_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS devices (
        id TEXT PRIMARY KEY,
        token_sha256 TEXT NOT NULL,
        install_id TEXT,
        name TEXT,
        created TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        last_seen TEXT,
        deactivated TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS devices_active_idx
    ON devices(active, install_id, created, id)
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox_items (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        target_device_id TEXT REFERENCES devices(id),
        delivery_scope TEXT NOT NULL DEFAULT 'legacy_any'
            CHECK (delivery_scope IN ('target_device', 'legacy_any')),
        claimed_by_device_id TEXT REFERENCES devices(id),
        delivered_at TEXT,
        active INTEGER NOT NULL DEFAULT 1,
        meta_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS outbox_pending_idx
    ON outbox_items(active, delivered_at, created_at, id)
    """,
)


def _retry_locked(operation):
    for attempt in range(_LOCK_RETRIES):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == _LOCK_RETRIES - 1:
                raise
            time.sleep(min(0.005 * (attempt + 1), 0.05))


def _legacy_rows(path: Path, collection_key: str) -> list[dict]:
    if not path.exists():
        return []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MigrationError(f"Cannot migrate {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get(collection_key), list):
        raise MigrationError(f"Cannot migrate {path}: expected a '{collection_key}' list")
    rows = document[collection_key]
    if not all(isinstance(row, dict) for row in rows):
        raise MigrationError(f"Cannot migrate {path}: every {collection_key} entry must be an object")
    ids = [row.get("id") for row in rows]
    if any(not isinstance(row_id, str) or not row_id for row_id in ids):
        raise MigrationError(f"Cannot migrate {path}: every entry needs a non-empty string id")
    if len(ids) != len(set(ids)):
        raise MigrationError(f"Cannot migrate {path}: duplicate ids would lose durable state")
    return rows


def _migration_error(path: Path, record_type: str, record_id: str, field: str, detail: str) -> MigrationError:
    return MigrationError(
        f"Cannot migrate {path}: {record_type} {record_id} field '{field}' {detail}"
    )


def _require_string(
    path: Path,
    record_type: str,
    record_id: str,
    field: str,
    value,
    *,
    allow_none: bool = False,
) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise _migration_error(path, record_type, record_id, field, "must be a string")
    return value


def _require_timestamp(
    path: Path,
    record_type: str,
    record_id: str,
    field: str,
    value,
    *,
    allow_none: bool = False,
) -> str | None:
    text = _require_string(
        path, record_type, record_id, field, value, allow_none=allow_none
    )
    if text is None:
        return None
    try:
        datetime.fromisoformat(text)
    except ValueError as exc:
        raise _migration_error(
            path, record_type, record_id, field, "must be an ISO-8601 timestamp"
        ) from exc
    return text


def _require_boolean(path: Path, record_type: str, record_id: str, field: str, value) -> bool:
    if type(value) is not bool:
        raise _migration_error(path, record_type, record_id, field, "must be a boolean")
    return value


def _migrate_devices(connection: sqlite3.Connection, path: Path, rows: list[dict]) -> None:
    required = ("id", "token_sha256", "created", "active")
    for legacy in rows:
        if any(key not in legacy for key in required):
            raise MigrationError(f"Cannot migrate {path}: device {legacy.get('id')} lacks required fields")
        record_id = legacy["id"]
        token_sha256 = cast(str, _require_string(
            path, "device", record_id, "token_sha256", legacy["token_sha256"]
        ))
        if re.fullmatch(r"[0-9a-f]{64}", token_sha256) is None:
            raise _migration_error(
                path, "device", record_id, "token_sha256",
                "must be a lowercase 64-character SHA-256 digest",
            )
        install_id = _require_string(
            path, "device", record_id, "install_id", legacy.get("install_id"),
            allow_none=True,
        )
        name = _require_string(
            path, "device", record_id, "name", legacy.get("name"), allow_none=True
        )
        created = _require_timestamp(
            path, "device", record_id, "created", legacy["created"]
        )
        active = _require_boolean(
            path, "device", record_id, "active", legacy["active"]
        )
        last_seen = _require_timestamp(
            path, "device", record_id, "last_seen", legacy.get("last_seen"),
            allow_none=True,
        )
        deactivated = _require_timestamp(
            path, "device", record_id, "deactivated", legacy.get("deactivated"),
            allow_none=True,
        )
        values = (
            record_id, token_sha256, install_id, name, created, int(active),
            last_seen, deactivated,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO devices (
                id, token_sha256, install_id, name, created, active, last_seen, deactivated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        stored = connection.execute(
            """
            SELECT id, token_sha256, install_id, name, created, active, last_seen, deactivated
            FROM devices WHERE id = ?
            """,
            (legacy["id"],),
        ).fetchone()
        if stored is None or tuple(stored) != values:
            raise MigrationError(f"Cannot migrate {path}: device {legacy['id']} conflicts with existing state")


def _migrate_outbox(connection: sqlite3.Connection, path: Path, rows: list[dict]) -> None:
    required = ("id", "kind", "text", "created_at", "meta", "active")
    for legacy in rows:
        if any(key not in legacy for key in required):
            raise MigrationError(f"Cannot migrate {path}: item {legacy.get('id')} lacks required fields")
        record_id = legacy["id"]
        kind = cast(str, _require_string(
            path, "item", record_id, "kind", legacy["kind"]
        ))
        if not kind:
            raise _migration_error(path, "item", record_id, "kind", "must not be empty")
        text = cast(str, _require_string(
            path, "item", record_id, "text", legacy["text"]
        ))
        created_at = _require_timestamp(
            path, "item", record_id, "created_at", legacy["created_at"]
        )
        delivered_at = _require_timestamp(
            path, "item", record_id, "delivered_at", legacy.get("delivered_at"),
            allow_none=True,
        )
        active = _require_boolean(
            path, "item", record_id, "active", legacy["active"]
        )
        meta = legacy["meta"]
        if not isinstance(meta, dict):
            raise _migration_error(path, "item", record_id, "meta", "must be an object")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in meta.items()):
            raise _migration_error(
                path, "item", record_id, "meta", "keys and values must be strings"
            )
        meta_json = json.dumps(meta, separators=(",", ":"), sort_keys=True)
        values = (
            record_id, kind, text, created_at, delivered_at, int(active), meta_json,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO outbox_items (
                id, kind, text, created_at, target_device_id, delivery_scope,
                claimed_by_device_id, delivered_at, active, meta_json
            ) VALUES (?, ?, ?, ?, NULL, 'legacy_any', NULL, ?, ?, ?)
            """,
            values,
        )
        stored = connection.execute(
            """
            SELECT id, kind, text, created_at, delivered_at, active, meta_json,
                   target_device_id, delivery_scope, claimed_by_device_id
            FROM outbox_items WHERE id = ?
            """,
            (legacy["id"],),
        ).fetchone()
        expected = (*values, None, "legacy_any", None)
        if stored is None or tuple(stored) != expected:
            raise MigrationError(f"Cannot migrate {path}: item {legacy['id']} conflicts with existing state")


def _migrate_legacy_json(connection: sqlite3.Connection, database_path: Path) -> None:
    marker = connection.execute(
        "SELECT value FROM schema_metadata WHERE key = ?", (_MIGRATION_KEY,)
    ).fetchone()
    if marker is not None:
        return

    devices_path = database_path.with_name("devices.json")
    outbox_path = database_path.with_name("outbox.json")
    devices = _legacy_rows(devices_path, "devices")
    items = _legacy_rows(outbox_path, "items")
    _migrate_devices(connection, devices_path, devices)
    _migrate_outbox(connection, outbox_path, items)

    if connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0] < len(devices):
        raise MigrationError(f"Cannot migrate {devices_path}: imported count validation failed")
    if connection.execute("SELECT COUNT(*) FROM outbox_items").fetchone()[0] < len(items):
        raise MigrationError(f"Cannot migrate {outbox_path}: imported count validation failed")

    connection.execute(
        "INSERT INTO schema_metadata(key, value) VALUES (?, '1')",
        (_MIGRATION_KEY,),
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES ('schema_version', '1')"
    )


def connect(database_path: Path, *, migrate_legacy: bool = True) -> sqlite3.Connection:
    """Open the plugin database and atomically initialize/migrate it."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(database_path.parent, 0o700)
    except OSError:
        pass
    connection = sqlite3.connect(
        database_path,
        timeout=_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    if str(journal_mode).lower() != "wal":
        _retry_locked(lambda: connection.execute("PRAGMA journal_mode = WAL").fetchone())
    connection.execute("PRAGMA synchronous = FULL")

    try:
        schema_exists = connection.execute(
            """
            SELECT COUNT(*) FROM sqlite_master
            WHERE type = 'table' AND name IN ('schema_metadata', 'devices', 'outbox_items')
            """
        ).fetchone()[0] == 3
        migration_complete = False
        if schema_exists and migrate_legacy:
            migration_complete = connection.execute(
                "SELECT 1 FROM schema_metadata WHERE key = ?", (_MIGRATION_KEY,)
            ).fetchone() is not None

        if not schema_exists or (migrate_legacy and not migration_complete):
            _retry_locked(lambda: connection.execute("BEGIN IMMEDIATE"))
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            if migrate_legacy:
                _migrate_legacy_json(connection, database_path)
            connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        raise

    try:
        os.chmod(database_path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{database_path}{suffix}")
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
    except OSError:
        pass
    return connection
