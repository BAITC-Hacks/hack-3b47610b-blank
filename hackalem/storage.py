"""Safe initialization and additive migrations for all implemented stages."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from hackalem.cleaning_schema import MIGRATION_5
from hackalem.forecast_schema import LEGACY_MIGRATION_6, LINK_LOST_DEMAND, MIGRATION_7
from hackalem.import_schema import MIGRATION_3, SCHEMA_SQL
from hackalem.lost_demand_schema import MIGRATION_6
from hackalem.orders_schema import MIGRATION_9
from hackalem.quality_schema import MIGRATION_4
from hackalem.replenishment_schema import MIGRATION_8

SCHEMA_VERSION = 9


class UnsupportedSchemaError(RuntimeError):
    """A database created by a different schema must not be silently changed."""


@dataclass(frozen=True)
class StorageInfo:
    path: Path
    schema_version: int


def _tables(statements):
    return [statement.split()[2] for statement in statements if statement.startswith("CREATE TABLE")]


def _signature(connection, statements):
    return {table: connection.execute(f"PRAGMA table_info({table})").fetchall()
            for table in _tables(statements)}


def _expected_statements(version, names):
    statements = list(SCHEMA_SQL)
    if version >= 3:
        statements += MIGRATION_3
    if version >= 4:
        statements += MIGRATION_4
    if version >= 5:
        statements += MIGRATION_5
    if version == 6 and "forecast_runs" in names and "lost_demand_runs" not in names:
        return statements + LEGACY_MIGRATION_6
    if version >= 6:
        statements += MIGRATION_6
    if version == 7 and "replenishment_runs" in names and "forecast_runs" not in names:
        return statements + MIGRATION_8
    if version >= 7:
        statements += MIGRATION_7
    if version >= 8:
        statements += MIGRATION_8
    if version >= 9:
        statements += MIGRATION_9
    return statements


def _pending_statements(version, names):
    if version < 2:
        return (SCHEMA_SQL + MIGRATION_3 + MIGRATION_4 + MIGRATION_5 +
                MIGRATION_6 + MIGRATION_7 + MIGRATION_8 + MIGRATION_9)
    statements = []
    if version < 3:
        statements += MIGRATION_3
    if version < 4:
        statements += MIGRATION_4
    if version < 5:
        statements += MIGRATION_5
    legacy_forecast = version == 6 and "forecast_runs" in names and "lost_demand_runs" not in names
    legacy_replenishment = (version == 7 and "replenishment_runs" in names and
                            "forecast_runs" not in names)
    if version < 6 or legacy_forecast:
        statements += MIGRATION_6
    if version < 7:
        statements += LINK_LOST_DEMAND if legacy_forecast else MIGRATION_7
    elif legacy_replenishment:
        statements += MIGRATION_7
    if version < 8 and not legacy_replenishment:
        statements += MIGRATION_8
    if version < 9:
        statements += MIGRATION_9
    return statements


def initialize_database(path: Path) -> StorageInfo:
    """Create metadata once and migrate known variants without losing facts."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in range(SCHEMA_VERSION + 1):
            raise UnsupportedSchemaError(
                f"Версия базы {version} не поддерживается; ожидается {SCHEMA_VERSION}. "
                "Существующая база не изменена."
            )
        objects = connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        names = {name for (name,) in objects}
        if version == 0 and objects:
            raise UnsupportedSchemaError(
                "Выбранная база уже содержит неизвестные данные. "
                "Выберите отдельную папку хранилища; база не изменена."
            )
        if version >= 1:
            columns = [(row[1], row[2].upper(), row[3], row[5])
                       for row in connection.execute("PRAGMA table_info(app_metadata)")]
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
        if version >= 2:
            expected_statements = _expected_statements(version, names)
            with closing(sqlite3.connect(":memory:")) as expected:
                for statement in expected_statements:
                    expected.execute(statement)
                if _signature(connection, expected_statements) != _signature(expected, expected_statements):
                    raise UnsupportedSchemaError(
                        "Структура импортов не распознана. База не изменена."
                    )
            if version == SCHEMA_VERSION:
                return StorageInfo(path=path, schema_version=SCHEMA_VERSION)
        new_statements = _pending_statements(version, names)
        if version >= 1:
            reserved = set(_tables(new_statements))
            if reserved.intersection(names):
                raise UnsupportedSchemaError(
                    "Имена таблиц новой схемы заняты. База не изменена."
                )
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_metadata(key, value) VALUES (?, ?)",
                ("created_at_utc", datetime.now(UTC).isoformat()),
            )
            for statement in new_statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return StorageInfo(path=path, schema_version=SCHEMA_VERSION)
