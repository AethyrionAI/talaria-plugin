"""Transactional device and pairing store for the Talaria plugin.

The plugin owns ``<HERMES_HOME>/talaria/talaria.db``. Pairing tokens are
stored only as SHA-256 hashes, and device records are deactivated rather than
deleted.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timezone

from .database import connect


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _device_from_row(row) -> dict:
    device = {
        "id": row["id"],
        "token_sha256": row["token_sha256"],
        "created": row["created"],
        "active": bool(row["active"]),
        "last_seen": row["last_seen"],
        "name": row["name"],
    }
    if row["install_id"] is not None:
        device["install_id"] = row["install_id"]
    if row["deactivated"] is not None:
        device["deactivated"] = row["deactivated"]
    return device


def _new_credentials() -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    return uuid.uuid4().hex[:12], token, hashlib.sha256(token.encode("utf-8")).hexdigest()


# 351-D: a claim held by a device that is no longer active would strand the
# row forever (nothing else may drain or ack it) — release it whenever the
# device population changes.
_RELEASE_STALE_CLAIMS = """
    UPDATE outbox_items SET claimed_by_device_id = NULL
    WHERE delivery_scope = 'legacy_any' AND active = 1
      AND delivered_at IS NULL
      AND claimed_by_device_id IN (SELECT id FROM devices WHERE active = 0)
"""


def create_pairing() -> tuple[str, str]:
    """Create a manual pairing record and return its one-time plaintext token."""
    device_id, token, token_sha256 = _new_credentials()
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO devices (
                id, token_sha256, install_id, name, created, active, last_seen, deactivated
            ) VALUES (?, ?, NULL, NULL, ?, 1, NULL, NULL)
            """,
            (device_id, token_sha256, _now_iso()),
        )
        connection.commit()
        return device_id, token
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def devices() -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT * FROM devices ORDER BY created, id"
        ).fetchall()
        return [_device_from_row(row) for row in rows]
    finally:
        connection.close()


def active_devices() -> list[dict]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT * FROM devices WHERE active = 1 ORDER BY created, id"
        ).fetchall()
        return [_device_from_row(row) for row in rows]
    finally:
        connection.close()


def active_device(device_id: str) -> dict | None:
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM devices WHERE id = ? AND active = 1",
            (device_id,),
        ).fetchone()
        return _device_from_row(row) if row is not None else None
    finally:
        connection.close()


def deactivate(device_id: str | None = None) -> int:
    """Deactivate one device, or every active device when no id is supplied."""
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        if device_id is None:
            cursor = connection.execute(
                "UPDATE devices SET active = 0, deactivated = ? WHERE active = 1",
                (_now_iso(),),
            )
        else:
            cursor = connection.execute(
                """
                UPDATE devices SET active = 0, deactivated = ?
                WHERE id = ? AND active = 1
                """,
                (_now_iso(), device_id),
            )
        connection.execute(_RELEASE_STALE_CLAIMS)
        connection.commit()
        return cursor.rowcount
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_paired_device(install_id: str, name: str) -> tuple[str, str]:
    """Atomically rotate any active row for an install and pair a new device."""
    device_id, token, token_sha256 = _new_credentials()
    timestamp = _now_iso()
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE devices SET active = 0, deactivated = ?
            WHERE install_id = ? AND active = 1
            """,
            (timestamp, install_id),
        )
        connection.execute(
            """
            INSERT INTO devices (
                id, token_sha256, install_id, name, created, active, last_seen, deactivated
            ) VALUES (?, ?, ?, ?, ?, 1, NULL, NULL)
            """,
            (device_id, token_sha256, install_id, name or None, timestamp),
        )
        # 351-D: undelivered rows targeted at this install's rotated-away
        # device ids follow the install to its new identity...
        connection.execute(
            """
            UPDATE outbox_items SET target_device_id = ?
            WHERE delivery_scope = 'target_device' AND active = 1
              AND delivered_at IS NULL
              AND target_device_id IN (
                  SELECT id FROM devices WHERE install_id = ? AND active = 0
              )
            """,
            (device_id, install_id),
        )
        # ...and claims held by any inactive device are released.
        connection.execute(_RELEASE_STALE_CLAIMS)
        connection.commit()
        return device_id, token
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def device_for_token(token: str) -> dict | None:
    digest = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    connection = connect()
    try:
        row = connection.execute(
            "SELECT * FROM devices WHERE token_sha256 = ? AND active = 1",
            (digest,),
        ).fetchone()
        return _device_from_row(row) if row is not None else None
    finally:
        connection.close()


def touch_device(device_id: str) -> None:
    connection = connect()
    try:
        # Single statement, autocommit (isolation_level=None): an explicit
        # BEGIN IMMEDIATE here only held the write lock longer for an
        # advisory column (351-E).
        connection.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ? AND active = 1",
            (_now_iso(), device_id),
        )
    finally:
        connection.close()
