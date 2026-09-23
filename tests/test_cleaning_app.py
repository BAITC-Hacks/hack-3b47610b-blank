"""The preparation panel is explicit and displays saved review results."""

import importlib.util
import sqlite3

from hackalem.config import PROJECT_ROOT


_spec = importlib.util.spec_from_file_location("quality_ui_fixture", PROJECT_ROOT / "tests/test_quality_app.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def _run_count(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM cleaning_runs").fetchone()[0]


def test_preparation_panel_requires_action_and_keeps_invalid_decisions_out(tmp_path, monkeypatch):
    app, database, snapshot = _fixture._start(tmp_path, monkeypatch)
    assert _run_count(database) == 0
    app.run(timeout=30)
    assert not app.exception and _run_count(database) == 0
    app.text_area(key=f"cleaning_{snapshot}_decisions").set_value("invalid JSON")
    _fixture._click(app, "Подготовить регулярный спрос")
    assert _run_count(database) == 0
    assert any("Подготовка не сохранена" in message.value for message in app.error)
    app.text_area(key=f"cleaning_{snapshot}_decisions").set_value("[]")
    _fixture._click(app, "Подготовить регулярный спрос")
    assert _run_count(database) == 1
    assert app.selectbox(key=f"cleaning_{snapshot}_run_id").value is not None
    assert any("Версия №" in message.value for message in app.success)
