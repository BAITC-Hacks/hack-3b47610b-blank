"""The stage-7 panel reports real-data limitations without inventing days."""

import importlib.util
import sqlite3

from hackalem.config import PROJECT_ROOT
from hackalem.services.cleaning import run_cleaning


_spec = importlib.util.spec_from_file_location("quality_ui_fixture_lost", PROJECT_ROOT / "tests/test_quality_app.py")
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)


def _count(database):
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT COUNT(*) FROM lost_demand_runs").fetchone()[0]


def test_real_panel_is_explicit_and_shows_unavailable_without_daily_evidence(tmp_path, monkeypatch):
    app, database, snapshot = _fixture._start(tmp_path, monkeypatch)
    cleaned = run_cleaning(database, snapshot, "2026-01-01")
    app.run(timeout=30)
    assert not app.exception and _count(database) == 0
    app.selectbox(key=f"lost_demand_{snapshot}_cleaning_run").set_value(cleaned["run_id"]).run(timeout=30)
    app.text_input(key=f"lost_demand_{snapshot}_sku").set_value("A").run(timeout=30)
    assert not app.exception and _count(database) == 0
    _fixture._click(app, "Оценить упущенный спрос")
    assert _count(database) == 1
    assert any("Точная коррекция недоступна" in item.value for item in app.warning)
