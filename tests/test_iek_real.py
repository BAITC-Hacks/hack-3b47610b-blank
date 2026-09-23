"""Private source acceptance; no source document is written or recalculated."""

import hashlib
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from hackalem.config import PROJECT_ROOT, load_settings
from hackalem.services.imports import import_supplier, read_records, trace_cell
from hackalem.services.units import (
    confirm_unit_conversion, convert_quantity, get_unit_assessment, list_unit_issues,
)

SOURCE_ROOT = Path(os.environ.get("HACKALEM_REAL_SOURCE_DIR", str(PROJECT_ROOT)))


@pytest.mark.skipif(not (SOURCE_ROOT / "IEK").is_dir(), reason="Локальные отчёты IEK отсутствуют")
def test_real_iek_import_archive_units_and_idempotency(tmp_path):
    settings = load_settings({"HACKALEM_SOURCE_DIR": str(SOURCE_ROOT), "HACKALEM_DATA_DIR": str(tmp_path / "runtime")})
    files = sorted((SOURCE_ROOT / "IEK").glob("*.xlsx"))
    hashes = {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files}
    report = import_supplier(settings, "IEK")
    sid = report["snapshot_id"]
    assert report["supplier"] == "IEK"
    assert report["totals"]["files"] == 6
    assert report["totals"]["sheets"] == 6
    assert report["totals"]["external_sheets"] == 3
    assert report["totals"]["hidden_rows"] == 19
    assert report["totals"]["transactions"] == 171603
    assert report["totals"]["monthly_values"] == 175428
    assert report["totals"]["incoming_orders"] == 306
    assert report["totals"]["catalog_items"] == 39275
    assert report["totals"]["seasonal_values"] == 36
    by_kind = {file["source_kind"]: file for file in report["files"]}
    assert {kind: file["distinct_skus"] for kind, file in by_kind.items()} == {
        "minimums": 1937, "transactions": 2151, "monthly_stock": 2853,
        "monthly_sales": 2463, "incoming": 2614, "seasonality": 0,
    }
    minimums = read_records(settings.database_path, sid, "minimums", "measures")
    assert len(minimums) == 1938
    assert sum(row["state"] == "error" and row["number"] is None for row in minimums) == 15
    assert len([row for row in minimums if row["sku"] == "270400035_"]) == 2
    orders = read_records(settings.database_path, sid, "incoming", "incoming_orders")
    assert len({row["sku"] for row in orders if row["quantity"] > 0}) == 300
    assert {row["order_number"] for row in orders} == {"УТ-7583", "УТ-7974", "УТ-7848", "УТ-8231", "УТ-8233", "УТ-8234"}
    sample = next(row for row in orders if row["cell"] == "F471")
    assert (sample["sku"], sample["quantity"], sample["order_date"], sample["eta_deadline"], sample["unit"]) == (
        "280200087_", 5, "2026-09-07", "2026-10-01", None,
    )
    header = trace_cell(settings.database_path, sid, "incoming", "Лист4", "F1")
    assert len([row for row in header["normalized"] if row["table"] == "incoming_orders"]) == 152
    price = trace_cell(settings.database_path, sid, "minimums", "externalLink1.xml/Прайс", "P8146")
    assert price["source"]["value"] == 662
    assert price["external_source"]["status"] == "archived_external_cache"
    assert price["external_source"]["price_date"] == "2026-08-03"
    assert price["normalized"][0]["base_price"] == 662
    catalog = read_records(settings.database_path, sid, "minimums", "catalog_items", article="CKMP10D-N-025-016-K01")
    assert len(catalog) == 1
    assert (catalog[0]["minimum_order"], catalog[0]["order_multiple"], catalog[0]["currency"]) == (1, 1, "KZT")
    hidden = trace_cell(settings.database_path, sid, "seasonality", "Сезонность", "F28")
    assert hidden["row_metadata"]["hidden"] == 1
    assessment = get_unit_assessment(settings.database_path, sid, "280200087_")
    assert (assessment["accounting_unit"], assessment["purchase_unit"], assessment["proposed_factor"]) == ("шт", "компл", 4)
    assert assessment["status"] == "conversion_confirmation_required"
    assert assessment["details"]["archive_terms_active"] is False
    assert list_unit_issues(settings.database_path, sid, sku="280200087_")
    with pytest.raises(ValueError, match="подтверждение"):
        convert_quantity(settings.database_path, sid, "280200087_", 5)
    with pytest.raises(ValueError, match="Подтвердите"):
        confirm_unit_conversion(settings.database_path, sid, "280200087_", 4,
                                reason="test", confirmed_by="test")
    decision = confirm_unit_conversion(settings.database_path, sid, "280200087_", 4,
                                      archive_units_confirmed=True, reason="Проверочный пример задания, временная база",
                                      confirmed_by="test fixture")
    converted = convert_quantity(settings.database_path, sid, "280200087_", 5, confirmation_id=decision["id"])
    assert converted["accounting_quantity"] == 20
    assert converted["archive_prices_and_moq_active"] is False
    repeat = import_supplier(settings, "IEK")
    assert repeat["snapshot_id"] == sid
    assert repeat["reused_files"] == 6
    assert repeat["totals"] == report["totals"]
    with closing(sqlite3.connect(settings.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM import_files").fetchone()[0] == 6
        assert connection.execute("SELECT COUNT(*) FROM import_issues WHERE code='EXCEL_CELL_ERROR' AND sku IS NOT NULL").fetchone()[0] == 15
        assert connection.execute("SELECT COUNT(*) FROM monthly_values WHERE series='opening_stock'").fetchone()[0] == 94149
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {file: hashlib.sha256(file.read_bytes()).hexdigest() for file in files} == hashes
