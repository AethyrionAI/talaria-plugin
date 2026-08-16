import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from .. import store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_store_path", lambda: tmp_path / "devices.json")


def test_create_paired_device_persists_hash_not_token(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, token = store.create_paired_device("install-1", "Owen's iPhone")
    rows = store.active_devices()
    assert len(rows) == 1
    assert rows[0]["id"] == device_id
    assert rows[0]["install_id"] == "install-1"
    assert rows[0]["name"] == "Owen's iPhone"
    assert rows[0]["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    connection = sqlite3.connect(tmp_path / "talaria.db")
    try:
        dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert token not in dump


def test_repair_same_install_rotates(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    _old_id, old_token = store.create_paired_device("install-1", "phone")
    new_id, new_token = store.create_paired_device("install-1", "phone")
    actives = store.active_devices()
    assert [d["id"] for d in actives] == [new_id]
    assert store.device_for_token(old_token) is None
    assert store.device_for_token(new_token)["id"] == new_id
    # Old row kept, deactivated — never deleted.
    assert len(store.devices()) == 2


def test_device_for_token_rejects_garbage(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    store.create_paired_device("install-1", "phone")
    assert store.device_for_token("not-a-token") is None


def test_touch_device_stamps_last_seen(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("install-1", "phone")
    store.touch_device(device_id)
    assert store.active_devices()[0]["last_seen"] is not None


def test_concurrent_pairings_preserve_every_device(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    installs = [f"install-{index}" for index in range(100)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        pairs = list(pool.map(lambda install: store.create_paired_device(install, install), installs))

    assert len({device_id for device_id, _ in pairs}) == len(installs)
    assert {device["install_id"] for device in store.active_devices()} == set(installs)
