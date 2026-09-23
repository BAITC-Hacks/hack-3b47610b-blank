"""Acceptance check for the supplied local reports; skipped without private data."""

import hashlib
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from openpyxl import load_workbook

from hackalem.config import PROJECT_ROOT, load_settings
from hackalem.services.systeme import import_systeme, read_records, trace_cell


SOURCE_ROOT = Path(os.environ.get("HACKALEM_REAL_SOURCE_DIR", str(PROJECT_ROOT)))


@pytest.mark.skipif(not (SOURCE_ROOT / "Systeme electric").is_dir(), reason="Локальные конфиденциальные отчёты отсутствуют")
def test_all_real_sources_versions_lineage_and_source_integrity(tmp_path):
    settings = load_settings({"HACKALEM_SOURCE_DIR": str(SOURCE_ROOT), "HACKALEM_DATA_DIR": str(tmp_path / "runtime")})
    files = sorted((SOURCE_ROOT / "Systeme electric").glob("*.xlsx"))
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    report = import_systeme(settings)
    snapshot_id = report["snapshot_id"]
    assert report["totals"]["files"] == 6
    assert report["totals"]["sheets"] == 8
    assert sum(sheet["state"] == "hidden" for file in report["files"] for sheet in file["sheets"]) == 2
    by_kind = {file["source_kind"]: file for file in report["files"]}
    assert {kind: file["distinct_skus"] for kind, file in by_kind.items()} == {
        "multiples": 554, "transactions": 565, "monthly_stock": 701,
        "monthly_sales": 554, "seasonality": 0, "current": 497,
    }
    assert report["totals"]["transactions"] == 77312
    assert report["totals"]["monthly_values"] == 57816
    assert report["totals"]["seasonal_values"] == 108
    assert by_kind["current"]["snapshot_date"] == "2026-09-22"
    assert all(file["snapshot_date"] is None for kind, file in by_kind.items() if kind != "current")
    for cell, expected, formula in [("AP3", 3773, "=SUM(AC3:AO3)"), ("AX3", 1118, None), ("BC484", 37800, None)]:
        trace = trace_cell(settings.database_path, snapshot_id, "current", "TDSheet", cell)
        assert trace["source"]["value"] == expected
        if formula:
            assert trace["source"]["formula"] == formula
        assert trace["normalized"]
    blank = trace_cell(settings.database_path, snapshot_id, "transactions", "Лист_1", "H25635")
    assert blank["source"]["value"] is None
    assert blank["normalized"][0]["quantity"] is None
    hidden = trace_cell(settings.database_path, snapshot_id, "monthly_sales", "Лист1", "B4")
    assert hidden["sheet_state"] == "hidden"
    assert hidden["normalized"]
    eta = trace_cell(settings.database_path, snapshot_id, "current", "TDSheet", "BC2")
    assert len(eta["normalized"]) == 497
    assert all(item["metric"] == "incoming_eta_label" for item in eta["normalized"])
    transactions = read_records(settings.database_path, snapshot_id, "transactions", "transactions")
    assert sum(row["quantity"] is None for row in transactions) == 13
    assert sum(row["quantity"] is not None and row["quantity"] < 0 for row in transactions) == 302
    assert min(row["occurred_at"] for row in transactions).startswith("2023-01-18")
    assert max(row["occurred_at"] for row in transactions).startswith("2026-09-22")
    assert sum(row["document_type"] == "Заказ покупателя" for row in transactions) == 3
    repeat = import_systeme(settings)
    assert repeat["snapshot_id"] == snapshot_id
    assert repeat["reused_files"] == 6
    assert repeat["totals"] == report["totals"]

    # Change only a disposable copy. The previous dataset must remain queryable.
    copied_folder = tmp_path / "copied" / "Systeme electric"
    copied_folder.mkdir(parents=True)
    for source in files:
        shutil.copy2(source, copied_folder / source.name)
    copied_multiple = copied_folder / "MOQ SystemElectric.xlsx"
    workbook = load_workbook(copied_multiple)
    workbook["Лист_1"]["E3"] = 2
    workbook.save(copied_multiple)
    workbook.close()
    changed_settings = load_settings({"HACKALEM_SOURCE_DIR": str(copied_folder.parent), "HACKALEM_DATA_DIR": str(settings.data_dir)})
    changed = import_systeme(changed_settings)
    assert changed["reused_files"] == 5
    assert changed["snapshot_id"] != snapshot_id
    assert trace_cell(settings.database_path, snapshot_id, "multiples", "Лист_1", "E3")["source"]["value"] == 1
    assert trace_cell(settings.database_path, changed["snapshot_id"], "multiples", "Лист_1", "E3")["source"]["value"] == 2
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM import_files").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 77312
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files} == before
