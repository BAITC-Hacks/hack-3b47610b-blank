"""Initialize the local application without loading business data."""

from dataclasses import dataclass

from hackalem.config import Settings
from hackalem.storage import StorageInfo, initialize_database


@dataclass(frozen=True)
class ApplicationState:
    settings: Settings
    storage: StorageInfo
    source_directory_exists: bool


def bootstrap(settings: Settings) -> ApplicationState:
    storage = initialize_database(settings.database_path)
    return ApplicationState(
        settings=settings,
        storage=storage,
        source_directory_exists=settings.source_dir.is_dir(),
    )
