"""#270 v0 — the dashboard backend, tested the way production loads it:
``importlib`` by file path with no package context, exactly what
``web_server._mount_plugin_api_routes()`` does. Offline throughout — the
probe arms run against a loopback HTTP server and a refused port, never a
live gateway.
"""

from __future__ import annotations

import http.server
import importlib.util
import socket
import threading
from pathlib import Path

import pytest

_DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"


@pytest.fixture(scope="module")
def plugin_api():
    spec = importlib.util.spec_from_file_location(
        "talaria_dashboard_plugin_api_under_test", _DASHBOARD / "plugin_api.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_module_exports_router_by_that_name(plugin_api):
    # web_server.py:17510 does getattr(mod, "router", None) — the attribute
    # NAME is the contract.
    from fastapi import APIRouter

    assert isinstance(plugin_api.router, APIRouter)


def test_device_projection_is_the_closed_field_list(plugin_api):
    # 270-E: token_sha256 above all, but ALSO everything else outside the
    # list — a projection that merely deletes known-bad keys rots the first
    # time the store grows a new column.
    rows = [
        {
            "id": "dev1",
            "name": "whoGoesThere",
            "active": True,
            "last_seen": "2026-08-16T20:00:00+00:00",
            "token_sha256": "deadbeef",
            "install_id": "install-1",
            "created": "2026-08-01T00:00:00+00:00",
            "deactivated": None,
            "some_future_column": "surprise",
        }
    ]
    projected = plugin_api.device_projection(rows)
    assert projected == [
        {
            "id": "dev1",
            "name": "whoGoesThere",
            "active": True,
            "last_seen": "2026-08-16T20:00:00+00:00",
        }
    ]


def test_probe_classification_is_the_269A_table(plugin_api):
    assert plugin_api.classify_probe_status(401) == "live"
    assert plugin_api.classify_probe_status(503) == "absent"
    # Any other status licenses nothing.
    assert plugin_api.classify_probe_status(200) == "indeterminate"
    assert plugin_api.classify_probe_status(418) == "indeterminate"


def _serve_status(status_code: int):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — stdlib naming
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):  # keep pytest output clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [(401, "live"), (503, "absent"), (200, "indeterminate")],
)
def test_probe_against_loopback_server(plugin_api, status_code, expected):
    server = _serve_status(status_code)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/api/platforms/talaria/events"
        result = plugin_api.probe_gateway_adapter(url)
        assert result["observation"] == expected
        assert result["status"] == status_code
    finally:
        server.shutdown()


def test_probe_against_refused_port_is_unreachable(plugin_api):
    # Reserve a port and close it so nothing listens there.
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        dead_port = probe_socket.getsockname()[1]
    result = plugin_api.probe_gateway_adapter(
        f"http://127.0.0.1:{dead_port}/api/platforms/talaria/events"
    )
    assert result["observation"] == "unreachable"
    assert result["status"] is None


def _counting_probe(plugin_api, monkeypatch, result_by_url):
    calls = []

    def fake_probe_once(target):
        calls.append(target)
        return dict(result_by_url[target])

    monkeypatch.setattr(plugin_api, "_probe_once", fake_probe_once)
    monkeypatch.setattr(plugin_api, "_probe_cache", {}, raising=False)
    return calls


def test_probe_cache_serves_verdicts_for_sixty_seconds(plugin_api, monkeypatch):
    # #362 3D-F: the pane polls /status every 5 s; without this floor the
    # probe writes ~17k 401 access-log lines/day into agent.log.
    url = "http://127.0.0.1:9/x"
    calls = _counting_probe(
        plugin_api, monkeypatch, {url: {"observation": "live", "status": 401, "url": url}}
    )
    clock = iter([0.0, 5.0, 59.9, 60.1]).__next__
    first = plugin_api.probe_gateway_adapter(url, now=clock)
    second = plugin_api.probe_gateway_adapter(url, now=clock)
    third = plugin_api.probe_gateway_adapter(url, now=clock)
    assert first == second == third
    assert len(calls) == 1  # 5.0 and 59.9 both served from cache
    plugin_api.probe_gateway_adapter(url, now=clock)
    assert len(calls) == 2  # 60.1 re-probed


def test_probe_cache_retries_failures_after_ten_seconds(plugin_api, monkeypatch):
    # A dead-gateway verdict must not stick for a full minute after the
    # gateway comes back — failures get the short floor.
    url = "http://127.0.0.1:9/x"
    results = {url: {"observation": "unreachable", "status": None, "url": url}}
    calls = _counting_probe(plugin_api, monkeypatch, results)
    clock = iter([0.0, 9.9, 10.1]).__next__
    plugin_api.probe_gateway_adapter(url, now=clock)
    plugin_api.probe_gateway_adapter(url, now=clock)
    assert len(calls) == 1  # 9.9 cached
    results[url] = {"observation": "live", "status": 401, "url": url}
    recovered = plugin_api.probe_gateway_adapter(url, now=clock)
    assert len(calls) == 2  # 10.1 re-probed
    assert recovered["observation"] == "live"


def test_probe_cache_is_keyed_per_url(plugin_api, monkeypatch):
    url_a = "http://127.0.0.1:9/a"
    url_b = "http://127.0.0.1:9/b"
    calls = _counting_probe(
        plugin_api,
        monkeypatch,
        {
            url_a: {"observation": "live", "status": 401, "url": url_a},
            url_b: {"observation": "absent", "status": 503, "url": url_b},
        },
    )
    clock = iter([0.0, 1.0]).__next__
    a = plugin_api.probe_gateway_adapter(url_a, now=clock)
    b = plugin_api.probe_gateway_adapter(url_b, now=clock)
    assert (a["observation"], b["observation"]) == ("live", "absent")
    assert calls == [url_a, url_b]  # b was not served a's cache


def test_plugin_version_reads_the_yaml(plugin_api):
    assert plugin_api._plugin_version() == "0.8.0"


def test_manifest_name_discipline():
    # #263(a) route (1): manifest name, plugin.yaml name, and the desktop
    # plugin id must all read `talaria`. The desktop id lives in
    # desktop-plugin/plugin.js (`id: ID` with ID = 'talaria') — pinned by
    # string match since the ESM file has no Python importer.
    import json

    manifest = json.loads((_DASHBOARD / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "talaria"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["tab"]["hidden"] is True

    plugin_yaml = (_DASHBOARD.parent / "plugin.yaml").read_text(encoding="utf-8")
    assert "name: talaria" in plugin_yaml

    plugin_js = (_DASHBOARD.parent / "desktop-plugin" / "plugin.js").read_text(encoding="utf-8")
    assert "const ID = 'talaria'" in plugin_js
