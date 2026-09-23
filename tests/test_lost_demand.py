"""Independent behaviour checks for full-day stockouts and causal estimates."""

import importlib.util
import json
import sqlite3
import shutil
from pathlib import Path

import pytest

from hackalem.config import PROJECT_ROOT
from hackalem.domain.lost_demand import estimate_days
from hackalem.services.cleaning import run_cleaning
from hackalem.services.lost_demand import (
    _scenario_evidence, adjusted_history, lost_demand_report, run_lost_demand,
)
from hackalem.services.synthetic import create_synthetic_dataset, synthetic_report
from hackalem.storage import SCHEMA_VERSION, initialize_database


@pytest.fixture(scope="module")
def dataset(tmp_path_factory):
    return create_synthetic_dataset(tmp_path_factory.mktemp("lost-demand-synthetic"), seed=20260923)


def test_confirmed_stockout_matches_hidden_synthetic_truth_without_oracle_access(dataset):
    database = dataset["database_path"]
    cleaned = run_cleaning(database, 2, "2026-01-01")
    report = run_lost_demand(database, cleaned["run_id"], "SYN-A-009")
    truth = json.loads((Path(dataset["dataset_dir"]) / "validation/truth.json").read_text(encoding="utf-8"))
    assert report["summary"]["confirmed_stockout_days"] == 28
    assert report["summary"]["estimated_lost_quantity"] == 196
    for period in ("2024-08-01", "2025-08-01"):
        month = next(item for item in report["months"] if item["period"] == period)
        assert (month["observed_regular_quantity"], month["estimated_lost_quantity"],
                month["adjusted_training_quantity"]) == (119, 98, 217)
        assert month["confirmed_stockout_days"] == 14
        assert month["historical_lost_demand_as_current_backlog"] == 0
        actual = sum(item["regular_demand"] for item in truth["daily"]
                     if item["sku"] == "SYN-A-009" and item["date"].startswith(period[:7]))
        raw_31_day_projection = month["observed_regular_quantity"] / 31 * 31
        adjusted_31_day_projection = month["adjusted_training_quantity"] / 31 * 31
        assert adjusted_31_day_projection > raw_31_day_projection
        assert abs(adjusted_31_day_projection - actual) < abs(raw_31_day_projection - actual)
    again = run_lost_demand(database, cleaned["run_id"], "SYN-A-009")
    assert again["run_id"] == report["run_id"]
    history = adjusted_history(database, report["run_id"])
    assert history["historical_lost_demand_as_current_backlog"] == 0
    assert [item["quantity"] for item in history["history"] if item["period"] in
            ("2024-08-01", "2025-08-01")] == [217, 217]
    assert synthetic_report(dataset["dataset_dir"])["validation"]["model_fingerprint"] == "ok"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_no_stockout_no_bonus_and_unknown_remains_distinct(dataset):
    database = dataset["database_path"]
    for snapshot, sku in ((2, "SYN-A-001"), (1, "SYN-B-001")):
        cleaned = run_cleaning(database, snapshot, "2026-01-01")
        report = run_lost_demand(database, cleaned["run_id"], sku)
        assert report["summary"]["confirmed_stockout_days"] == 0
        if sku == "SYN-A-001":
            assert report["summary"]["estimated_lost_quantity"] == 0
            assert all(item["adjusted_training_quantity"] == item["observed_regular_quantity"]
                       for item in report["months"])
        else:
            assert report["summary"]["estimated_lost_quantity"] is None
            november = next(item for item in report["months"] if item["period"] == "2025-11-01")
            assert november["state"] == "needs_review"
            assert november["adjusted_training_quantity"] is None
            assert november["unknown_or_unresolved_days"] >= 7
            assert not report["summary"]["full_day_evidence_available"]


def test_historical_cutoff_never_reads_future_days(dataset):
    database = dataset["database_path"]
    cleaned = run_cleaning(database, 2, "2024-09-01")
    report = run_lost_demand(database, cleaned["run_id"], "SYN-A-009")
    assert all(item["period"] <= "2024-08-01" for item in report["months"])
    august = next(item for item in report["months"] if item["period"] == "2024-08-01")
    assert august["estimated_lost_quantity"] == 98
    for item in lost_demand_report(database, report["run_id"], limit=10000)["days"]:
        assert item["date"] < "2024-09-01"
        assert all(reference < item["date"] for reference in item["reference_days"])


def test_explicit_intervals_produce_only_scenario_history(dataset):
    database = dataset["database_path"]
    cleaned = run_cleaning(database, 2, "2024-09-01")
    shared = {"observed_hours_per_day": 24, "scope": "all_selected_warehouses",
              "author": "Тестовый оператор", "reason": "Ручной проверочный интервал"}
    intervals = [{**shared, "start": "2024-08-01", "end": "2024-08-10", "state": "available"},
                 {**shared, "start": "2024-08-11", "end": "2024-08-24", "state": "out_of_stock"}]
    report = run_lost_demand(database, cleaned["run_id"], "SYN-A-009", scenario_intervals=intervals)
    august = next(item for item in report["months"] if item["period"] == "2024-08-01")
    assert report["summary"]["source"] == "manual_scenario"
    assert august["state"] == "scenario" and august["estimated_lost_quantity"] == 98
    assert august["evidence_coverage"] == "partial_month"
    assert adjusted_history(database, report["run_id"])["scenario"] is True


def test_one_snapshot_or_one_sale_cannot_prove_full_day_or_extrapolate():
    sales = {"2025-01-01": {"quantity": 9, "state": "value", "document_keys": ["a"]}}
    evidence = [{"date": "2025-01-01", "available": True, "observed_hours": 24},
                {"date": "2025-01-02", "available": False, "observed_hours": 1},
                {"date": "2025-01-03", "available": False, "observed_hours": 24}]
    days = estimate_days(evidence, sales, "2025-02-01")
    assert days[1]["state"] == "unknown_availability" and days[1]["estimated_lost_quantity"] is None
    assert days[2]["state"] == "insufficient_history" and days[2]["estimated_lost_quantity"] is None
    # Later observations cannot change an estimate made for an earlier date.
    future = evidence + [{"date": "2025-01-04", "available": True, "observed_hours": 24}]
    future_sales = {**sales, "2025-01-04": {"quantity": 1000, "state": "value", "document_keys": ["future"]}}
    assert estimate_days(future, future_sales, "2025-02-01")[2] == days[2]
    long_gap = ([{"date": f"2025-01-{day:02d}", "available": True, "observed_hours": 24}
                 for day in range(1, 11)] +
                [{"date": f"2025-01-{day:02d}", "available": False, "observed_hours": 24}
                 for day in range(11, 32)])
    known = {f"2025-01-{day:02d}": {"quantity": 7, "state": "value", "document_keys": [str(day)]}
             for day in range(1, 11)}
    capped = estimate_days(long_gap, known, "2025-02-01")
    assert sum(item["state"] == "estimated" for item in capped) == 20
    assert capped[-1]["state"] == "extrapolation_limit"


def test_real_data_requires_explicit_scenario_intervals(tmp_path):
    spec = importlib.util.spec_from_file_location("quality_fixture_lost", PROJECT_ROOT / "tests/test_quality.py")
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    database, snapshot = fixture._seed_store(tmp_path)
    cleaned = run_cleaning(database, snapshot, "2026-01-01")
    report = run_lost_demand(database, cleaned["run_id"], "A")
    assert report["summary"]["source"] == "real_without_daily_availability"
    assert report["summary"]["estimated_lost_quantity"] is None
    assert all(item["state"] == "exact_unavailable" and item["adjusted_training_quantity"] is None
               for item in report["months"])
    with pytest.raises(ValueError, match="Точная коррекция недоступна"):
        adjusted_history(database, report["run_id"])
    interval = {"start": "2025-01-01", "end": "2025-01-01", "state": "out_of_stock",
                "observed_hours_per_day": 1, "scope": "all_selected_warehouses",
                "author": "Проверяющий", "reason": "Проверка режима"}
    scenario = run_lost_demand(database, cleaned["run_id"], "A", scenario_intervals=[interval])
    assert scenario["summary"]["source"] == "manual_scenario"
    assert scenario["days"][0]["state"] == "unknown_availability"
    with pytest.raises(ValueError, match="охват"):
        _scenario_evidence([{**interval, "scope": "one_warehouse"}], "2026-01-01")
    with pytest.raises(ValueError, match="пересекаются"):
        _scenario_evidence([interval, interval], "2026-01-01")


def test_migration_from_stage6_preserves_cleaning_and_source_data(dataset, tmp_path):
    source = Path(dataset["database_path"])
    cleaned = run_cleaning(source, 2, "2026-01-01")
    target = tmp_path / "hackalem.sqlite3"
    shutil.copyfile(source, target)
    with sqlite3.connect(target) as connection:
        before = connection.execute("SELECT COUNT(*),SUM(quantity) FROM transactions").fetchone()
        old_run = connection.execute("SELECT COUNT(*) FROM cleaning_documents WHERE run_id=?",
                                     (cleaned["run_id"],)).fetchone()[0]
        for table in ("order_events", "order_items", "order_versions", "order_projects",
                      "replenishment_items", "replenishment_runs",
                      "forecast_points", "forecast_runs",
                      "lost_demand_days", "lost_demand_months", "lost_demand_runs"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=5")
    assert initialize_database(target).schema_version == SCHEMA_VERSION
    assert initialize_database(target).schema_version == SCHEMA_VERSION
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*),SUM(quantity) FROM transactions").fetchone() == before
        assert connection.execute("SELECT COUNT(*) FROM cleaning_documents WHERE run_id=?",
                                  (cleaned["run_id"],)).fetchone()[0] == old_run
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
