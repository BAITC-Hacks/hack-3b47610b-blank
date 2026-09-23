import sqlite3
from contextlib import closing

import pytest

from hackalem.import_schema import SCHEMA_SQL
from hackalem.services.imports import read_records, report_snapshot
from hackalem.services.units import confirm_unit_conversion, convert_quantity
from hackalem.storage import SCHEMA_VERSION, initialize_database


def test_schema_two_migration_keeps_existing_systeme_snapshot_and_metadata(tmp_path):
    path = tmp_path / "v2.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO app_metadata VALUES (?, ?)", [("created_at_utc", "old"), ("user-note", "keep")])
        for statement in SCHEMA_SQL:
            connection.execute(statement)
        connection.execute("INSERT INTO import_files VALUES (42, 'transactions', 'Systeme Electric', 'old-path', 'old.xlsx', 'old-hash', 'old-date', NULL, 'systeme-1', 'parser-hash')")
        connection.execute("INSERT INTO import_sheets VALUES (42, 'Лист', 'visible', 'A1:H2', 2, 8)")
        connection.execute("INSERT INTO source_rows VALUES (42, 'Лист', 2, '{}')")
        connection.execute("INSERT INTO transactions VALUES (42, 'Лист', 2, '0007_', '2026-09-22', '001', 'sale', 'шт', 'Алматы', 7, 'value', 'H2')")
        connection.execute("INSERT INTO snapshots VALUES (5, 'fingerprint', 'old-date', 'old-code', '{}', 'old-parameters', '{}')")
        connection.execute("INSERT INTO snapshot_files VALUES (5, 'transactions', 42)")
        connection.execute("PRAGMA user_version=2")
    initialize_database(path)
    initialize_database(path)
    assert read_records(path, 5, "transactions", "transactions")[0]["quantity"] == 7
    report = report_snapshot(path, 5)
    assert report["supplier"] == "Systeme Electric"
    assert report["versions"]["code_version"] == "old-code"
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert dict(connection.execute("SELECT * FROM app_metadata")) == {"created_at_utc": "old", "user-note": "keep"}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.fixture
def unit_database(tmp_path):
    path = tmp_path / "units.sqlite3"
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        for sid in (1, 2):
            connection.execute("INSERT INTO snapshots VALUES (?, ?, 'now', 'code', '{}', 'params', '{}', 'IEK')", (sid, str(sid)))
            for sku in ("A", "B"):
                connection.execute("INSERT INTO unit_assessments VALUES (?, ?, 'шт', 'компл', 4, 'conversion_confirmation_required', '{}')", (sid, sku))
    return path


def test_confirmation_is_bound_to_exact_sku_snapshot_and_positive_factor(unit_database):
    path = unit_database
    for invalid in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(ValueError, match="Коэффициент"):
            confirm_unit_conversion(path, 1, "A", invalid, archive_units_confirmed=True, reason="fixture", confirmed_by="tester")
    with pytest.raises(ValueError, match="основание"):
        confirm_unit_conversion(path, 1, "A", 4, archive_units_confirmed=True)
    confirmation = confirm_unit_conversion(path, 1, "A", 4, archive_units_confirmed=True, reason="fixture", confirmed_by="tester")
    assert convert_quantity(path, 1, "A", 5, confirmation_id=confirmation["id"])["accounting_quantity"] == 20
    for sid, sku in ((2, "A"), (1, "B")):
        with pytest.raises(ValueError, match="не относится"):
            convert_quantity(path, sid, sku, 5, confirmation_id=confirmation["id"])
    with pytest.raises(ValueError, match="подтверждение"):
        convert_quantity(path, 1, "A", 5)


def test_iek_ui_confirmation_requires_explicit_inputs(unit_database, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from hackalem.config import PROJECT_ROOT

    # load_settings uses the fixed database name inside its runtime directory.
    database = unit_database.with_name("hackalem.sqlite3")
    unit_database.rename(database)
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(database.parent))
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=20)
    app.selectbox(key="selected_snapshot_id").select(1).run(timeout=20)
    assert not app.exception
    next(item for item in app.text_input if item.label == "Код товара для проверки единиц").set_value("A").run(timeout=20)
    next(item for item in app.button if item.label == "Сохранить подтверждение единиц").click().run(timeout=20)
    assert not app.exception
    assert any("Подтвердите" in error.value for error in app.error)
    next(item for item in app.number_input if item.label == "Подтверждаемый коэффициент").set_value(4)
    next(item for item in app.number_input if item.label == "Количество в закупочной единице для проверки").set_value(5)
    next(item for item in app.text_input if item.label == "Кто подтвердил").set_value("UI test")
    next(item for item in app.text_input if item.label == "Основание подтверждения").set_value("Temporary fixture")
    app.checkbox[0].check()
    next(item for item in app.button if item.label == "Сохранить подтверждение единиц").click().run(timeout=20)
    assert not app.exception
    assert any("Подтверждение" in item.value for item in app.success)
    import json
    assert json.loads(app.json[-1].value)["accounting_quantity"] == 20
