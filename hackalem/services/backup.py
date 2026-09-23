"""Consistent SQLite copies, including committed WAL data, without overwrite."""

from contextlib import closing
from pathlib import Path
import sqlite3

from hackalem.config import PROJECT_ROOT, load_settings
from hackalem.storage import SCHEMA_VERSION


def copy_database(source, destination):
    """Back up or restore to a new file; never migrate or overwrite the source."""
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_file():
        raise ValueError("Исходная база не найдена.")
    if destination.suffix != ".sqlite3":
        raise ValueError("Копия базы должна иметь расширение .sqlite3.")
    for root in (PROJECT_ROOT, load_settings().source_dir):
        for folder in ("IEK", "Systeme electric"):
            if destination.is_relative_to((root / folder).resolve()):
                raise ValueError("Нельзя сохранять базу в папку исходных файлов.")
    if destination.exists():
        raise ValueError("Целевая база уже существует; выберите новую папку или имя. Данные сохранены.")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
        version = original.execute("PRAGMA user_version").fetchone()[0]
        metadata = original.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_metadata'"
        ).fetchone()
        if not metadata or not 1 <= version <= SCHEMA_VERSION:
            raise ValueError("Источник не является поддерживаемой базой HackAlem.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation closes the check/create race; failure never removes
        # a destination belonging to another process or the user.
        with destination.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(destination)) as backup:
                original.backup(backup)
                integrity = backup.execute("PRAGMA integrity_check").fetchall()
                if integrity != [("ok",)] or backup.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("Копия не прошла проверку целостности SQLite.")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return {"source": str(source), "database_path": str(destination),
            "schema_version": version, "integrity": "ok", "bytes": destination.stat().st_size}
