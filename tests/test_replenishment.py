"""Stage 9: arithmetic, calendar risks, units and persisted headless runs."""

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from hackalem.domain.replenishment import calculate_replenishment
from hackalem.__main__ import main
from hackalem.services.cleaning import run_cleaning
from hackalem.services.lost_demand import lost_demand_report, run_lost_demand
from hackalem.services.forecasting import run_forecast
from hackalem.services.replenishment import replenishment_report, run_replenishment
from hackalem.services.synthetic import create_synthetic_dataset
from hackalem.storage import initialize_database


AS_OF = "2026-09-22"


def _prepared(**changes):
    values = {"stock_date": AS_OF, "current_stock": 100, "reserved_stock": 10,
              "lead_time_days": 15, "review_period_days": 15,
              "stock_policy": {"mode": "stock", "safety_days": 6},
              "minimum_order": 0, "order_multiple": 12,
              "accounting_unit": "шт", "purchase_unit": "шт", "unit_factor": 1}
    values.update(changes)
    return {"sku": "A", "effective_values": values, "incoming": [],
            "quality_run_id": 1, "cleaning_run_id": 2, "snapshot_id": 3}


def _forecast(quantity=10, days=30):
    start = date.fromisoformat(AS_OF)
    return {"basis": "regular_unreserved", "version": "manual-1", "source": "pytest",
            "author": "pytest", "reason": "Проверка расчёта", "granularity": "daily", "unit": "шт",
            "values": [{"date": (start + timedelta(days=index)).isoformat(), "quantity": quantity}
                       for index in range(1, days + 1)]}


def _arrival(day, quantity, *, factor=1):
    return {"eta": {"eta": day, "meaning": "expected"}, "accounting_quantity": quantity * factor,
            "quantity": quantity, "source_kind": "incoming", "sheet": "Data", "cell": "F2"}


def test_formula_rounding_and_no_moq_order_at_zero():
    prepared = _prepared()
    prepared["incoming"] = [_arrival("2026-10-01", 100)]
    result = calculate_replenishment(prepared, _forecast(), as_of=AS_OF)
    explanation = result["explanation"]
    assert (explanation["regular_forecast"], explanation["safety_stock"],
            explanation["available_stock"], explanation["timely_incoming"]) == (300, 60, 90, 100)
    assert explanation["raw_need_accounting"] == 170
    assert result["order_quantity"] == 180
    assert explanation["rounding_added_accounting"] == 10
    zero = calculate_replenishment(_prepared(current_stock=500, minimum_order=100),
                                   _forecast(quantity=0), as_of=AS_OF)
    assert zero["order_quantity"] == 0


def test_reserve_and_project_commitment_are_counted_once():
    base = calculate_replenishment(_prepared(), _forecast(), as_of=AS_OF)
    assert base["explanation"]["available_stock"] == 90
    assert base["explanation"]["raw_need_accounting"] == 270
    obligations = [{"due": "2026-09-25", "quantity": 10, "reservation": "already_reserved", "source": "project-1"},
                   {"due": "2026-09-26", "quantity": 20, "reservation": "unreserved", "source": "project-2"}]
    result = calculate_replenishment(_prepared(), _forecast(), as_of=AS_OF,
                                     project_commitments=obligations)
    assert result["explanation"]["unreserved_project_due"] == 20
    assert result["explanation"]["raw_need_accounting"] == 290


def test_delayed_arrival_changes_urgent_risk_and_monthly_precision():
    early = _prepared()
    early["incoming"] = [_arrival("2026-09-25", 100)]
    late = _prepared()
    late["incoming"] = [_arrival("2026-10-15", 100)]
    early_result = calculate_replenishment(early, _forecast(), as_of=AS_OF)
    late_result = calculate_replenishment(late, _forecast(), as_of=AS_OF)
    assert early_result["urgent_problem"] is False
    assert late_result["urgent_problem"] is True
    assert late_result["explanation"]["urgent_risk_date"] < late_result["explanation"]["new_order_eta"]
    monthly = {**_forecast(), "granularity": "monthly", "values": [
        {"period": "2026-09-01", "quantity": 300},
        {"period": "2026-10-01", "quantity": 310}]}
    result = calculate_replenishment(_prepared(), monthly, as_of=AS_OF)
    assert result["explanation"]["risk_date_precision"].startswith("approximate")
    assert len(result["calendar"]) == 30


def test_only_multiple_does_not_invent_moq_and_five_packs_are_twenty_units():
    prepared = _prepared(minimum_order=None, purchase_unit="компл", unit_factor=4)
    prepared["incoming"] = [_arrival("2026-10-01", 5, factor=4)]
    result = calculate_replenishment(prepared, _forecast(), as_of=AS_OF)
    explanation = result["explanation"]
    assert explanation["timely_incoming"] == 20
    assert explanation["minimum_order"] is None
    assert result["status"] == "incomplete_constraints"
    assert result["order_quantity"] == 72  # 250 / 4 = 62.5; next multiple of 12.


def test_missing_multiple_is_incomplete_and_overdue_is_not_stock():
    prepared = _prepared(order_multiple=None)
    prepared["incoming"] = [_arrival("2026-09-20", 100),
                            {"eta": None, "accounting_quantity": 50, "cell": "F3"}]
    result = calculate_replenishment(prepared, _forecast(), as_of=AS_OF)
    assert result["status"] == "incomplete_constraints"
    assert result["order_quantity"] is None
    assert result["explanation"]["timely_incoming"] == 0
    assert {row["reason"] for row in result["explanation"]["excluded_arrivals"]} == {
        "overdue_not_received", "unconfirmed"}


def test_full_headless_run_is_reproducible_by_run_id(tmp_path, monkeypatch, capsys):
    dataset = create_synthetic_dataset(tmp_path / "synthetic", seed=20260923)
    database = dataset["database_path"]
    snapshot = next(row for row in dataset["snapshots"] if row["supplier"] == "Systeme Electric")
    clean = run_cleaning(database, snapshot["snapshot_id"], "2026-01-01")
    forecast_config = {
        "horizon_months": 12, "warehouse_scope": "source_report",
        "growth_application": {"mode": "replace_trend", "start": "2026-01-01",
                               "end": "2026-12-01", "scope": "source_report",
                               "status": "scenario", "reason": "Интеграционный тест", "author": "pytest"},
        "short_history_fallback": None,
        "seasonal_aggregate_policy": {"use": False, "unit_status": "unknown",
                                      "reason": "Единица не подтверждена", "author": "pytest"},
    }
    forecast_run = run_forecast(database, snapshot["run_id"], clean["run_id"],
                                "SYN-A-001", forecast_config, allow_scenario=True)
    payload = {"quality_run_id": snapshot["run_id"], "cleaning_run_id": clean["run_id"],
               "as_of": "2026-01-01", "supplier": "Systeme Electric",
               "warehouse": "all_selected_warehouses", "items": [
                   {"sku": "SYN-A-001", "category_code": "SYN-CAT-01",
                    "forecast_run_id": forecast_run["run_id"],
                    "project_commitments": []},
                   {"sku": "SYN-A-006", "category_code": "SYN-CAT-01",
                    "forecast": {**_forecast(), "granularity": "monthly", "values": [
                        {"period": "2026-01-01", "quantity": 124},
                        {"period": "2026-02-01", "quantity": 112}]},
                    "project_commitments": []}]}
    report = run_replenishment(database, payload)
    assert report["as_of"] == "2026-01-01"
    assert report["summary"]["items"] == 2
    assert next(row for row in report["items"] if row["sku"] == "SYN-A-001")["status"] == "scenario"
    assert next(row for row in report["items"] if row["sku"] == "SYN-A-001")["explanation"]["sources"]["forecast_run_id"] == forecast_run["run_id"]
    assert run_replenishment(database, payload)["run_id"] == report["run_id"]
    base_before = replenishment_report(database, report["run_id"])
    scenario_payload = deepcopy(payload)
    scenario_payload["scenario"] = {
        "base_run_id": report["run_id"], "demand_factor": 1.2,
        "arrival_delay_days": 7, "author": "pytest",
        "reason": "Проверка UI-сценария",
    }
    scenario = run_replenishment(database, scenario_payload)
    assert scenario["run_id"] != report["run_id"]
    assert replenishment_report(database, report["run_id"]) == base_before
    base_item = next(row for row in report["items"] if row["sku"] == "SYN-A-001")
    scenario_item = next(row for row in scenario["items"] if row["sku"] == "SYN-A-001")
    assert scenario_item["scenario_parameters"] == scenario_payload["scenario"]
    assert scenario_item["explanation"]["regular_forecast"] == pytest.approx(
        base_item["explanation"]["regular_forecast"] * 1.2
    )
    assert scenario_item["explanation"]["sources"]["forecast_run_id"] == forecast_run["run_id"]
    with pytest.raises(ValueError, match="автор и основание"):
        run_replenishment(database, {
            **scenario_payload,
            "scenario": {**scenario_payload["scenario"], "reason": ""},
        })
    with pytest.raises(ValueError, match="одному снимку и срезу"):
        run_replenishment(database, {**payload, "as_of": AS_OF})
    selected = replenishment_report(database, report["run_id"], sku="SYN-A-001")
    assert selected["items"][0]["explanation"]["raw_need_accounting"] == max(
        0, selected["items"][0]["explanation"]["regular_forecast"] +
        selected["items"][0]["explanation"]["safety_stock"] -
        selected["items"][0]["explanation"]["available_stock"] -
        selected["items"][0]["explanation"]["timely_incoming"])
    source_file = tmp_path / "scenario.json"
    source_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(Path(database).parent))
    assert main(["replenish", "--file", str(source_file)]) == 0
    assert json.loads(capsys.readouterr().out)["run_id"] == report["run_id"]
    assert main(["replenishment-report", "--run", str(report["run_id"]), "--sku", "SYN-A-001"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["sku"] == "SYN-A-001"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_from_stage6_preserves_source_and_lost_demand(tmp_path):
    dataset = create_synthetic_dataset(tmp_path / "migration", seed=20260923)
    database = Path(dataset["database_path"])
    snapshot = next(row for row in dataset["snapshots"] if row["supplier"] == "Systeme Electric")
    cleaned = run_cleaning(database, snapshot["snapshot_id"], "2026-01-01")
    lost = run_lost_demand(database, cleaned["run_id"], "SYN-A-001")
    with sqlite3.connect(database) as connection:
        before = connection.execute("SELECT COUNT(*),SUM(quantity) FROM transactions").fetchone()
        for table in ("order_events", "order_items", "order_versions", "order_projects",
                      "replenishment_items", "replenishment_runs", "forecast_points", "forecast_runs"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=6")
    assert initialize_database(database).schema_version == 9
    assert initialize_database(database).schema_version == 9
    assert lost_demand_report(database, lost["run_id"])["sku"] == "SYN-A-001"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*),SUM(quantity) FROM transactions").fetchone() == before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
