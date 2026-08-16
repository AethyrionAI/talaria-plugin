"""Transactional, device-scoped agent-to-phone outbox for Talaria."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import get_hermes_home

from .database import connect


class UnknownTargetError(ValueError):
    """Raised when a message target is absent or inactive."""


def _outbox_path() -> Path:
    """Legacy JSON path retained for first-use migration compatibility."""
    return Path(get_hermes_home()) / "talaria" / "outbox.json"


def _database_path() -> Path:
    return _outbox_path().with_name("talaria.db")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _item_from_row(row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "text": row["text"],
        "created_at": row["created_at"],
        "meta": json.loads(row["meta_json"]),
        "delivered_at": row["delivered_at"],
        "active": bool(row["active"]),
    }


def _safe_meta(meta: dict | None) -> dict[str, str]:
    # The app decodes item metadata as [String: String].
    return {str(key): str(value) for key, value in (meta or {}).items()}


def _new_item(text: str, meta: dict | None) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "kind": "message",
        "text": text,
        "created_at": _now_iso(),
        "meta": _safe_meta(meta),
        "delivered_at": None,
        "active": True,
    }


def _insert_targeted(connection, item: dict, target_device_id: str) -> None:
    active = connection.execute(
        "SELECT 1 FROM devices WHERE id = ? AND active = 1",
        (target_device_id,),
    ).fetchone()
    if active is None:
        raise UnknownTargetError(f"Talaria device '{target_device_id}' is unknown or inactive")
    connection.execute(
        """
        INSERT INTO outbox_items (
            id, kind, text, created_at, target_device_id, delivery_scope,
            claimed_by_device_id, delivered_at, active, meta_json
        ) VALUES (?, ?, ?, ?, ?, 'target_device', NULL, NULL, 1, ?)
        """,
        (
            item["id"], item["kind"], item["text"], item["created_at"],
            target_device_id,
            json.dumps(item["meta"], separators=(",", ":"), sort_keys=True),
        ),
    )


def append(
    text: str,
    meta: dict | None = None,
    *,
    target_device_id: str | None = None,
) -> dict:
    """Atomically append one item to an explicit or unambiguous active target.

    Omitting ``target_device_id`` is safe only when exactly one active device
    exists. New writes never use the migration-only ``legacy_any`` scope.
    """
    item = _new_item(text, meta)
    connection = connect(_database_path())
    try:
        connection.execute("BEGIN IMMEDIATE")
        if target_device_id is None:
            active_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM devices WHERE active = 1 ORDER BY created, rowid"
                ).fetchall()
            ]
            if not active_ids:
                raise UnknownTargetError(
                    "Talaria send has no active device; pair one or choose an explicit target"
                )
            if len(active_ids) > 1:
                raise UnknownTargetError(
                    "Talaria send has multiple active devices; choose an explicit target"
                )
            target_device_id = active_ids[0]
        assert target_device_id is not None
        _insert_targeted(connection, item, target_device_id)
        connection.commit()
        return item
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def append_for_devices(text: str, device_ids: list[str], meta: dict | None = None) -> list[dict]:
    """Atomically fan out one independently acknowledged row per device."""
    unique_ids = list(dict.fromkeys(device_ids))
    if not unique_ids:
        return []
    items = [_new_item(text, meta) for _ in unique_ids]
    connection = connect(_database_path())
    try:
        connection.execute("BEGIN IMMEDIATE")
        for item, device_id in zip(items, unique_ids):
            _insert_targeted(connection, item, device_id)
        connection.commit()
        return items
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def all_pending_for_diagnostics() -> list[dict]:
    """Inspect all pending rows without claiming them; never use for delivery."""
    connection = connect(_database_path())
    try:
        rows = connection.execute(
            """
            SELECT * FROM outbox_items
            WHERE active = 1 AND delivered_at IS NULL
            ORDER BY created_at, rowid
            """
        ).fetchall()
        return [_item_from_row(row) for row in rows]
    finally:
        connection.close()


def pending(device_id: str) -> list[dict]:
    """Return pending items entitled to one authenticated active device."""
    connection = connect(_database_path())
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM devices WHERE id = ? AND active = 1", (device_id,)
        ).fetchone() is None:
            connection.commit()
            return []
        connection.execute(
            """
            UPDATE outbox_items
            SET claimed_by_device_id = ?
            WHERE active = 1 AND delivered_at IS NULL
              AND delivery_scope = 'legacy_any'
              AND claimed_by_device_id IS NULL
            """,
            (device_id,),
        )
        rows = connection.execute(
            """
            SELECT * FROM outbox_items
            WHERE active = 1 AND delivered_at IS NULL
              AND (
                  (delivery_scope = 'target_device' AND target_device_id = ?)
                  OR
                  (delivery_scope = 'legacy_any' AND claimed_by_device_id = ?)
              )
            ORDER BY created_at, rowid
            """,
            (device_id, device_id),
        ).fetchall()
        connection.commit()
        return [_item_from_row(row) for row in rows]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_delivered(item_ids: list[str], *, device_id: str) -> list[str]:
    """Acknowledge only rows routed or atomically claimed to ``device_id``."""
    wanted = list(dict.fromkeys(item_ids or []))
    if not wanted or not device_id:
        return []
    connection = connect(_database_path())
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM devices WHERE id = ? AND active = 1", (device_id,)
        ).fetchone() is None:
            connection.commit()
            return []
        acked: list[str] = []
        delivered_at = _now_iso()
        for item_id in wanted:
            cursor = connection.execute(
                """
                UPDATE outbox_items
                SET delivered_at = ?
                WHERE id = ? AND active = 1 AND delivered_at IS NULL
                  AND (
                      (delivery_scope = 'target_device' AND target_device_id = ?)
                      OR
                      (delivery_scope = 'legacy_any' AND claimed_by_device_id = ?)
                  )
                """,
                (delivered_at, item_id, device_id, device_id),
            )
            if cursor.rowcount:
                acked.append(item_id)
        connection.commit()
        return acked
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
