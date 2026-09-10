"""#309 Lane D — `hermes talaria pair-qr`.

The payload pinned here is a CROSS-REPO CONTRACT: the Talaria iOS app's
QR scanner (tracker #309 Lane B) pins the same fixture bytes,
`tests/fixtures/pair_payload.json`. Changing the shape means changing
both repositories in lockstep, so this file asserts the shape
deliberately rather than incidentally.
"""

from __future__ import annotations

import io
import json
import logging
import pathlib
import socket
from types import SimpleNamespace

import pytest

from talaria import admin, pairing_qr

FIXTURE_PATH = pathlib.Path(__file__).parent / "fixtures" / "pair_payload.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The payload contract
# --------------------------------------------------------------------------

def test_payload_matches_the_pinned_cross_repo_fixture():
    """The app scanner pins these exact bytes. Key ORDER is irrelevant;
    the key SET and the value types are not."""
    built = pairing_qr.build_payload(
        gateway_url=FIXTURE["gateway"],
        api_key=FIXTURE["key"],
        name=FIXTURE["name"],
    )
    assert built == FIXTURE


def test_payload_version_field_is_mandatory_and_is_the_integer_one():
    built = pairing_qr.build_payload(gateway_url="http://h:8642", api_key="k", name="n")
    assert "talaria" in built
    assert built["talaria"] == pairing_qr.PAYLOAD_VERSION == 1
    assert isinstance(built["talaria"], int) and not isinstance(built["talaria"], bool)


def test_payload_carries_exactly_the_four_contract_keys():
    built = pairing_qr.build_payload(gateway_url="http://h:8642", api_key="k", name="n")
    assert set(built) == {"talaria", "gateway", "key", "name"}


def test_payload_refuses_to_build_without_a_key():
    """A QR that pairs nothing is worse than a named failure."""
    with pytest.raises(pairing_qr.PairingQRError):
        pairing_qr.build_payload(gateway_url="http://h:8642", api_key="", name="n")


def test_payload_refuses_a_gateway_url_with_no_scheme():
    with pytest.raises(pairing_qr.PairingQRError):
        pairing_qr.build_payload(gateway_url="100.64.0.20:8642", api_key="k", name="n")


def test_encode_payload_round_trips_and_is_compact():
    text = pairing_qr.encode_payload(FIXTURE)
    assert json.loads(text) == FIXTURE
    assert ", " not in text and '": ' not in text     # compact separators


# --------------------------------------------------------------------------
# Credential + address resolution (read at print time, never stored)
# --------------------------------------------------------------------------

def test_api_key_prefers_the_hermes_dotenv_read(monkeypatch):
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: "from-dotenv")
    monkeypatch.setenv("API_SERVER_KEY", "from-shell")
    assert pairing_qr.resolve_api_key() == "from-dotenv"


def test_api_key_falls_back_to_the_process_environment(monkeypatch):
    """Same read `platform_adapter._api_key()` uses inside the gateway."""
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.setenv("API_SERVER_KEY", "from-shell")
    assert pairing_qr.resolve_api_key() == "from-shell"


def test_api_key_missing_resolves_empty_rather_than_raising(monkeypatch):
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    assert pairing_qr.resolve_api_key() == ""


def test_gateway_override_wins_and_says_so(monkeypatch):
    url, provenance = pairing_qr.resolve_gateway_url("http://example:9999")
    assert url == "http://example:9999"
    assert "--gateway" in provenance


def test_gateway_override_is_validated_not_trusted():
    with pytest.raises(pairing_qr.PairingQRError):
        pairing_qr.resolve_gateway_url("not a url")


def test_gateway_port_follows_api_server_precedence(monkeypatch):
    monkeypatch.setattr(pairing_qr, "_configured_host", lambda: "10.0.0.5")
    monkeypatch.setenv("API_SERVER_PORT", "9001")
    url, _ = pairing_qr.resolve_gateway_url(None)
    assert url == "http://10.0.0.5:9001"


def test_gateway_port_defaults_to_8642(monkeypatch):
    monkeypatch.setattr(pairing_qr, "_configured_host", lambda: "10.0.0.5")
    monkeypatch.delenv("API_SERVER_PORT", raising=False)
    url, _ = pairing_qr.resolve_gateway_url(None)
    assert url.endswith(":8642")


def test_a_loopback_bind_host_is_never_advertised_to_a_phone(monkeypatch):
    """`API_SERVER_HOST` is a BIND address; 127.0.0.1 and 0.0.0.0 are not
    reachable from the phone, so they must not become the QR's URL."""
    monkeypatch.setattr(pairing_qr, "_tailnet_address", lambda: "100.64.0.20")
    for bind in ("127.0.0.1", "0.0.0.0", "localhost", "::", ""):
        monkeypatch.setattr(pairing_qr, "_configured_host", lambda bind=bind: bind)
        url, provenance = pairing_qr.resolve_gateway_url(None)
        assert url == "http://100.64.0.20:8642", bind
        assert "tailnet" in provenance


def test_no_derivable_address_is_a_named_failure_not_an_invention(monkeypatch):
    monkeypatch.setattr(pairing_qr, "_configured_host", lambda: "0.0.0.0")
    monkeypatch.setattr(pairing_qr, "_tailnet_address", lambda: None)
    with pytest.raises(pairing_qr.PairingQRError) as excinfo:
        pairing_qr.resolve_gateway_url(None)
    assert "--gateway" in str(excinfo.value)


def test_tailnet_probe_only_accepts_a_cgnat_address(monkeypatch):
    """The probe is self-validating: a LAN source address is rejected, so
    a machine that is not on the tailnet cannot yield a bogus URL."""
    monkeypatch.setattr(pairing_qr, "_route_source_address", lambda: "192.168.1.20")
    assert pairing_qr._tailnet_address() is None
    monkeypatch.setattr(pairing_qr, "_route_source_address", lambda: "100.64.0.20")
    assert pairing_qr._tailnet_address() == "100.64.0.20"
    monkeypatch.setattr(pairing_qr, "_route_source_address", lambda: None)
    assert pairing_qr._tailnet_address() is None


def test_host_name_override_wins(monkeypatch):
    assert pairing_qr.resolve_host_name("STUDIO") == "STUDIO"


def test_host_name_strips_the_dns_domain(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "owens-mac-mini.tail5663a6.ts.net")
    assert pairing_qr.resolve_host_name(None) == "owens-mac-mini"


def test_host_name_never_resolves_empty(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "")
    assert pairing_qr.resolve_host_name(None)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def test_render_ansi_is_a_square_of_half_block_rows():
    art = pairing_qr.render_ansi(pairing_qr.encode_payload(FIXTURE), color=False)
    lines = art.splitlines()
    assert lines, "no QR rendered"
    assert all("\x1b" not in line for line in lines), "color=False must emit no escapes"
    assert any("▀" in line or "█" in line for line in lines)


def test_render_ansi_colour_form_emits_escapes_and_resets_every_row():
    art = pairing_qr.render_ansi(pairing_qr.encode_payload(FIXTURE), color=True)
    lines = [line for line in art.splitlines() if line]
    assert lines
    for line in lines:
        assert line.startswith("\x1b["), "each row must set its own colours"
        assert line.endswith("\x1b[0m"), "each row must reset, or the terminal bleeds"


def test_the_module_grid_is_a_real_qr_symbol():
    """Structural, not decorative: a square grid carrying the mandatory
    top-left finder pattern is what a decoder locks onto first."""
    text = pairing_qr.encode_payload(FIXTURE)
    matrix = pairing_qr.payload_matrix(text)
    assert len(matrix) == len(matrix[0]) >= 21
    # Finder pattern: a 7x7 dark ring with a light second ring inside it.
    assert all(matrix[0][index] for index in range(7))
    assert all(matrix[index][0] for index in range(7))
    assert all(matrix[6][index] for index in range(7))
    assert not matrix[1][1]
    assert matrix[2][2] and matrix[3][3] and matrix[4][4]


def test_a_longer_payload_grows_the_symbol_rather_than_truncating():
    small = pairing_qr.payload_matrix(pairing_qr.encode_payload(FIXTURE))
    big_payload = dict(FIXTURE, name="x" * 300)
    big = pairing_qr.payload_matrix(pairing_qr.encode_payload(big_payload))
    assert len(big) > len(small)


def test_render_ansi_paints_a_quiet_zone_by_default():
    """`render_ansi()` with no arguments is what the CLI calls. A code drawn
    flush to the terminal background does not scan on a dark theme, so the
    default border is load-bearing, not cosmetic."""
    lines = pairing_qr.render_ansi(pairing_qr.encode_payload(FIXTURE), color=False).splitlines()
    assert lines[0].strip() == "" and lines[1].strip() == ""
    assert lines[-1].strip() == ""
    assert all(line.startswith("    ") for line in lines if line.strip())


def test_the_quiet_zone_is_painted_not_inherited():
    """A QR whose margin is the terminal's own background does not scan on
    a dark theme. The border rows must be present in the rendered art."""
    bare = pairing_qr.payload_matrix(pairing_qr.encode_payload(FIXTURE), border=0)
    bordered = pairing_qr.payload_matrix(pairing_qr.encode_payload(FIXTURE), border=4)
    assert len(bordered) == len(bare) + 8
    assert not any(bordered[0])                 # top quiet-zone row is all light


def _unpack_half_blocks(art: str) -> list[list[bool]]:
    """Invert the half-block packing: two module rows per text line."""
    inverse = {"█": (True, True), "▀": (True, False), "▄": (False, True), " ": (False, False)}
    rows: list[list[bool]] = []
    for line in art.splitlines():
        top, bottom = [], []
        for char in line:
            upper, lower = inverse[char]
            top.append(upper)
            bottom.append(lower)
        rows.append(top)
        rows.append(bottom)
    return rows


def test_the_terminal_art_is_the_same_symbol_the_matrix_describes():
    """Half-block packing folds two module rows into one text line — an
    off-by-one there silently draws a DIFFERENT code that still looks like
    a QR. Unpack the art and compare it to the grid module for module."""
    text = pairing_qr.encode_payload(FIXTURE)
    expected = pairing_qr.payload_matrix(text, border=4)
    unpacked = _unpack_half_blocks(pairing_qr.render_ansi(text, color=False))
    assert unpacked[: len(expected)] == expected
    # Any trailing padding row must be quiet zone, never truncated modules.
    assert not any(any(row) for row in unpacked[len(expected):])


def test_the_png_and_the_terminal_carry_the_same_symbol(tmp_path):
    """Both arms must encode one payload identically — otherwise `--png`
    is a second code path that can drift from the one people scan."""
    text = pairing_qr.encode_payload(FIXTURE)
    qrcode = pytest.importorskip("qrcode")
    from qrcode.image.pure import PyPNGImage

    code = qrcode.QRCode(
        border=4, box_size=8,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        image_factory=PyPNGImage,
    )
    code.add_data(text)
    code.make(fit=True)
    png_matrix = [[bool(cell) for cell in row] for row in code.get_matrix()]
    assert png_matrix == pairing_qr.payload_matrix(text, border=4)


def test_png_export_writes_a_real_png(tmp_path):
    target = tmp_path / "pair.png"
    written = pairing_qr.write_png(pairing_qr.encode_payload(FIXTURE), target)
    assert written == target
    assert target.exists() and target.stat().st_size > 0
    assert target.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_missing_qrcode_dependency_is_an_actionable_message(monkeypatch):
    monkeypatch.setattr(pairing_qr, "_import_qrcode", lambda: None)
    with pytest.raises(pairing_qr.PairingQRError) as excinfo:
        pairing_qr.render_ansi("anything")
    message = str(excinfo.value)
    assert "qrcode" in message
    assert "pip install" in message


# --------------------------------------------------------------------------
# The CLI arm
# --------------------------------------------------------------------------

def _pair_qr_args(**overrides):
    args = {
        "talaria_cmd": "pair-qr",
        "gateway": None,
        "name": None,
        "png": None,
        "no_color": True,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


def test_pair_qr_prints_a_qr_and_never_the_key(monkeypatch, capsys):
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.setenv("API_SERVER_KEY", FIXTURE["key"])
    result = admin.handle_cli(_pair_qr_args(gateway=FIXTURE["gateway"], name=FIXTURE["name"]))
    out = capsys.readouterr().out
    assert result == 0
    assert FIXTURE["key"] not in out, "the key must live in the QR, never in the prose"
    assert FIXTURE["gateway"] in out                       # the operator must verify the URL
    assert FIXTURE["name"] in out
    assert "▀" in out or "█" in out              # something square got drawn


def test_pair_qr_without_a_key_fails_loudly_and_draws_nothing(monkeypatch, capsys):
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    result = admin.handle_cli(_pair_qr_args(gateway=FIXTURE["gateway"]))
    out = capsys.readouterr().out
    assert result == 1
    assert "API_SERVER_KEY" in out
    assert "▀" not in out and "█" not in out


def test_pair_qr_writes_no_row_to_the_device_store(monkeypatch, tmp_path, capsys):
    """Nothing persisted — the QR carries the host's existing credential."""
    from talaria import database, store
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.setenv("API_SERVER_KEY", FIXTURE["key"])
    admin.handle_cli(_pair_qr_args(gateway=FIXTURE["gateway"]))
    capsys.readouterr()
    assert store.devices() == []


def test_pair_qr_logs_nothing_at_all(monkeypatch, capsys, caplog):
    """The key must not reach any logger, at any level."""
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.setenv("API_SERVER_KEY", FIXTURE["key"])
    with caplog.at_level(logging.DEBUG, logger="talaria"):
        admin.handle_cli(_pair_qr_args(gateway=FIXTURE["gateway"]))
    capsys.readouterr()
    assert caplog.records == []


def test_pair_qr_masks_the_key_in_the_confirmation_line(monkeypatch, capsys):
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.setenv("API_SERVER_KEY", FIXTURE["key"])
    admin.handle_cli(_pair_qr_args(gateway=FIXTURE["gateway"]))
    out = capsys.readouterr().out
    assert FIXTURE["key"] not in out
    assert FIXTURE["key"][:4] in out and FIXTURE["key"][-4:] in out


def test_pair_qr_png_flag_writes_the_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(pairing_qr, "_dotenv_api_key", lambda: None)
    monkeypatch.setenv("API_SERVER_KEY", FIXTURE["key"])
    target = tmp_path / "pair.png"
    result = admin.handle_cli(
        _pair_qr_args(gateway=FIXTURE["gateway"], png=str(target))
    )
    out = capsys.readouterr().out
    assert result == 0
    assert target.exists()
    assert str(target) in out


def test_pair_qr_parses_from_the_real_parser():
    import argparse
    parser = argparse.ArgumentParser()
    admin.setup_cli(parser)
    parsed = parser.parse_args(["pair-qr", "--gateway", "http://h:8642", "--png", "/tmp/x.png"])
    assert parsed.talaria_cmd == "pair-qr"
    assert parsed.gateway == "http://h:8642"
    assert parsed.png == "/tmp/x.png"


def test_pair_qr_help_appears_in_the_registered_command(monkeypatch):
    captured = {}
    admin.register_cli(SimpleNamespace(register_cli_command=lambda **kw: captured.update(kw)))
    assert "pair-qr" in captured["help"]
