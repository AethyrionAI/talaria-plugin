"""Connection-lifecycle hygiene (#351-A, hygiene half).

The token-hash database must be 0600 from the moment it exists, a failed
open must close the connection it opened, and the token lookup column is
indexed. Absolute imports on purpose: this file must survive the PR2
package-layout change unedited.
"""

import os
import sqlite3
import stat

import pytest
from talaria import database


def _redirect(monkeypatch, tmp_path):
    monkeypatch.setattr(database, "database_path", lambda: tmp_path / "talaria.db")


class _RecordingConnection:
    """Forwarding wrapper: sqlite3.Connection attributes are C-level, so
    close() can't be instrumented by assignment on the instance."""

    def __init__(self, inner, closed):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_closed", closed)

    def close(self):
        self._closed.append(True)
        object.__getattribute__(self, "_inner").close()

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)


def test_database_file_is_0600_from_creation(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    connection = database.connect()
    connection.close()
    mode = stat.S_IMODE(os.stat(tmp_path / "talaria.db").st_mode)
    assert mode == 0o600


def test_failed_open_closes_the_connection_and_leaves_0600(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    db = tmp_path / "talaria.db"
    db.write_bytes(b"definitely not a sqlite file")
    os.chmod(db, 0o600)
    closed = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        return _RecordingConnection(real_connect(*args, **kwargs), closed)

    monkeypatch.setattr(database.sqlite3, "connect", recording_connect)
    with pytest.raises(sqlite3.DatabaseError):
        database._open(database.database_path())
    assert closed == [True]
    assert stat.S_IMODE(os.stat(db).st_mode) == 0o600


def test_readonly_probe_never_creates_the_database(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    assert database.try_connect_readonly() is None
    assert not (tmp_path / "talaria.db").exists()


def test_token_index_exists(monkeypatch, tmp_path):
    _redirect(monkeypatch, tmp_path)
    connection = database.connect()
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    finally:
        connection.close()
    assert "devices_token_idx" in names
