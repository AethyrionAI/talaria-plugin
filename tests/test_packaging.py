import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    path = ROOT / "pyproject.toml"
    assert path.is_file(), "pyproject.toml must define installable package metadata"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_distribution_metadata_matches_directory_plugin_manifest():
    project = _pyproject()["project"]
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text(encoding="utf-8"))

    assert project["name"] == "talaria-hermes-plugin"
    assert project["version"] == manifest["version"]
    assert project["requires-python"] == ">=3.11"
    assert project["entry-points"]["hermes_agent.plugins"] == {"talaria": "talaria"}


def test_setuptools_uses_the_conventional_talaria_source_package():
    setuptools = _pyproject()["tool"]["setuptools"]

    assert setuptools["packages"] == ["talaria"]
    assert "package-dir" not in setuptools
    assert "package-data" not in setuptools
    assert (ROOT / "talaria" / "__init__.py").is_file()
