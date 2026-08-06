"""Device/pairing store for the Talaria plugin.

A small JSON file under HERMES_HOME — profile-aware via the canonical
resolver, so per-profile installs never share pairing state. Records are
deactivated, never deleted (rollback stays possible; mirrors the Talaria
tracker's #144 convention).

Tokens are stored as SHA-256 hashes only. The plaintext pairing token is
printed exactly once by ``hermes talaria pair`` and never persisted.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import get_hermes_home


def _store_path() -> Path:
    return Path(get_hermes_home()) / "talaria" / "devices.json"


def _load() -> dict:
    path = _store_path()
    if not path.exists():
        return {"devices": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt store must not brick the CLI; keep the bad file aside.
        backup = path.with_suffix(".json.corrupt")
        try:
            path.rename(backup)
        except OSError:
            pass
        return {"devices": []}


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_pairing() -> tuple[str, str]:
    """Create a device record and return ``(device_id, plaintext_token)``.

    The token is returned to the caller for one-time display and only its
    hash is persisted.
    """
    token = secrets.token_urlsafe(32)
    device_id = uuid.uuid4().hex[:12]
    data = _load()
    data["devices"].append({
        "id": device_id,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "created": _now_iso(),
        "active": True,
        "last_seen": None,
        "name": None,
    })
    _save(data)
    return device_id, token


def devices() -> list[dict]:
    return list(_load().get("devices", []))


def active_devices() -> list[dict]:
    return [d for d in devices() if d.get("active")]


def deactivate(device_id: str | None = None) -> int:
    """Deactivate one device (or all when ``device_id`` is None).

    Returns the number of records deactivated. Records are kept for
    rollback — flip ``active`` back to true by hand if a deactivation was
    a mistake.
    """
    data = _load()
    count = 0
    for device in data.get("devices", []):
        if device.get("active") and (device_id is None or device.get("id") == device_id):
            device["active"] = False
            device["deactivated"] = _now_iso()
            count += 1
    if count:
        _save(data)
    return count


def create_paired_device(install_id: str, name: str) -> tuple[str, str]:
    """App-driven pairing (2A): mint a device bound to a durable install id.

    Re-pairing the same install deactivates the prior row first (#144 —
    rotate, never accumulate; rollback stays possible).
    """
    data = _load()
    for device in data.get("devices", []):
        if device.get("active") and device.get("install_id") == install_id:
            device["active"] = False
            device["deactivated"] = _now_iso()
    token = secrets.token_urlsafe(32)
    device_id = uuid.uuid4().hex[:12]
    data["devices"].append({
        "id": device_id,
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "created": _now_iso(),
        "active": True,
        "last_seen": None,
        "name": name or None,
        "install_id": install_id,
    })
    _save(data)
    return device_id, token


def device_for_token(token: str) -> dict | None:
    digest = hashlib.sha256((token or "").encode("utf-8")).hexdigest()
    for device in active_devices():
        if device.get("token_sha256") == digest:
            return device
    return None


def touch_device(device_id: str) -> None:
    data = _load()
    for device in data.get("devices", []):
        if device.get("id") == device_id and device.get("active"):
            device["last_seen"] = _now_iso()
            _save(data)
            return
