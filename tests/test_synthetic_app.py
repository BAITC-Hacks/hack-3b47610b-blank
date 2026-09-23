"""Shared UI displays synthetic inputs distinctly and keeps real imports disabled."""
from contextlib import closing
from pathlib import Path
import sqlite3

from streamlit.testing.v1 import AppTest

from hackalem.config import PROJECT_ROOT
from hackalem.services.synthetic import create_synthetic_dataset


def _quality_counts(database):
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("quality_configurations", "quality_runs"))


def test_synthetic_home_marks_dataset_disables_real_import_and_opens_both_checks(tmp_path, monkeypatch):
    generated = create_synthetic_dataset(tmp_path / "datasets", seed=20260923)
    database = Path(generated["database_path"])
    source_dir = tmp_path / "available-source-folder"
    source_dir.mkdir()
    # An existing source directory makes the synthetic dataset flag the reason
    # the import button is disabled, not the missing-source fallback.
    monkeypatch.setenv("HACKALEM_SOURCE_DIR", str(source_dir))
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(database.parent))
    before = _quality_counts(database)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    assert any("СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР" in message.value for message in app.warning)
    assert any("Для реальных закупок не применяется" in message.value for message in app.warning)
    assert len(app.selectbox(key="selected_snapshot_id").options) == 2

    for snapshot in generated["snapshots"]:
        supplier = snapshot["supplier"]
        snapshot_id = snapshot["snapshot_id"]
        run_id = snapshot["run_id"]
        app.selectbox(key="import_supplier").set_value(supplier).run(timeout=30)
        assert not app.exception
        button = next(item for item in app.button if item.label == f"Импортировать {supplier}")
        assert button.disabled is True
        app.selectbox(key="selected_snapshot_id").set_value(snapshot_id).run(timeout=30)
        assert not app.exception
        app.selectbox(key=f"quality_{snapshot_id}_run_id").set_value(run_id).run(timeout=30)
        assert not app.exception
        reports = [item.value for item in app.dataframe
                   if {"Код", "Статус", "Входы допускают подтверждённый заказ"}.issubset(item.value.columns)]
        assert len(reports) == 1
        assert len(reports[0]) == snapshot["sku_count"]
        assert not reports[0]["Входы допускают подтверждённый заказ"].any()
        assert set(reports[0]["Статус"]) <= {"Сценарный расчёт", "Не хватает данных"}
        assert all(sku.startswith("SYN-") for sku in reports[0]["Код"])

        sku = "SYN-B-001" if supplier == "IEK" else "SYN-A-001"
        app.text_input(key=f"quality_{snapshot_id}_sku_filter").set_value(sku).run(timeout=30)
        assert not app.exception
        detail = f"quality_{snapshot_id}_{run_id}_detail_sku"
        assert app.selectbox(key=detail).options == [sku]
        app.selectbox(key=detail).set_value(sku).run(timeout=30)
        assert not app.exception
        histories = [item.value for item in app.dataframe
                     if {"period", "source", "quantity", "provenance"}.issubset(item.value.columns)]
        assert len(histories) == 1 and len(histories[0]) == 24
        assert set(histories[0]["source"]) == {"transactions"}
        assert _quality_counts(database) == before
        assert any("СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР" in message.value for message in app.warning)
