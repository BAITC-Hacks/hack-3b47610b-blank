"""Runtime paths. Configuration reads never modify source documents."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    source_dir: Path
    data_dir: Path

    @property
    def database_path(self) -> Path:
        return self.data_dir / "hackalem.sqlite3"


def _resolve_path(value: str, root: Path) -> Path:
    candidate = Path(value).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def load_settings(
    environ: Mapping[str, str] | None = None,
    project_root: Path = PROJECT_ROOT,
) -> Settings:
    """Resolve relative overrides against the project, not the working directory."""
    env = os.environ if environ is None else environ
    root = project_root.resolve()
    paths = {}
    for field, key, default in (
        ("source_dir", "HACKALEM_SOURCE_DIR", str(root)),
        ("data_dir", "HACKALEM_DATA_DIR", str(root / ".local")),
    ):
        value = env.get(key, default).strip()
        if not value:
            raise ValueError(f"Настройка {key} не должна быть пустой.")
        paths[field] = _resolve_path(value, root)
    settings = Settings(**paths)
    if settings.source_dir.exists() and not settings.source_dir.is_dir():
        raise ValueError("Путь к исходным данным должен указывать на папку.")
    # Runtime files belong in a separate folder, never in supplier source folders.
    for source_root in {root, settings.source_dir}:
        if settings.data_dir == source_root:
            raise ValueError("Выберите отдельную папку для локального хранилища.")
        for supplier in ("IEK", "Systeme electric"):
            protected = (source_root / supplier).resolve()
            if settings.data_dir.is_relative_to(protected):
                raise ValueError("Хранилище нельзя размещать в папке поставщика.")
    return settings
