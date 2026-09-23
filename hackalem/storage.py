"""Safe initialization and the additive migration from stage 1 metadata."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hackalem.import_schema import MIGRATION_3, SCHEMA_SQL, schema_signature
from hackalem.quality_schema import MIGRATION_4
from hackalem.cleaning_schema import MIGRATION_5

SCHEMA_VERSION = 5


class UnsupportedSchemaError(RuntimeError):
    """A database created by a different schema must not be silently changed."""


@dataclass(frozen=True)
class StorageInfo:
    path: Path
    schema_version: int


def initialize_database(path: Path) -> StorageInfo:
    """Create metadata once and leave existing metadata intact on every rerun."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3, 4, SCHEMA_VERSION):
            raise UnsupportedSchemaError(
                f"Версия базы {version} не поддерживается; ожидается {SCHEMA_VERSION}. "
                "Существующая база не изменена."
            )
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if version == 0 and objects:
            raise UnsupportedSchemaError(
                "Выбранная база уже содержит неизвестные данные. "
                "Выберите отдельную папку хранилища; база не изменена."
            )
        if version in (1, 2, 3, 4, SCHEMA_VERSION):
            columns = [
                (row[1], row[2].upper(), row[3], row[5])
                for row in connection.execute("PRAGMA table_info(app_metadata)")
            ]
            if columns != [("key", "TEXT", 0, 1), ("value", "TEXT", 1, 0)]:
                raise UnsupportedSchemaError(
                    "Структура существующей базы не распознана. База не изменена."
                )
            if not connection.execute(
                "SELECT 1 FROM app_metadata WHERE key='created_at_utc'"
            ).fetchone():
                raise UnsupportedSchemaError(
                    "В базе отсутствуют метаданные приложения. База не изменена."
                )
        if version in (2, 3, 4, SCHEMA_VERSION):
            with closing(sqlite3.connect(":memory:")) as expected:
                for statement in SCHEMA_SQL:
                    expected.execute(statement)
                if version >= 3:
                    for statement in MIGRATION_3:
                        expected.execute(statement)
                if version >= 4:
                    for statement in MIGRATION_4:
                        expected.execute(statement)
                if version >= 5:
                    for statement in MIGRATION_5:
                        expected.execute(statement)
                if schema_signature(connection, version) != schema_signature(expected, version):
                    raise UnsupportedSchemaError(
                        "Структура импортов не распознана. База не изменена."
                    )
            if version == SCHEMA_VERSION:
                return StorageInfo(path=path, schema_version=SCHEMA_VERSION)
        new_statements = ((SCHEMA_SQL if version < 2 else [])
                          + (MIGRATION_3 if version < 3 else [])
                          + (MIGRATION_4 if version < 4 else []) + MIGRATION_5)
        if version in (1, 2, 3, 4):
            reserved = {statement.split()[2] for statement in new_statements if statement.startswith("CREATE")}
            if reserved.intersection(name for (name,) in objects):
                raise UnsupportedSchemaError(
                    "Имена таблиц новой схемы заняты. База не изменена."
                )
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS app_metadata ("
                "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_metadata(key, value) VALUES (?, ?)",
                ("created_at_utc", datetime.now(UTC).isoformat()),
            )
            for statement in new_statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return StorageInfo(path=path, schema_version=SCHEMA_VERSION)
