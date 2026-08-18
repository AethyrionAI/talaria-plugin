"""#363 outbox hygiene: artifact-kind rows scrub (delivered) or expire
(undelivered) at 7 days — deactivate/blank, never DELETE (the #144 shape).
Message-kind rows are OUT of scope in v0 and every arm pins that."""

from datetime import datetime, timedelta, timezone

from talaria import artifact_mirror, database, hygiene, outbox, store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _backdate(item_id: str, *, created_days_ago=None, delivered_days_ago=None):
    connection = database.connect()
    try:
        if created_days_ago is not None:
            connection.execute(
                "UPDATE outbox_items SET created_at = ? WHERE id = ?",
                (_iso(created_days_ago), item_id),
            )
        if delivered_days_ago is not None:
            connection.execute(
                "UPDATE outbox_items SET delivered_at = ? WHERE id = ?",
                (_iso(delivered_days_ago), item_id),
            )
        connection.commit()
    finally:
        connection.close()


def _row(item_id: str) -> dict:
    connection = database.connect()
    try:
        row = connection.execute(
            "SELECT * FROM outbox_items WHERE id = ?", (item_id,)
        ).fetchone()
        return dict(row) if row is not None else {}
    finally:
        connection.close()


def _seed(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    return device_id


# ---- 363-A: scrub of delivered artifact rows ----

def test_old_delivered_artifact_scrubs_but_row_survives(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("big file bytes", meta={"path": "a.md"},
                         target_device_id=device_id, kind="artifact")
    outbox.mark_delivered([item["id"]], device_id=device_id)
    _backdate(item["id"], delivered_days_ago=8)

    counts = hygiene.sweep()

    assert counts == {"scrubbed": 1, "expired": 0}
    row = _row(item["id"])
    assert row  # never deleted
    assert row["text"] == ""
    assert row["active"] == 0
    assert row["delivered_at"] is not None
    assert "a.md" in row["meta_json"]  # meta is the retained audit trail


def test_young_delivered_artifact_is_untouched(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("fresh", meta={"path": "b.md"},
                         target_device_id=device_id, kind="artifact")
    outbox.mark_delivered([item["id"]], device_id=device_id)
    _backdate(item["id"], delivered_days_ago=6)
    before = _row(item["id"])

    assert hygiene.sweep() == {"scrubbed": 0, "expired": 0}
    assert _row(item["id"]) == before  # byte-for-byte


# ---- 363-B: expiry of undelivered artifact rows + message scope pin ----

def test_old_undelivered_artifact_expires_and_stops_serving(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("stranded", meta={"path": "c.md"},
                         target_device_id=device_id, kind="artifact")
    _backdate(item["id"], created_days_ago=8)

    counts = hygiene.sweep()

    assert counts == {"scrubbed": 0, "expired": 1}
    row = _row(item["id"])
    assert row["active"] == 0
    assert row["text"] == "stranded"  # expiry deactivates; only DELIVERED rows scrub
    assert outbox.pending(device_id) == []


def test_young_undelivered_artifact_still_serves(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("queued", meta={"path": "d.md"},
                         target_device_id=device_id, kind="artifact")
    _backdate(item["id"], created_days_ago=6)

    assert hygiene.sweep() == {"scrubbed": 0, "expired": 0}
    assert [r["id"] for r in outbox.pending(device_id)] == [item["id"]]


def test_message_rows_are_untouched_by_every_arm(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    delivered = outbox.append("old inbox note", target_device_id=device_id)
    outbox.mark_delivered([delivered["id"]], device_id=device_id)
    _backdate(delivered["id"], delivered_days_ago=400)
    stranded = outbox.append("undelivered note", target_device_id=device_id)
    _backdate(stranded["id"], created_days_ago=400)

    assert hygiene.sweep() == {"scrubbed": 0, "expired": 0}
    assert _row(delivered["id"])["text"] == "old inbox note"
    assert _row(stranded["id"])["active"] == 1
    assert [r["id"] for r in outbox.pending(device_id)] == [stranded["id"]]


# ---- 363-D: idempotence + no resurrection ----

def test_sweep_is_idempotent(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("bytes", meta={"path": "e.md"},
                         target_device_id=device_id, kind="artifact")
    outbox.mark_delivered([item["id"]], device_id=device_id)
    _backdate(item["id"], delivered_days_ago=9)

    assert hygiene.sweep() == {"scrubbed": 1, "expired": 0}
    assert hygiene.sweep() == {"scrubbed": 0, "expired": 0}


def test_ack_after_expiry_cannot_resurrect(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("late ack", meta={"path": "f.md"},
                         target_device_id=device_id, kind="artifact")
    _backdate(item["id"], created_days_ago=8)
    assert hygiene.sweep()["expired"] == 1

    outbox.mark_delivered([item["id"]], device_id=device_id)
    row = _row(item["id"])
    assert row["active"] == 0
    assert outbox.pending(device_id) == []


# ---- 363-C: triggers ----

def test_maybe_sweep_honors_the_throttle(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    ran = []
    monkeypatch.setattr(hygiene, "sweep", lambda: ran.append(1) or {"scrubbed": 0, "expired": 0})
    monkeypatch.setattr(hygiene, "_last_sweep_at", None)

    clock = iter([0.0, 100.0, 6 * 3600 + 1.0]).__next__
    hygiene.maybe_sweep(now=clock)
    hygiene.maybe_sweep(now=clock)   # 100 s later — throttled
    assert len(ran) == 1
    hygiene.maybe_sweep(now=clock)   # past the 6 h floor — runs
    assert len(ran) == 2


def test_maybe_sweep_never_raises(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(
        hygiene, "sweep",
        lambda: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    monkeypatch.setattr(hygiene, "_last_sweep_at", None)
    hygiene.maybe_sweep(now=iter([0.0]).__next__)  # must not raise


def test_mirror_append_triggers_maybe_sweep(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(artifact_mirror, "_api_plane", lambda: True)
    monkeypatch.setattr(artifact_mirror.HUB, "wake", lambda d=None: None)
    called = []
    monkeypatch.setattr(artifact_mirror.hygiene, "maybe_sweep", lambda: called.append(1))

    artifact_mirror._on_pre_tool_call(
        tool_name="write_file",
        args={"path": "g.md", "content": "x"},
        session_id="s1",
    )
    assert called == [1]


def test_register_runs_the_startup_sweep_and_survives_a_raising_one(monkeypatch, tmp_path):
    import talaria

    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(talaria.tools, "register_tools", lambda ctx: None)
    monkeypatch.setattr(talaria.admin, "register_cli", lambda ctx: None)

    class _Ctx:
        def register_hook(self, *args, **kwargs):
            pass

        def register_platform(self, **kwargs):
            pass

    called = []
    monkeypatch.setattr(hygiene, "maybe_sweep", lambda: called.append(1))
    talaria.register(_Ctx())
    assert called == [1]

    monkeypatch.setattr(
        hygiene, "maybe_sweep",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    talaria.register(_Ctx())  # must not raise — gateway load survives


def test_prune_cli_reports_dry_run_then_prunes(monkeypatch, tmp_path, capsys):
    from argparse import Namespace

    from talaria import admin

    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("bytes", meta={"path": "cli.md"},
                         target_device_id=device_id, kind="artifact")
    outbox.mark_delivered([item["id"]], device_id=device_id)
    _backdate(item["id"], delivered_days_ago=8)

    assert admin.handle_cli(Namespace(talaria_cmd="prune", dry_run=True)) == 0
    assert "Would scrub 1" in capsys.readouterr().out
    assert _row(item["id"])["text"] == "bytes"  # dry run wrote nothing

    assert admin.handle_cli(Namespace(talaria_cmd="prune", dry_run=False)) == 0
    assert "Scrubbed 1" in capsys.readouterr().out
    assert _row(item["id"])["text"] == ""


def test_dry_run_counts_without_writing(monkeypatch, tmp_path):
    device_id = _seed(monkeypatch, tmp_path)
    item = outbox.append("bytes", meta={"path": "h.md"},
                         target_device_id=device_id, kind="artifact")
    outbox.mark_delivered([item["id"]], device_id=device_id)
    _backdate(item["id"], delivered_days_ago=8)

    counts = hygiene.sweep(dry_run=True)
    assert counts == {"scrubbed": 1, "expired": 0}
    row = _row(item["id"])
    assert row["text"] == "bytes"      # nothing written
    assert row["active"] == 1
    # …and the real sweep still finds it afterwards.
    assert hygiene.sweep() == {"scrubbed": 1, "expired": 0}
