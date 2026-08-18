from concurrent.futures import ThreadPoolExecutor

import pytest

from talaria import database, outbox, store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def test_append_then_pending(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    item = outbox.append("hello phone", meta={"source": "test"})
    assert item["kind"] == "message"
    assert item["text"] == "hello phone"
    rows = outbox.pending(device_id)
    assert [r["id"] for r in rows] == [item["id"]]


def test_untargeted_append_requires_exactly_one_active_device(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)

    with pytest.raises(outbox.UnknownTargetError, match="no active"):
        outbox.append("nobody")

    phone_id, _ = store.create_paired_device("phone-install", "phone")
    item = outbox.append("one target")
    assert [row["id"] for row in outbox.pending(phone_id)] == [item["id"]]

    store.create_paired_device("ipad-install", "ipad")
    with pytest.raises(outbox.UnknownTargetError, match="multiple active"):
        outbox.append("ambiguous")


def test_pending_is_oldest_first_and_excludes_delivered(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("install-1", "phone")
    first = outbox.append("one", target_device_id=device_id)
    second = outbox.append("two", target_device_id=device_id)
    outbox.mark_delivered([first["id"]], device_id=device_id)
    rows = outbox.pending(device_id)
    assert [r["id"] for r in rows] == [second["id"]]


def test_mark_delivered_is_idempotent_and_reports_only_real_acks(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("install-1", "phone")
    item = outbox.append("one", target_device_id=device_id)
    assert outbox.mark_delivered([item["id"], "nonsense"], device_id=device_id) == [item["id"]]
    assert outbox.mark_delivered([item["id"]], device_id=device_id) == []


def test_append_stringifies_non_string_meta_values(monkeypatch, tmp_path):
    # A DURABLE item with a non-string meta value would never decode
    # app-side (strict [String: String]), never ack, and fail every drain
    # forever — close the class even though live writers only produce
    # strings today (#251 finding 2, latent).
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    item = outbox.append("hello", meta={"n": 7})
    assert item["meta"] == {"n": "7"}
    [row] = outbox.pending(device_id)
    assert row["meta"] == {"n": "7"}


def test_outbox_survives_reload(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    item = outbox.append("durable")
    # Fresh connection to SQLite — nothing cached in module state.
    rows = outbox.pending(device_id)
    assert rows and rows[0]["id"] == item["id"]
    assert (tmp_path / "talaria.db").exists()
    assert "durable" in [row["text"] for row in rows]


def test_concurrent_appends_preserve_every_item(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    texts = [f"item-{index}" for index in range(100)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        items = list(pool.map(outbox.append, texts))

    assert len({item["id"] for item in items}) == len(texts)
    assert {item["text"] for item in outbox.pending(device_id)} == set(texts)


def test_targeted_item_is_visible_only_to_its_device(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    phone_id, _ = store.create_paired_device("phone-install", "phone")
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    item = outbox.append("phone only", target_device_id=phone_id)

    assert [row["id"] for row in outbox.pending(phone_id)] == [item["id"]]
    assert outbox.pending(ipad_id) == []


def test_append_kind_artifact_rides_row_and_wire(monkeypatch, tmp_path):
    # 3D (#362): artifact-kind items must survive append → pending intact,
    # because the app routes on `kind` — a row silently coerced back to
    # "message" would land file contents in the phone's inbox as junk.
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    item = outbox.append(
        "file-bytes",
        meta={"path": "a.txt"},
        target_device_id=device_id,
        kind="artifact",
    )
    assert item["kind"] == "artifact"
    [row] = outbox.pending(device_id)
    assert row["kind"] == "artifact"
    assert row["text"] == "file-bytes"


def test_append_kind_defaults_to_message(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    item = outbox.append("hi", target_device_id=device_id)
    assert item["kind"] == "message"
    [row] = outbox.pending(device_id)
    assert row["kind"] == "message"


def test_append_for_devices_carries_kind(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    phone_id, _ = store.create_paired_device("phone-install", "phone")
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    items = outbox.append_for_devices("blob", [phone_id, ipad_id], kind="artifact")
    assert [item["kind"] for item in items] == ["artifact", "artifact"]
    assert [row["kind"] for row in outbox.pending(phone_id)] == ["artifact"]
    assert [row["kind"] for row in outbox.pending(ipad_id)] == ["artifact"]


def test_non_target_device_cannot_acknowledge_item(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    phone_id, _ = store.create_paired_device("phone-install", "phone")
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    item = outbox.append("phone only", target_device_id=phone_id)

    assert outbox.mark_delivered([item["id"]], device_id=ipad_id) == []
    assert [row["id"] for row in outbox.pending(phone_id)] == [item["id"]]
    assert outbox.mark_delivered([item["id"]], device_id=phone_id) == [item["id"]]
