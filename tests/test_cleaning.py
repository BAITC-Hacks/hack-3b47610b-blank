"""Stage 6: signed returns, reviewable one-offs and versioned prepared history."""

import sqlite3

import pytest

from hackalem.domain.cleaning import aggregate_months, classify_documents
from hackalem.services.cleaning import cleaning_report, prepared_input, run_cleaning
from hackalem.services.synthetic import create_synthetic_dataset, synthetic_report


def _document(key, sku, period, quantity, customer=None, doc_type="Расходная накладная"):
    return {"document_key": key, "sku": sku, "period": period, "raw_quantity": quantity,
            "source_state": "value", "document_type": doc_type, "customer_id": customer}


def test_single_document_without_customer_is_reviewed_not_automatically_removed():
    assert classify_documents([]) == [] and aggregate_months([]) == []
    rows = classify_documents([_document("1", "IEK-210K", "2025-06-01", 210000)])
    assert rows[0]["status"] == "candidate"
    assert rows[0]["client_analysis"] == "unavailable"
    assert rows[0]["regular_quantity"] == 210000  # provisional only
    assert aggregate_months(rows)[0]["regular_quantity"] is None
    assert "клиентский анализ недоступен" in rows[0]["reason"] or "короткой" in rows[0]["reason"]


def test_repeated_large_customer_and_growth_remain_regular():
    documents = [_document(f"base-{i}", "A", f"2024-{(i % 12) + 1:02d}-01", 2, "base") for i in range(20)]
    documents += [_document(f"large-{i}", "A", f"2025-{i + 1:02d}-01", 80, "repeat") for i in range(3)]
    documents += [_document(f"growth-{i}", "G", f"2025-{i + 1:02d}-01", 2 + i, "grow") for i in range(12)]
    rows = classify_documents(documents, policy="exclude_high_confidence")
    assert all(row["status"] == "regular" and row["regular_quantity"] == 80
               for row in rows if row["document_key"].startswith("large-"))
    assert all(row["status"] == "regular" for row in rows if row["sku"] == "G")


def test_return_sign_and_ambiguous_negative_remain_distinct():
    documents = [_document(f"sale-{i}", "A", "2025-02-01", 10) for i in range(12)]
    documents += [_document("return", "A", "2025-02-01", -7, doc_type="Возврат от покупателя"),
                  _document("ambiguous", "B", "2025-02-01", -3)]
    rows = {row["document_key"]: row for row in classify_documents(documents)}
    assert rows["return"]["status"] == "return" and rows["return"]["return_quantity"] == -7
    assert rows["return"]["regular_quantity"] == 0
    assert rows["ambiguous"]["status"] == "needs_review" and rows["ambiguous"]["regular_quantity"] is None
    months = {row["sku"]: row for row in aggregate_months(rows.values())}
    assert months["A"]["raw_signed_quantity"] == 113 and months["A"]["regular_quantity"] == 120
    assert months["B"]["state"] == "needs_review"


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    return create_synthetic_dataset(tmp_path_factory.mktemp("cleaning-synthetic"), seed=20260923)


def test_synthetic_one_off_return_repetition_and_immutable_sources(dataset):
    database = dataset["database_path"]
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT COUNT(*),SUM(quantity) FROM transactions").fetchone()
    report = run_cleaning(database, 2, "2026-09-22", policy="exclude_high_confidence")
    assert report["summary"]["status_counts"]["excluded_by_policy"] == 1
    assert report["summary"]["status_counts"]["return"] == 1
    assert run_cleaning(database, 2, "2026-09-22", policy="exclude_high_confidence")["run_id"] == report["run_id"]
    one_off = cleaning_report(database, report["run_id"], sku="SYN-A-006", limit=1000)
    removed = next(item for item in one_off["documents"] if item["status"] == "excluded_by_policy")
    assert removed["raw_quantity"] == removed["removed_component"] == 500
    assert removed["regular_quantity"] == 0
    june = next(row for row in one_off["months"] if row["period"] == "2025-06-01")
    assert june["raw_signed_quantity"] - june["regular_quantity"] == 500
    assert june["project_commitment_quantity"] == 0
    repeat = cleaning_report(database, report["run_id"], sku="SYN-A-007", limit=1000)
    assert all(row["regular_quantity"] == row["raw_signed_quantity"] for row in repeat["months"])
    returned = cleaning_report(database, report["run_id"], sku="SYN-A-008", limit=1000)
    assert any(row["return_quantity"] == -7 and row["raw_quantity"] == -7 for row in returned["documents"])
    review_only = run_cleaning(database, 2, "2026-09-22")
    assert review_only["run_id"] != report["run_id"]
    assert cleaning_report(database, review_only["run_id"], sku="SYN-A-006")["months_total"] > 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*),SUM(quantity) FROM transactions").fetchone() == before
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert synthetic_report(dataset["dataset_dir"])["validation"]["model_fingerprint"] == "ok"


def test_manual_exclusion_preserves_project_obligation(dataset):
    database = dataset["database_path"]
    base = run_cleaning(database, 2, "2026-09-22")
    key = next(row["document_key"] for row in cleaning_report(database, base["run_id"], sku="SYN-A-006", limit=1000)["documents"]
               if row["raw_quantity"] == 500)
    decision = [{"document_key": key, "action": "exclude_regular", "author": "Проверяющий",
                 "reason": "Подтверждённый проектный заказ", "project_commitment_quantity": 500}]
    changed = run_cleaning(database, 2, "2026-09-22", decisions=decision)
    assert changed["run_id"] != base["run_id"]
    row = next(row for row in cleaning_report(database, changed["run_id"], sku="SYN-A-006", limit=1000)["documents"]
               if row["document_key"] == key)
    assert row["status"] == "excluded_manual" and row["project_commitment_quantity"] == 500
    assert row["regular_quantity"] == 0
    with pytest.raises(ValueError, match="отсутствующий документ"):
        run_cleaning(database, 2, "2026-09-22", decisions=[{**decision[0], "document_key": "wrong"}])


def test_prepared_input_resolves_only_handled_returns(dataset):
    database = dataset["database_path"]
    quality_id = next(row["run_id"] for row in dataset["snapshots"] if row["snapshot_id"] == 2)
    clean = run_cleaning(database, 2, dataset["manifest"]["as_of"])
    with pytest.raises(ValueError, match="Сценарный расчёт"):
        prepared_input(database, quality_id, clean["run_id"], "SYN-A-008")
    result = prepared_input(database, quality_id, clean["run_id"], "SYN-A-008", allow_scenario=True)
    assert result["status"] == "Сценарный расчёт"
    month = next(row for row in result["history"] if row["period"] == "2025-02-01")
    assert month["return_quantity"] == -7 and month["quantity"] == month["raw_quantity"] + 7
    with pytest.raises(ValueError, match="требует решения"):
        prepared_input(database, quality_id, clean["run_id"], "SYN-A-006", allow_scenario=True)
