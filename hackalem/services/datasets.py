"""Dataset identity shared by readers, guards and the UI; no oracle access."""

from contextlib import closing
from pathlib import Path
import sqlite3


def dataset_context(database_path):
    path = Path(database_path)
    metadata = {}
    if path.exists():
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='app_metadata' AND type='table'").fetchone():
                metadata = dict(connection.execute("SELECT key, value FROM app_metadata"))
    kind = metadata.get('dataset_kind', 'real')
    if kind not in ('real', 'synthetic'):
        raise ValueError('Неизвестный тип набора данных.')
    return {
        'kind': kind,
        'dataset_id': metadata.get('dataset_id', 'real'),
        'label': 'СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР' if kind == 'synthetic' else 'Реальные исходные отчёты',
    }


def require_real_dataset(database_path):
    if dataset_context(database_path)['kind'] != 'real':
        raise ValueError('Импорт реальных файлов в синтетический dataset запрещён. Выберите отдельную рабочую базу.')
