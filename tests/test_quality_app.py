"""Quality UI integration with a tiny synthetic snapshot and real services."""
import importlib.util
import json
import shutil
import sqlite3
from contextlib import closing

from streamlit.testing.v1 import AppTest

from hackalem.config import PROJECT_ROOT


_spec = importlib.util.spec_from_file_location("quality_fixture", PROJECT_ROOT / "tests/test_quality.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def _counts(database):
    with closing(sqlite3.connect(database)) as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("quality_configurations", "quality_runs"))


def _click(app, label):
    next(button for button in app.button if button.label == label).click().run(timeout=30)
    assert not app.exception


def _start(tmp_path, monkeypatch):
    source_database, snapshot = _fixture._seed_store(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = runtime / "hackalem.sqlite3"
    shutil.copyfile(source_database, database)
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(runtime))
    monkeypatch.setenv("HACKALEM_SOURCE_DIR", str(tmp_path / "unavailable-synthetic-sources"))
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    app.selectbox(key="selected_snapshot_id").set_value(snapshot).run(timeout=30)
    assert not app.exception
    return app, database, snapshot


def test_quality_panel_does_not_create_runs_on_display_or_invalid_save(tmp_path, monkeypatch):
    app, database, snapshot = _start(tmp_path, monkeypatch)
    assert _counts(database) == (0, 0)
    assert any("проверок пока нет" in message.value for message in app.info)
    app.run(timeout=30)
    assert not app.exception
    assert _counts(database) == (0, 0)

    editor = app.text_area(key=f"quality_{snapshot}_configuration_text")
    editor.set_value('{"defaults": invalid JSON}')
    _click(app, "Сохранить настройку")
    assert any("Не удалось сохранить настройку" in message.value for message in app.error)
    assert _counts(database) == (0, 0)

    app.text_area(key=f"quality_{snapshot}_configuration_text").set_value("[]")
    _click(app, "Сохранить настройку")
    assert any("объектом JSON" in message.value for message in app.error)
    assert _counts(database) == (0, 0)


def test_quality_save_run_filter_and_product_detail_show_real_selected_history(tmp_path, monkeypatch):
    app, database, snapshot = _start(tmp_path, monkeypatch)
    payload = _fixture._configuration()
    payload["sales_choices"] = [_fixture._choice(end=_fixture.FEB)]
    app.text_area(key=f"quality_{snapshot}_configuration_text").set_value(json.dumps(payload, ensure_ascii=False))
    _click(app, "Сохранить настройку")
    assert _counts(database) == (1, 0)
    assert any("Настройка №" in message.value for message in app.success)
    configuration_id = app.selectbox(key=f"quality_{snapshot}_configuration_id").value
    assert configuration_id is not None

    _click(app, "Проверить данные")
    assert _counts(database) == (1, 1)
    run_id = app.selectbox(key=f"quality_{snapshot}_run_id").value
    assert run_id is not None
    app.text_input(key=f"quality_{snapshot}_sku_filter").set_value("A").run(timeout=30)
    assert not app.exception
    detail_key = f"quality_{snapshot}_{run_id}_detail_sku"
    assert app.selectbox(key=detail_key).options == ["A"]
    app.selectbox(key=detail_key).set_value("A").run(timeout=30)
    assert not app.exception
    json_values = [json.loads(item.value) if isinstance(item.value, str) else item.value for item in app.json]
    assert any(isinstance(item, dict) and item.get("lead_time_days", {}).get("value") == 14 for item in json_values)
    selected = [item.value for item in app.dataframe if {"source", "period", "quantity", "choice", "provenance"}.issubset(item.value.columns)]
    assert len(selected) == 1
    assert selected[0]["period"].tolist() == [_fixture.JAN, _fixture.FEB]
    assert selected[0]["source"].tolist() == ["monthly_sales", "monthly_sales"]
    assert selected[0]["quantity"].tolist() == [10, 20]
    assert all("file_id" in value for value in selected[0]["provenance"])
    assert _counts(database) == (1, 1)

    app.text_input(key=f"quality_{snapshot}_sku_filter").set_value("NO_SUCH_SKU").run(timeout=30)
    assert not app.exception
    assert any("товары не найдены" in message.value for message in app.info)
    assert not any(item.key == detail_key for item in app.selectbox)
    assert _counts(database) == (1, 1)

    app.text_input(key=f"quality_{snapshot}_sku_filter").set_value("").run(timeout=30)
    assert not app.exception
    assert set(app.selectbox(key=detail_key).options) == {"A", "B", "C", "D", "ONLY_STOCK"}
    assert _counts(database) == (1, 1)
