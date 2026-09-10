import argparse
from types import SimpleNamespace

import pytest

from talaria import admin, database, outbox, store


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


def _args(text, *, device=None, send_all=False):
    return SimpleNamespace(
        talaria_cmd="send",
        text=text.split(),
        device=device,
        send_all=send_all,
    )


def test_cli_send_with_one_active_device_targets_it(monkeypatch, tmp_path, capsys):
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("phone", "phone")

    result = admin.handle_cli(_args("hello phone"))

    assert result == 0
    rows = outbox.pending(device_id)
    assert [row["text"] for row in rows] == ["hello phone"]
    assert device_id in capsys.readouterr().out


def test_cli_send_with_multiple_devices_requires_selector(monkeypatch, tmp_path, capsys):
    _redirect(monkeypatch, tmp_path)
    store.create_paired_device("phone", "phone")
    store.create_paired_device("ipad", "ipad")

    result = admin.handle_cli(_args("ambiguous"))

    assert result == 1
    output = capsys.readouterr().out
    assert "--device <id> or --all" in output
    assert outbox.all_pending_for_diagnostics() == []


def test_cli_device_targets_exactly_one(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    phone_id, _ = store.create_paired_device("phone", "phone")
    ipad_id, _ = store.create_paired_device("ipad", "ipad")

    result = admin.handle_cli(_args("phone only", device=phone_id))

    assert result == 0
    assert [row["text"] for row in outbox.pending(phone_id)] == ["phone only"]
    assert outbox.pending(ipad_id) == []


def test_cli_all_creates_independent_targeted_deliveries(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    phone_id, _ = store.create_paired_device("phone", "phone")
    ipad_id, _ = store.create_paired_device("ipad", "ipad")

    result = admin.handle_cli(_args("everyone", send_all=True))

    assert result == 0
    [phone_item] = outbox.pending(phone_id)
    [ipad_item] = outbox.pending(ipad_id)
    assert phone_item["id"] != ipad_item["id"]
    assert outbox.mark_delivered([phone_item["id"]], device_id=phone_id) == [phone_item["id"]]
    assert outbox.pending(phone_id) == []
    assert [row["id"] for row in outbox.pending(ipad_id)] == [ipad_item["id"]]


def test_cli_send_failures_return_nonzero_without_queueing(monkeypatch, tmp_path, capsys):
    _redirect(monkeypatch, tmp_path)

    assert admin.handle_cli(_args("")) == 1
    assert admin.handle_cli(_args("no target")) == 1

    device_id, _ = store.create_paired_device("phone", "phone")
    assert admin.handle_cli(_args("wrong target", device="missing")) == 1
    assert outbox.pending(device_id) == []
    assert "No message was queued" in capsys.readouterr().out


def test_cli_unpair_missing_device_returns_nonzero(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    assert admin.handle_cli(SimpleNamespace(talaria_cmd="unpair", device_id="missing")) == 1


def test_cli_parser_exposes_mutually_exclusive_device_and_all_flags():
    parser = argparse.ArgumentParser()
    admin.setup_cli(parser)

    selected = parser.parse_args(["send", "--device", "phone-1", "hello"])
    assert selected.device == "phone-1"
    assert selected.send_all is False

    broadcast = parser.parse_args(["send", "--all", "hello"])
    assert broadcast.device is None
    assert broadcast.send_all is True

    with pytest.raises(SystemExit):
        parser.parse_args(["send", "--device", "phone-1", "--all", "hello"])


def test_status_prints_device_names(monkeypatch, tmp_path, capsys):
    """351-H: --device <id> is mandatory at >1 active, so status must let
    the operator tell the ids apart."""
    _redirect(monkeypatch, tmp_path)
    device_id, _ = store.create_paired_device("i-phone", "Owen's iPhone")
    admin.handle_cli(SimpleNamespace(talaria_cmd="status"))
    out = capsys.readouterr().out
    assert "Owen's iPhone" in out
    assert "i-phone" in out
    assert device_id in out


def test_the_orphan_manual_pairing_arm_is_gone(monkeypatch, tmp_path, capsys):
    """#309 Lane D / #412: `hermes talaria pair` minted an install_id-less
    token that the app had no way to redeem — a flow the Pairing & Devices
    screen advertised and that dead-ended at both ends. `pair-qr` replaces
    it. The parser must reject the old verb rather than quietly accept it."""
    parser = argparse.ArgumentParser()
    admin.setup_cli(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(["pair"])

    _redirect(monkeypatch, tmp_path)
    result = admin.handle_cli(SimpleNamespace(talaria_cmd="pair"))
    out = capsys.readouterr().out
    # An unknown verb falls through to status, which must not mint anything.
    assert result == 0
    assert "One-time pairing token" not in out
    assert store.devices() == []


def test_status_points_at_pair_qr_when_no_device_is_paired(monkeypatch, tmp_path, capsys):
    """The empty-store hint named a command that no longer exists."""
    _redirect(monkeypatch, tmp_path)
    admin.handle_cli(SimpleNamespace(talaria_cmd="status"))
    out = capsys.readouterr().out
    assert "hermes talaria pair-qr" in out
    assert "hermes talaria pair\n" not in out
