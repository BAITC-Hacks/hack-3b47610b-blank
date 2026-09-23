import sqlite3
from contextlib import closing

import pytest

from hackalem.config import load_settings
from hackalem.services.bootstrap import bootstrap
from hackalem.storage import SCHEMA_VERSION, UnsupportedSchemaError, initialize_database


def test_paths_are_relative_to_project_and_sources_are_untouched(tmp_path):
    sources = tmp_path / "reports"
    sources.mkdir()
    original = sources / "untouched.xlsx"
    original.write_bytes(b"source fixture")
    settings = load_settings(
        {"HACKALEM_SOURCE_DIR": "reports", "HACKALEM_DATA_DIR": "runtime"},
        project_root=tmp_path,
    )
    state = bootstrap(settings)
    assert settings.source_dir == sources
    assert state.storage.path == tmp_path / "runtime" / "hackalem.sqlite3"
    assert state.source_directory_exists
    assert original.read_bytes() == b"source fixture"
    assert list(sources.iterdir()) == [original]


def test_database_bootstrap_preserves_existing_metadata(tmp_path):
    path = tmp_path / "db" / "test.sqlite3"
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "INSERT INTO app_metadata(key, value) VALUES ('keep', 'original')"
        )
        connection.commit()
        created = connection.execute(
            "SELECT value FROM app_metadata WHERE key='created_at_utc'"
        ).fetchone()[0]
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert dict(connection.execute("SELECT key, value FROM app_metadata")) == {
            "created_at_utc": created, "keep": "original",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_future_schema_is_not_overwritten(tmp_path):
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA user_version = 99")
    with pytest.raises(UnsupportedSchemaError):
        initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall() == []


@pytest.mark.parametrize("version", [0, 1])
def test_unrelated_database_is_never_adopted(tmp_path, version):
    path = tmp_path / "hackalem.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE notes (value TEXT)")
        connection.execute("INSERT INTO notes VALUES ('user data')")
        connection.execute(f"PRAGMA user_version = {version}")
        connection.commit()
    original = path.read_bytes()
    with pytest.raises(UnsupportedSchemaError):
        initialize_database(path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("folder", ["IEK", "Systeme electric/nested"])
def test_runtime_cannot_be_written_inside_supplier_sources(tmp_path, folder):
    with pytest.raises(ValueError, match="папке поставщика"):
        load_settings({"HACKALEM_DATA_DIR": folder}, project_root=tmp_path)


def test_blank_environment_value_is_not_silently_defaulted(tmp_path):
    with pytest.raises(ValueError, match="не должна быть пустой"):
        load_settings({"HACKALEM_DATA_DIR": " "}, project_root=tmp_path)
