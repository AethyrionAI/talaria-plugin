"""SQLite schema, connection, and legacy JSON migration for Talaria."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from hermes_constants import get_hermes_home, secure_parent_dir

logger = logging.getLogger("talaria")

_BUSY_TIMEOUT_MS = 30_000
_MIGRATION_KEY = "legacy_json_migration"

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()  # str(database_path) values initialized this process


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
    """
    CREATE INDEX IF NOT EXISTS devices_token_idx
    ON devices(token_sha256)
    """,
)


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


def _quarantine_file(path: Path, exc: Exception) -> None:
    """Preserve a bad legacy file's bytes under a new name and warn loudly.

    Fail-soft is #351-A's whole point: one malformed field must never brick
    auth and the CLI forever the way a raise out of migration did."""
    rejected = path.with_name(path.name + ".rejected")
    try:
        if not rejected.exists():
            path.rename(rejected)
    except OSError:
        pass
    logger.warning(
        "talaria: legacy %s failed migration and was quarantined to %s "
        "(bytes preserved; see README 'Migration recovery'): %s",
        path.name, rejected.name, exc,
    )


def _quarantine_database(path: Path, exc: Exception) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if candidate.exists():
                candidate.rename(candidate.with_name(f"{candidate.name}.corrupt-{stamp}"))
        except OSError:
            pass
    logger.warning(
        "talaria: unreadable database quarantined to %s.corrupt-%s and recreated: %s",
        path.name, stamp, exc,
    )


def _import_file(connection, path: Path, collection_key: str, migrate_fn) -> bool:
    """Import one legacy file atomically: a validation failure rolls back
    ONLY this file's rows, quarantines the file, and migration continues.
    Returns True when the file was present (imported or quarantined)."""
    if not path.exists():
        return False
    connection.execute("SAVEPOINT legacy_file")
    try:
        rows = _legacy_rows(path, collection_key)
        migrate_fn(connection, path, rows)
        connection.execute("RELEASE SAVEPOINT legacy_file")
    except MigrationError as exc:
        connection.execute("ROLLBACK TO SAVEPOINT legacy_file")
        connection.execute("RELEASE SAVEPOINT legacy_file")
        _quarantine_file(path, exc)
    return True


def _migrate_legacy_json(connection: sqlite3.Connection, db_path: Path) -> None:
    marker = connection.execute(
        "SELECT value FROM schema_metadata WHERE key = ?", (_MIGRATION_KEY,)
    ).fetchone()
    if marker is not None:
        return

    devices_path = db_path.with_name("devices.json")
    outbox_path = db_path.with_name("outbox.json")
    saw_devices = _import_file(connection, devices_path, "devices", _migrate_devices)
    saw_outbox = _import_file(connection, outbox_path, "items", _migrate_outbox)
    if not saw_devices and not saw_outbox:
        # #351-C: nothing to migrate — write NO marker, so legacy JSON
        # appearing later (old-gateway overlap, restore-from-backup) still
        # imports at the next initialize().
        return

    connection.execute(
        "INSERT INTO schema_metadata(key, value) VALUES (?, '1')",
        (_MIGRATION_KEY,),
    )
    connection.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES ('schema_version', '1')"
    )


def database_path() -> Path:
    """The plugin's single durable-state location; tests monkeypatch THIS."""
    return Path(get_hermes_home()) / "talaria" / "talaria.db"


def _open(path: Path) -> sqlite3.Connection:
    """Open with hygiene: parent secured, file 0600 from creation, every
    PRAGMA inside the close-on-failure guard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    secure_parent_dir(path)
    # Pre-create at 0600 so token hashes are never observable at a wider
    # mode — sqlite itself would create the file at umask default.
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    os.close(fd)
    connection = sqlite3.connect(
        path,
        timeout=_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    except Exception:
        connection.close()
        raise
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        try:
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
        except OSError:
            pass
    return connection


def try_connect_readonly(path: Path | None = None) -> sqlite3.Connection | None:
    """Open read-only WITHOUT creating or migrating; None when absent or
    unreadable. The liveness-prose probe rides this (351-I)."""
    resolved = path if path is not None else database_path()
    if not resolved.exists():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{resolved}?mode=ro",
            uri=True,
            timeout=_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
    except sqlite3.Error:
        return None
    connection.row_factory = sqlite3.Row
    return connection


def initialize(path: Path | None = None) -> None:
    """One-shot per process: schema creation + legacy import.

    Fail-soft on bad legacy input (quarantine + warning, never a raise);
    an unreadable database file is itself quarantined and recreated, with
    the untouched legacy JSON re-imported into the fresh database. Raises
    only when a database cannot be created at all."""
    resolved = path if path is not None else database_path()
    key = str(resolved)
    with _INIT_LOCK:
        if key in _INITIALIZED:
            return
        try:
            connection = _open(resolved)
        except sqlite3.Error as exc:
            _quarantine_database(resolved, exc)
            connection = _open(resolved)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)
            _migrate_legacy_json(connection, resolved)
            connection.commit()
        except Exception:
            connection.rollback()
            connection.close()
            raise
        connection.close()
        _INITIALIZED.add(key)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the plugin database, initializing lazily on first use."""
    resolved = path if path is not None else database_path()
    if str(resolved) not in _INITIALIZED:
        initialize(resolved)
    return _open(resolved)
