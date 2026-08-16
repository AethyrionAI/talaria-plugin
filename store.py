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
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ? AND active = 1",
            (_now_iso(), device_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
