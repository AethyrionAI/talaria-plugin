"""3D artifact mirror (#362): the ``pre_tool_call`` observer that mirrors
``write_file``/``create_file`` args to the outbox as ``kind="artifact"``.

The hook runs synchronously on the gateway's tool-dispatch hot path, so the
two contracts under test everywhere here are: fail-closed (no row unless
every gate passes) and never-raise (an exception in the mirror must never
break a tool call — even though the gateway would swallow it, our contract
is stricter and pinned)."""

import pytest

from talaria import artifact_mirror, database, outbox, store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def _arm(monkeypatch, tmp_path, *, api_plane=True):
    """Seed one active device, force the plane gate, capture wakes."""
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone-install", "phone")
    monkeypatch.setattr(artifact_mirror, "_api_plane", lambda: api_plane)
    wakes = []
    monkeypatch.setattr(artifact_mirror.HUB, "wake", lambda d=None: wakes.append(d))
    return device_id, wakes


def _fire(**overrides):
    kwargs = {
        "tool_name": "write_file",
        "args": {"path": "notes/a.txt", "content": "abc"},
        "task_id": "s1",
        "session_id": "s1",
        "tool_call_id": "call-1",
        "turn_id": "s1:s1:deadbeef",
    }
    kwargs.update(overrides)
    return artifact_mirror._on_pre_tool_call(**kwargs)


def test_write_file_on_api_plane_appends_artifact_and_wakes(monkeypatch, tmp_path):
    device_id, wakes = _arm(monkeypatch, tmp_path)
    assert _fire() is None  # observer contract: never a directive
    [row] = outbox.pending(device_id)
    assert row["kind"] == "artifact"
    assert row["text"] == "abc"
    assert row["meta"]["session_id"] == "s1"
    assert row["meta"]["turn_id"] == "s1:s1:deadbeef"
    assert row["meta"]["tool_call_id"] == "call-1"
    assert row["meta"]["path"] == "notes/a.txt"
    assert row["meta"]["type"] == "written_file"
    assert row["meta"]["ts"]  # host clock, present and non-empty
    assert wakes == [device_id]


def test_create_file_also_mirrors(monkeypatch, tmp_path):
    device_id, _ = _arm(monkeypatch, tmp_path)
    _fire(tool_name="create_file")
    assert [r["kind"] for r in outbox.pending(device_id)] == ["artifact"]


def test_non_write_tools_produce_nothing(monkeypatch, tmp_path):
    device_id, wakes = _arm(monkeypatch, tmp_path)
    _fire(tool_name="read_file")
    _fire(tool_name="execute_command")
    assert outbox.pending(device_id) == []
    assert wakes == []


def test_non_api_plane_turns_produce_nothing(monkeypatch, tmp_path):
    device_id, wakes = _arm(monkeypatch, tmp_path, api_plane=False)
    _fire()
    assert outbox.pending(device_id) == []
    assert wakes == []


def test_empty_session_id_produces_nothing(monkeypatch, tmp_path):
    device_id, _ = _arm(monkeypatch, tmp_path)
    _fire(session_id="")
    assert outbox.pending(device_id) == []


def test_arg_key_drift_matches_app_tolerance(monkeypatch, tmp_path):
    # The app's WrittenFileArgs accepts path/file_path/filename — the mirror
    # must not be stricter than the sessions plane it replaces.
    device_id, _ = _arm(monkeypatch, tmp_path)
    _fire(args={"file_path": "b.txt", "content": "x"})
    _fire(args={"filename": "c.txt", "content": "y"})
    assert sorted(r["meta"]["path"] for r in outbox.pending(device_id)) == [
        "b.txt",
        "c.txt",
    ]


def test_missing_or_non_string_content_produces_nothing(monkeypatch, tmp_path):
    # A pointer-only write (no content key) has nothing to mirror; a
    # non-string content must never be str()-coerced into a fake artifact.
    device_id, _ = _arm(monkeypatch, tmp_path)
    _fire(args={"path": "a.txt"})
    _fire(args={"path": "a.txt", "content": None})
    _fire(args={"path": "a.txt", "content": {"nested": "dict"}})
    _fire(args="not-a-dict")
    _fire(args={"content": "orphan, no path"})
    assert outbox.pending(device_id) == []


def test_empty_string_content_is_a_real_file(monkeypatch, tmp_path):
    # Writing an empty file is a real write; empty string is not "missing".
    device_id, _ = _arm(monkeypatch, tmp_path)
    _fire(args={"path": "empty.txt", "content": ""})
    [row] = outbox.pending(device_id)
    assert row["text"] == ""
    assert row["meta"]["path"] == "empty.txt"


def test_zero_active_devices_fails_closed_silently(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    monkeypatch.setattr(artifact_mirror, "_api_plane", lambda: True)
    wakes = []
    monkeypatch.setattr(artifact_mirror.HUB, "wake", lambda d=None: wakes.append(d))
    _fire()  # must not raise
    assert wakes == []


def test_multiple_active_devices_each_get_their_own_row(monkeypatch, tmp_path):
    # #366 design correction (2026-08-18): the v0 exactly-one gate silently
    # dropped every artifact on a two-device host (iPhone + iPad,
    # measured — zero outbox rows across two live write turns). Multi-device
    # hosts are first-class now: one independently-acknowledged targeted row
    # per active device, one wake each.
    phone_id, wakes = _arm(monkeypatch, tmp_path)
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    _fire()  # must not raise
    [phone_row] = outbox.pending(phone_id)
    [ipad_row] = outbox.pending(ipad_id)
    assert phone_row["kind"] == ipad_row["kind"] == "artifact"
    assert phone_row["text"] == ipad_row["text"] == "abc"
    assert phone_row["meta"]["path"] == ipad_row["meta"]["path"] == "notes/a.txt"
    assert phone_row["id"] != ipad_row["id"]
    assert sorted(wakes) == sorted([phone_id, ipad_id])


def test_multi_device_rows_acknowledge_independently(monkeypatch, tmp_path):
    # Acking one device's copy must not consume the other's — the rows are
    # separate targeted items, not one shared row.
    phone_id, _ = _arm(monkeypatch, tmp_path)
    ipad_id, _ = store.create_paired_device("ipad-install", "ipad")
    _fire()
    [phone_row] = outbox.pending(phone_id)
    outbox.mark_delivered([phone_row["id"]], device_id=phone_id)
    assert outbox.pending(phone_id) == []
    assert len(outbox.pending(ipad_id)) == 1


def test_handler_never_raises_on_fanout(monkeypatch, tmp_path):
    # The multi-device append is the new storage seam — a raise there must
    # be swallowed like every other.
    _arm(monkeypatch, tmp_path)
    store.create_paired_device("ipad-install", "ipad")
    monkeypatch.setattr(
        artifact_mirror.outbox,
        "append_for_devices",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("storage down")),
    )
    assert _fire() is None


def test_handler_never_raises(monkeypatch, tmp_path):
    _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(
        artifact_mirror.outbox,
        "append",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("storage down")),
    )
    assert _fire() is None

    monkeypatch.setattr(
        artifact_mirror,
        "_api_plane",
        lambda: (_ for _ in ()).throw(RuntimeError("gateway import broke")),
    )
    assert _fire() is None

    monkeypatch.setattr(
        artifact_mirror.store,
        "active_devices",
        lambda: (_ for _ in ()).throw(RuntimeError("db locked")),
    )
    assert _fire() is None


def test_api_plane_reads_real_session_context():
    # Pin the real discriminator against the real gateway module: empty
    # context reads False; a bound api_server context reads True. If the
    # gateway ever renames the var, this test — not the phone — finds out.
    from gateway.session_context import clear_session_vars, set_session_vars

    assert artifact_mirror._api_plane() is False
    tokens = set_session_vars(platform="api_server", session_id="s1")
    try:
        assert artifact_mirror._api_plane() is True
    finally:
        clear_session_vars(tokens)
    assert artifact_mirror._api_plane() is False


def test_register_hooks_registers_pre_tool_call():
    recorded = []

    class _Ctx:
        def register_hook(self, name, callback):
            recorded.append((name, callback))

    artifact_mirror.register_hooks(_Ctx())
    assert recorded == [("pre_tool_call", artifact_mirror._on_pre_tool_call)]
