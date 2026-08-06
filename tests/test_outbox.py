import importlib

from .. import outbox


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, "_outbox_path", lambda: tmp_path / "outbox.json")


def test_append_then_pending(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    item = outbox.append("hello phone", meta={"source": "test"})
    assert item["kind"] == "message"
    assert item["text"] == "hello phone"
    rows = outbox.pending()
    assert [r["id"] for r in rows] == [item["id"]]


def test_pending_is_oldest_first_and_excludes_delivered(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    first = outbox.append("one")
    second = outbox.append("two")
    outbox.mark_delivered([first["id"]])
    rows = outbox.pending()
    assert [r["id"] for r in rows] == [second["id"]]


def test_mark_delivered_is_idempotent_and_reports_only_real_acks(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    item = outbox.append("one")
    assert outbox.mark_delivered([item["id"], "nonsense"]) == [item["id"]]
    assert outbox.mark_delivered([item["id"]]) == []


def test_outbox_survives_reload(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    item = outbox.append("durable")
    # Fresh read from disk — nothing cached in module state.
    rows = outbox.pending()
    assert rows and rows[0]["id"] == item["id"]
    raw = (tmp_path / "outbox.json").read_text(encoding="utf-8")
    assert "durable" in raw
