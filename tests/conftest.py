import pytest


@pytest.fixture(autouse=True)
def isolate_hermes_home(monkeypatch, tmp_path):
    """Keep every test away from the operator's real profile state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
