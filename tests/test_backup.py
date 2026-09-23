"""A backup must include committed WAL records and preserve existing files."""

from contextlib import closing
import sqlite3

import pytest

from hackalem.__main__ import main
from hackalem.services.backup import copy_database
from hackalem.storage import initialize_database


def test_online_backup_and_restore_preserve_committed_wal(tmp_path):
    source = tmp_path / "live.sqlite3"
    initialize_database(source)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO app_metadata VALUES ('check', 'committed in WAL')")
        connection.commit()
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        copied = tmp_path / "backups" / "copy.sqlite3"
        result = copy_database(source, copied)
        assert result["integrity"] == "ok"
        with closing(sqlite3.connect(copied)) as backup:
            assert backup.execute("SELECT value FROM app_metadata WHERE key='check'").fetchone()[0] == "committed in WAL"
    restored = tmp_path / "restored" / "hackalem.sqlite3"
    copy_database(copied, restored)
    initialize_database(restored)
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute("SELECT value FROM app_metadata WHERE key='check'").fetchone()[0] == "committed in WAL"
    before = copied.read_bytes()
    with pytest.raises(ValueError, match="уже существует"):
        copy_database(source, copied)
    assert copied.read_bytes() == before


def test_missing_or_foreign_database_never_creates_destination(tmp_path):
    source, destination = tmp_path / "source.sqlite3", tmp_path / "copy.sqlite3"
    with pytest.raises(ValueError, match="не найдена"):
        copy_database(source, destination)
    assert not source.exists() and not destination.exists()
    with closing(sqlite3.connect(source)) as connection:
        connection.execute("CREATE TABLE notes (value TEXT)")
        connection.commit()
    before = source.read_bytes()
    with pytest.raises(ValueError, match="HackAlem"):
        copy_database(source, destination)
    assert source.read_bytes() == before and not destination.exists()


def test_cli_restore_does_not_bootstrap_destination_first(tmp_path, monkeypatch):
    source, data = tmp_path / "backup.sqlite3", tmp_path / "restore"
    initialize_database(source)
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(data))
    assert main(["db-restore", "--input", str(source)]) == 0
    assert (data / "hackalem.sqlite3").is_file()
    copied = tmp_path / "new-backup.sqlite3"
    assert main(["db-backup", "--output", str(copied)]) == 0
    assert main(["db-restore", "--input", str(source)]) == 1
