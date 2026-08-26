import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from talaria import database, store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def test_no_install_id_less_mint_survives(monkeypatch, tmp_path):
    """#309 Lane D / #412: ``create_pairing`` minted a device row with a
    NULL ``install_id``, so nothing could ever rotate it and the app had no
    way to redeem its token. The wire ``pair`` verb (``create_paired_device``)
    is the only mint left. Deleted, not merely unused."""
    assert not hasattr(store, "create_pairing")

    _redirect(monkeypatch, tmp_path)
    store.create_paired_device("install-1", "phone")
    assert all(device.get("install_id") for device in store.active_devices())


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


def test_repair_rehomes_pending_targeted_rows(monkeypatch, tmp_path):
    """351-D RED->GREEN: re-pairing must not orphan queued messages."""
    _redirect(monkeypatch, tmp_path)
    from talaria import outbox
    old_id, _ = store.create_paired_device("install-1", "phone")
    item = outbox.append("queued while offline", target_device_id=old_id)
    new_id, _ = store.create_paired_device("install-1", "phone")

    assert [row["id"] for row in outbox.pending(new_id)] == [item["id"]]
    assert outbox.mark_delivered([item["id"]], device_id=new_id) == [item["id"]]


def test_repair_releases_legacy_claims_of_inactive_devices(monkeypatch, tmp_path):
    """351-D RED->GREEN: a claim held by a rotated-away device is released."""
    _redirect(monkeypatch, tmp_path)
    from talaria import database, outbox
    old_id, _ = store.create_paired_device("install-1", "phone")
    connection = database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO outbox_items (id, kind, text, created_at, target_device_id,"
            " delivery_scope, claimed_by_device_id, delivered_at, active, meta_json)"
            " VALUES ('leg-1', 'message', 'legacy', '2026-08-01T00:00:00+00:00',"
            " NULL, 'legacy_any', NULL, NULL, 1, '{}')"
        )
        connection.commit()
    finally:
        connection.close()
    assert [row["id"] for row in outbox.pending(old_id)] == ["leg-1"]   # claimed, never acked
    new_id, _ = store.create_paired_device("install-1", "phone")

    assert [row["id"] for row in outbox.pending(new_id)] == ["leg-1"]   # released, re-claimable
