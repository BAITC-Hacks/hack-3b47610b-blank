from pathlib import Path

from streamlit.testing.v1 import AppTest


APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def test_initial_screen_runs_and_remains_empty_after_rerun(tmp_path, monkeypatch):
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("HACKALEM_SOURCE_DIR", str(tmp_path / "unavailable-reports"))
    app = AppTest.from_file(str(APP_PATH)).run(timeout=20)
    assert not app.exception
    assert app.title[0].value == "Помощник закупщика"
    assert any("Данные пока не загружены" in item.value for item in app.info)
    assert any("недоступна" in item.value for item in app.warning)
    assert not app.file_uploader
    assert (tmp_path / "runtime" / "hackalem.sqlite3").exists()
    app.run(timeout=20)
    assert not app.exception
    assert any("Данные пока не загружены" in item.value for item in app.info)
