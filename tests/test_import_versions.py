"""Storage invariants, independent of the partner workbook layouts."""

import hashlib
import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from hackalem.config import load_settings
from hackalem.services import systeme
from hackalem.storage import SCHEMA_VERSION, UnsupportedSchemaError, initialize_database


@pytest.fixture
def sources(tmp_path, monkeypatch):
    folder = tmp_path / "sources" / "Systeme electric"
    folder.mkdir(parents=True)
    for kind in systeme.SOURCE_KINDS:
        (folder / f"{kind}.xlsx").write_text(f"{kind}:5", encoding="utf-8")

    def parse_copy(path):
        kind, number = path.read_text(encoding="utf-8").split(":")
        quantity = int(number)
        return SimpleNamespace(
            source_kind=kind, snapshot_date=None,
            sheets=[dict(sheet="Sheet", state="visible", declared_dimension="A1:H3", max_row=3, max_column=8)],
            raw_rows=[dict(sheet="Sheet", row=row, cells={"H": dict(value=quantity, formula=None, data_type="n", number_format="0")}) for row in [2, 3]],
            products=[dict(sheet="Sheet", row=2, sku="0007_", name="Example", article=None, unit="шт", cell="H2")],
            transactions=[dict(sheet="Sheet", row=row, sku="0007_", occurred_at="2026-09-22T00:00:00", document_number="00012", document_type="sale", unit="шт", warehouse="Test", quantity=quantity, state="value", cell=f"H{row}") for row in [2, 3]] if kind == "transactions" else [],
            monthly_values=[], measures=[], seasonal_values=[], issues=[],
        )

    monkeypatch.setattr(systeme.parser, "parse_workbook", parse_copy)
    settings = load_settings({"HACKALEM_SOURCE_DIR": str(folder.parent), "HACKALEM_DATA_DIR": str(tmp_path / "runtime")})
    return settings, folder


def _counts(path):
    with closing(sqlite3.connect(path)) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("import_files", "snapshots", "transactions", "source_rows")}


def test_repeat_import_is_idempotent_and_keeps_identical_transaction_lines(sources):
    settings, folder = sources
    original = {path.name: path.read_bytes() for path in folder.iterdir()}
    first = systeme.import_systeme(settings)
    counts = _counts(settings.database_path)
    second = systeme.import_systeme(settings)
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["reused_files"] == 6
    assert counts == _counts(settings.database_path)
    assert counts["transactions"] == 2
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == original
    assert first["versions"]["parameters"]["eta_year"] is None
    assert first["versions"]["code_manifest"]


def test_changed_file_and_parameters_keep_old_snapshot_reproducible(sources):
    settings, folder = sources
    first = systeme.import_systeme(settings)
    (folder / "transactions.xlsx").write_text("transactions:9", encoding="utf-8")
    second = systeme.import_systeme(settings)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["reused_files"] == 5
    assert _counts(settings.database_path)["import_files"] == 7
    for snapshot, expected in [(first, 5), (second, 9)]:
        facts = systeme.read_records(settings.database_path, snapshot["snapshot_id"], "transactions", "transactions")
        assert [row["quantity"] for row in facts] == [expected, expected]
        trace = systeme.trace_cell(settings.database_path, snapshot["snapshot_id"], "transactions", "Sheet", "H2")
        assert trace["source"]["value"] == expected
        assert next(item for item in trace["normalized"] if item["table"] == "transactions")["quantity"] == expected
        assert trace["file"]["sha256"] == hashlib.sha256(f"transactions:{expected}".encode()).hexdigest()
    third = systeme.import_systeme(settings, {"scenario_note": "explicit parameters version"})
    assert third["reused_files"] == 6
    assert third["versions"]["parameter_version"] != second["versions"]["parameter_version"]
    assert _counts(settings.database_path)["import_files"] == 7
    assert systeme.report_snapshot(settings.database_path, first["snapshot_id"])["versions"] == first["versions"]


def test_failed_batch_rolls_back_and_does_not_modify_old_snapshot(sources):
    settings, folder = sources
    first = systeme.import_systeme(settings)
    before = _counts(settings.database_path)
    (folder / "current.xlsx").write_text("current:999", encoding="utf-8")
    (folder / "transactions.xlsx").write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError, match="transactions.xlsx"):
        systeme.import_systeme(settings)
    assert _counts(settings.database_path) == before
    assert systeme.report_snapshot(settings.database_path, first["snapshot_id"]) == {key: value for key, value in first.items() if key != "reused_files"}
    assert not list(settings.data_dir.glob("systeme-import-*"))


def test_snapshot_requires_exactly_one_matching_version_per_source(sources):
    settings, _ = sources
    report = systeme.import_systeme(settings)
    versions = {file["source_kind"]: file["id"] for file in report["files"]}
    assert systeme.create_snapshot(settings.database_path, versions) == report["snapshot_id"]
    with pytest.raises(ValueError, match="шесть"):
        systeme.create_snapshot(settings.database_path, {"current": versions["current"]})
    versions["transactions"] = versions["current"]
    with pytest.raises(ValueError, match="не соответствует"):
        systeme.create_snapshot(settings.database_path, versions)
    with pytest.raises(ValueError, match="не найден"):
        systeme.read_records(settings.database_path, 999, "transactions", "transactions")


def test_transform_code_change_creates_new_parse_versions(sources, monkeypatch):
    settings, _ = sources
    first = systeme.import_systeme(settings)
    monkeypatch.setattr(systeme.parser, "RULES_VERSION", "test-rules-next")
    second = systeme.import_systeme(settings)
    assert second["reused_files"] == 0
    assert second["versions"]["rules_version"] == ["test-rules-next"]
    assert systeme.report_snapshot(settings.database_path, first["snapshot_id"])["versions"]["rules_version"] != second["versions"]["rules_version"]


def test_schema_one_migration_preserves_user_metadata_and_is_repeatable(tmp_path):
    path = tmp_path / "v1.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT INTO app_metadata VALUES (?, ?)", [("created_at_utc", "original"), ("keep", "mine")])
        connection.execute("PRAGMA user_version=1")
    initialize_database(path)
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        assert dict(connection.execute("SELECT key, value FROM app_metadata")) == {"created_at_utc": "original", "keep": "mine"}
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_schema_two_with_missing_table_is_rejected_without_changes(tmp_path):
    path = tmp_path / "v2.sqlite3"
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE transactions")
    original = path.read_bytes()
    with pytest.raises(UnsupportedSchemaError):
        initialize_database(path)
    assert path.read_bytes() == original


def test_filename_date_context_participates_in_import_identity(sources, monkeypatch):
    settings, folder = sources
    original_parser = systeme.parser.parse_workbook

    def parse_with_date(path):
        parsed = original_parser(path)
        if parsed.source_kind == "current":
            parsed.snapshot_date = "2026-09-23" if "23.09.2026" in path.name else "2026-09-22"
        return parsed

    monkeypatch.setattr(systeme.parser, "parse_workbook", parse_with_date)
    first = systeme.import_systeme(settings)
    (folder / "current.xlsx").rename(folder / "current 23.09.2026.xlsx")
    second = systeme.import_systeme(settings)
    assert second["reused_files"] == 5
    assert next(file for file in first["files"] if file["source_kind"] == "current")["snapshot_date"] == "2026-09-22"
    assert next(file for file in second["files"] if file["source_kind"] == "current")["snapshot_date"] == "2026-09-23"


def test_application_code_version_creates_new_snapshot_without_duplicate_facts(sources, monkeypatch):
    settings, _ = sources
    first = systeme.import_systeme(settings)
    monkeypatch.setattr(systeme, "_code_manifest", lambda: ("new-code-hash", {"example.py": "new-source-hash"}))
    second = systeme.import_systeme(settings)
    assert second["snapshot_id"] != first["snapshot_id"]
    assert second["reused_files"] == 6
    assert second["versions"]["code_version"] == "new-code-hash"
    assert _counts(settings.database_path)["transactions"] == 2


def test_import_screen_report_repeat_and_lineage(sources, monkeypatch):
    from streamlit.testing.v1 import AppTest
    from hackalem.config import PROJECT_ROOT

    settings, _ = sources
    monkeypatch.setenv("HACKALEM_SOURCE_DIR", str(settings.source_dir))
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(settings.data_dir))
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=20)
    app.button[0].click().run(timeout=20)
    assert not app.exception
    assert app.selectbox(key="selected_snapshot_id").value == 1
    assert [metric.value for metric in app.metric[:2]] == ["6", "6"]
    app.button[0].click().run(timeout=20)
    assert not app.exception
    assert "Файлов без изменений: 6" in app.success[0].value
    app.selectbox(key="lineage_source").select("transactions").run(timeout=20)
    app.text_input[0].set_value("H2")
    next(button for button in app.button if button.label == "Показать происхождение").click().run(timeout=20)
    assert not app.exception
    assert json.loads(app.json[-1].value)["source"]["value"] == 5
