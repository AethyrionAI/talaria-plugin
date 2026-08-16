import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from threading import Barrier

from .. import database, outbox, store
from ..database import connect


def _process_append(home: str, index: int) -> tuple[str, str]:
    os.environ["HERMES_HOME"] = home
    from .. import outbox as process_outbox

    item = process_outbox.append(f"process-item-{index}")
    return item["id"], item["text"]


def _process_pair(home: str, index: int) -> tuple[str, str]:
    os.environ["HERMES_HOME"] = home
    from .. import store as process_store

    device_id, _ = process_store.create_paired_device(
        f"process-install-{index}", f"process-device-{index}"
    )
    return device_id, f"process-install-{index}"


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def test_cross_process_appends_preserve_every_item(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    device_id, _ = store.create_paired_device("phone-install", "phone")
    texts = {f"process-item-{index}" for index in range(48)}

    with ProcessPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(_process_append, [str(hermes_home)] * len(texts), range(len(texts))))

    assert len({item_id for item_id, _ in items}) == len(texts)
    assert {item["text"] for item in outbox.pending(device_id)} == texts


def test_cross_process_pairings_preserve_every_device(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    installs = {f"process-install-{index}" for index in range(48)}

    with ProcessPoolExecutor(max_workers=8) as pool:
        pairs = list(pool.map(_process_pair, [str(hermes_home)] * len(installs), range(len(installs))))

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    assert len({device_id for device_id, _ in pairs}) == len(installs)
    assert {device["install_id"] for device in store.active_devices()} == installs


def test_concurrent_append_and_ack_do_not_lose_or_resurrect_items(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("install-1", "phone")
    old_item = outbox.append("old", target_device_id=device_id)
    barrier = Barrier(2)

    def acknowledge():
        barrier.wait()
        return outbox.mark_delivered([old_item["id"]], device_id=device_id)

    def append_new():
        barrier.wait()
        return outbox.append("new", target_device_id=device_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        ack_future = pool.submit(acknowledge)
        append_future = pool.submit(append_new)
        acked = ack_future.result()
        new_item = append_future.result()

    assert acked == [old_item["id"]]
    assert [item["id"] for item in outbox.pending(device_id)] == [new_item["id"]]


def test_concurrent_pairing_and_touch_preserve_both_updates(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    existing_id, _ = store.create_paired_device("existing-install", "phone")
    barrier = Barrier(2)

    def touch_existing():
        barrier.wait()
        store.touch_device(existing_id)

    def pair_new():
        barrier.wait()
        return store.create_paired_device("new-install", "ipad")

    with ThreadPoolExecutor(max_workers=2) as pool:
        touch_future = pool.submit(touch_existing)
        pair_future = pool.submit(pair_new)
        touch_future.result()
        new_id, _ = pair_future.result()

    by_id = {device["id"]: device for device in store.active_devices()}
    assert by_id[existing_id]["last_seen"] is not None
    assert by_id[new_id]["install_id"] == "new-install"


def test_concurrent_legacy_any_drains_claim_item_for_exactly_one_device(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    phone_id, _ = store.create_paired_device("phone-install", "phone")
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    legacy_item = {
        "id": "legacy-pending",
        "text": "legacy pending",
    }
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO outbox_items (
                id, kind, text, created_at, target_device_id, delivery_scope,
                claimed_by_device_id, delivered_at, active, meta_json
            ) VALUES (?, 'message', ?, ?, NULL, 'legacy_any', NULL, NULL, 1, '{}')
            """,
            (legacy_item["id"], legacy_item["text"], "2026-08-01T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()
    barrier = Barrier(2)

    def drain(device_id):
        barrier.wait()
        return outbox.pending(device_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        phone_future = pool.submit(drain, phone_id)
        ipad_future = pool.submit(drain, ipad_id)
        results = [phone_future.result(), ipad_future.result()]

    owners = [rows for rows in results if [row["id"] for row in rows] == [legacy_item["id"]]]
    assert len(owners) == 1
    assert sum(len(rows) for rows in results) == 1
