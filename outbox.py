"""Durable agent→phone outbox for the Talaria plugin.

Same JSON-file family as store.py (HERMES_HOME/talaria/), same #144
convention: items are marked delivered, never deleted. `pending()` is
fetch-on-connect by construction — the first drain after days away gets
the whole backlog, oldest first.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hermes_constants import get_hermes_home


def _outbox_path() -> Path:
    return Path(get_hermes_home()) / "talaria" / "outbox.json"


def _load() -> dict:
    path = _outbox_path()
    if not path.exists():
        return {"items": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        backup = path.with_suffix(".json.corrupt")
        try:
            path.rename(backup)
        except OSError:
            pass
        return {"items": []}


def _save(data: dict) -> None:
    path = _outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(text: str, meta: dict | None = None) -> dict:
    # Same strict-decode shape as transport.enqueue_query's params: the app
    # decodes item meta as [String: String], so one non-string value would
    # never decode, never ack, and fail every drain forever for a DURABLE
    # item (#251 latent finding — live writers only produce strings today;
    # closing the class here rather than trusting every future caller).
    safe_meta = {str(k): str(v) for k, v in (meta or {}).items()}
    item = {
        "id": uuid.uuid4().hex[:12],
        "kind": "message",
        "text": text,
        "created_at": _now_iso(),
        "meta": safe_meta,
        "delivered_at": None,
        "active": True,
    }
    data = _load()
    data["items"].append(item)
    _save(data)
    return item


def pending() -> list[dict]:
    data = _load()
    return [
        i for i in data.get("items", [])
        if i.get("active") and not i.get("delivered_at")
    ]


def mark_delivered(item_ids: list[str]) -> list[str]:
    wanted = set(item_ids or [])
    data = _load()
    acked: list[str] = []
    for item in data.get("items", []):
        if item.get("id") in wanted and not item.get("delivered_at"):
            item["delivered_at"] = _now_iso()
            acked.append(item["id"])
    if acked:
        _save(data)
    return acked
