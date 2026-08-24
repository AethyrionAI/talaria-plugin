"""#308's compatibility floor (Talaria-27 tracker, bars 308-FLOOR-A..E).

The floor stack ruled 2026-08-23: ``manifest_version: 1`` in plugin.yaml,
a loud fail-soft load-time check in ``register()``, and a README
tested-against SHA held in lockstep with the CI pin by the structural
test below. The floor claims only what was measured: the oldest host
version the plugin is live-verified on.
"""

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

FLOOR_TOKEN = "[talaria] COMPATIBILITY FLOOR"


def _read(relpath: str) -> str:
    path = REPO_ROOT / relpath
    assert path.is_file(), f"{relpath} missing — the floor stack requires it"
    return path.read_text(encoding="utf-8")


class _RecordingCtx:
    """Accepts every ctx.register_* call register() makes."""

    def __getattr__(self, name):
        def _accept(*args, **kwargs):
            return None

        return _accept


# --- 308-FLOOR-A -----------------------------------------------------------


def test_manifest_declares_manifest_version_1():
    manifest = yaml.safe_load(_read("plugin.yaml"))
    assert manifest.get("manifest_version") == 1, (
        "plugin.yaml must declare manifest_version: 1 — the 0.20.5 installer "
        "accepts it and refuses only greater, which is the forward guard"
    )


# --- 308-FLOOR-B -----------------------------------------------------------


def test_below_floor_version_warns_loudly_and_does_not_raise(capsys):
    from talaria import compat

    returned = compat.check_hermes_floor("0.19.0")
    out = capsys.readouterr().out
    assert FLOOR_TOKEN in out
    assert returned is not None, "the check reports the warning it emitted"


# --- 308-FLOOR-C -----------------------------------------------------------


def test_at_floor_and_above_floor_are_silent(capsys):
    from talaria import compat

    floor_str = ".".join(str(part) for part in compat.HERMES_FLOOR)
    assert compat.check_hermes_floor(floor_str) is None
    assert compat.check_hermes_floor("99.0.0") is None
    assert FLOOR_TOKEN not in capsys.readouterr().out


def test_unparseable_version_skips_and_never_raises(capsys):
    from talaria import compat

    assert compat.check_hermes_floor("not-a-version") is None
    assert "version check skipped" in capsys.readouterr().out


def test_reading_the_real_version_never_raises():
    from talaria import compat

    # In this venv hermes_cli is importable, so the read succeeds; on a
    # box where it is not, the same call must skip, not raise. Either way
    # the call is total.
    compat.check_hermes_floor()


# --- register() integration (mutation target for 308-FLOOR-E) --------------


def test_register_consults_the_compat_floor(monkeypatch, tmp_path):
    import talaria
    from talaria import compat, database

    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "t.db")
    calls = []
    monkeypatch.setattr(
        compat, "check_hermes_floor", lambda *a, **k: calls.append(1)
    )
    talaria.register(_RecordingCtx())
    assert calls, "register() no longer consults the compat floor"


# --- 308-FLOOR-D -----------------------------------------------------------

_SHA_RE = re.compile(r"\b[0-9a-f]{40}\b")


def test_ci_pin_and_readme_tested_against_share_one_sha():
    ci = _read(".github/workflows/ci.yml")
    readme = _read("README.md")

    tested_lines = [
        line for line in readme.splitlines() if "Tested against" in line
    ]
    assert len(tested_lines) == 1, (
        "README.md must carry exactly one 'Tested against' line"
    )
    tested_shas = _SHA_RE.findall(tested_lines[0])
    assert len(tested_shas) == 1, (
        "the tested-against line must name one full 40-char hermes-agent SHA"
    )

    assert tested_shas[0] in ci, (
        "CI must clone hermes-agent at the exact SHA the README claims was "
        "tested — the two files move together or the claim is anecdotal"
    )
    assert (
        "clone --depth 1 https://github.com/NousResearch/hermes-agent.git"
        not in ci
    ), "CI still clones floating HEAD — the pin is the whole point"
