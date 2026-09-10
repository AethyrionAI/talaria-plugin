"""#224's plugin verb (Talaria-27 tracker, bars 224-V-A..F).

The phone gets upstream's own persistent approval-mode selection —
``run_approval_mode_command``, the same canonical ``set_config_value``
chokepoint the gateway's ``/approvals`` slash command uses — behind the
envelope's device auth. Design calls on record in the tracker: paired-device
auth stands in for the slash gate's admin check, and ``/yolo`` is
deliberately NOT exposed.

The conftest's autouse fixture isolates ``HERMES_HOME`` per test, which is
what makes the set arm safe to exercise: upstream honors the env var
(``hermes_constants.get_hermes_home``), and an empty isolated home reads
upstream's default mode ("smart") — probed before these tests were written.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from talaria import database, outbox, store
from talaria.envelope import EnvelopeService
from talaria.transport import TransportHub

API_KEY = "test-api-key-" + "64chars-" + "a" * 43
assert len(API_KEY) == 64


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
    service = EnvelopeService(
        api_key_provider=lambda: API_KEY,
        hub=TransportHub(),
        store_mod=store,
        outbox_mod=outbox,
        hold_seconds=0.05,
        touch_throttle_seconds=0.0,
    )
    return service


async def _paired(service):
    result = await service.dispatch(
        {"type": "pair", "auth": API_KEY, "install_id": "i-1", "device_name": "p"}
    )
    return result["device_id"], result["device_token"]


# --- 224-V-A ---------------------------------------------------------------


async def test_bogus_auth_is_refused_before_any_config_read(env):
    refused = await env.dispatch(
        {"type": "approval_mode", "device_id": "ghost", "auth": "junk"}
    )
    assert refused["code"] == "device_auth_mismatch"


async def test_read_arm_returns_the_effective_mode_without_mutating(env):
    device_id, token = await _paired(env)
    result = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token}
    )
    assert result["ok"] is True
    assert result["mode"] == "smart"  # upstream's default on an empty home
    assert result["changed"] is False
    assert result["message"].startswith("Approval mode:")


# --- 224-V-B ---------------------------------------------------------------


async def test_set_arm_persists_through_the_canonical_chokepoint(env):
    device_id, token = await _paired(env)
    set_result = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token, "mode": "off"}
    )
    assert set_result["ok"] is True
    assert set_result["mode"] == "off"
    assert set_result["changed"] is True

    read_back = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token}
    )
    assert read_back["mode"] == "off", "the isolated home's config now carries the mode"


# --- 224-V-C ---------------------------------------------------------------


async def test_invalid_mode_passes_through_upstreams_rejection(env):
    device_id, token = await _paired(env)
    result = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token, "mode": "yolo"}
    )
    assert result["ok"] is False
    assert "Usage:" in result["message"]

    read_back = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token}
    )
    assert read_back["mode"] == "smart", "a rejected mode mutates nothing"


async def test_non_string_mode_is_malformed_not_a_crash(env):
    device_id, token = await _paired(env)
    result = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token, "mode": 7}
    )
    assert result["code"] == "malformed_mode"


# --- 224-V-D ---------------------------------------------------------------


async def test_yolo_is_not_a_verb(env):
    device_id, token = await _paired(env)
    result = await env.dispatch(
        {"type": "yolo", "device_id": device_id, "auth": token}
    )
    assert result["code"] == "unknown_event_type"


def test_dispatch_source_carries_no_yolo_key():
    source_path = Path(__file__).resolve().parent.parent / "talaria" / "envelope.py"
    source = source_path.read_text(encoding="utf-8")
    assert '"yolo"' not in source, "224-V-D: the session bypass must not ride the phone"


# --- 224-V-E ---------------------------------------------------------------


async def test_unavailable_upstream_import_degrades_to_a_named_error(env, monkeypatch):
    device_id, token = await _paired(env)
    monkeypatch.setitem(sys.modules, "hermes_cli.approval_mode", None)
    result = await env.dispatch(
        {"type": "approval_mode", "device_id": device_id, "auth": token}
    )
    assert result["code"] == "approval_mode_unavailable"
