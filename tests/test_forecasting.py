"""Stage 8: leakage-safe simple models, explicit growth and synthetic metrics."""

import math
import sqlite3
import json
import shutil
from pathlib import Path

import pytest

from hackalem.domain.forecasting import add_month, forecast_monthly
from hackalem.__main__ import main
from hackalem.services.cleaning import run_cleaning
from hackalem.services.forecasting import forecast_report, run_forecast
from hackalem.services.lost_demand import run_lost_demand
from hackalem.services.synthetic import create_synthetic_dataset, evaluate_forecasts, synthetic_report
from hackalem.storage import SCHEMA_VERSION, initialize_database


def _config(start="2026-01-01", *, fallback=None, growth_mode="replace_trend"):
    return {
        "horizon_months": 12,
        "warehouse_scope": "source_report",
        "growth_application": {
            "mode": growth_mode, "start": start, "end": add_month(start, 11),
            "scope": "source_report", "status": "scenario",
            "reason": "Фиксированное условие тестового сценария.", "author": "test",
        },
        "short_history_fallback": fallback,
        "seasonal_aggregate_policy": {
            "use": False, "unit_status": "unknown",
            "reason": "Единица агрегата не установлена.", "author": "test",
        },
    }


def _history(values, start="2024-01-01"):
    return [{"period": add_month(start, index), "quantity": value, "state": "value"}
            for index, value in enumerate(values)]


def test_simple_models_reproduce_seasonality_growth_and_compare_baselines():
    seasonal = [186, 168, 248, 300, 434, 540, 620, 558, 390, 310, 240, 186] * 2
    growth = [186 + 31 * index for index in range(24)]

    seasonal_result = forecast_monthly(_history(seasonal), "2026-01-01", _config(),
                                       business_growth=0, category_code="C")
    growth_result = forecast_monthly(_history(growth), "2026-01-01", _config(),
                                     business_growth=0, category_code="C")

    assert seasonal_result["selected_model"] == "seasonal_analog"
    assert seasonal_result["metrics"]["wape"] == 0
    assert [row["prediction"] for row in seasonal_result["forecasts"]] == seasonal[-12:]
    assert growth_result["selected_model"] == "damped_level_trend"
    assert growth_result["metrics"]["wape"] < 0.02
    assert growth_result["model_selection"]["wape_difference_to_best_baseline"] < 0
    assert growth_result["forecasts"][-1]["prediction"] > growth_result["forecasts"][0]["prediction"]
    assert all(math.isfinite(row["prediction"]) and row["prediction"] >= 0
               for row in seasonal_result["forecasts"] + growth_result["forecasts"])


def test_growth_is_applied_once_and_short_history_requires_explicit_fallback():
    values = [100.0] * 24
    result = forecast_monthly(_history(values), "2026-01-01", _config(),
                              business_growth=0.2, category_code="C")
    assert all(row["base_prediction"] == 100 and row["prediction"] == 120
               and row["growth_applied_count"] == 1 for row in result["forecasts"])
    short = forecast_monthly(_history([10, 11, 12]), "2024-04-01", _config("2024-04-01"),
                             business_growth=0, category_code="NEW")
    assert short["status"] == "insufficient_history" and short["forecasts"] == []
    fallback = {"category_code": "NEW", "monthly_demand": 15, "status": "scenario",
                "reason": "Явный тестовый fallback категории.", "author": "test"}
    short = forecast_monthly(_history([10, 11, 12]), "2024-04-01",
                             _config("2024-04-01", fallback=fallback),
                             business_growth=0, category_code="NEW")
    assert short["selected_model"] == "category_fallback"
    assert short["model_selection"]["measured"] is False
    assert {row["prediction"] for row in short["forecasts"]} == {15.0}


def test_unknown_months_and_unconfirmed_seasonal_aggregates_are_not_silently_used():
    broken = _history([10] * 12)
    broken.pop(5)
    with pytest.raises(ValueError, match="разрыв"):
        forecast_monthly(broken, "2025-01-01", _config("2025-01-01"),
                         business_growth=0, category_code="C")
    config = _config()
    config["seasonal_aggregate_policy"] = {
        "use": True, "unit_status": "unknown", "reason": "test", "author": "test",
    }
    with pytest.raises(ValueError, match="нельзя использовать"):
        forecast_monthly(_history([10] * 24), "2026-01-01", config,
                         business_growth=0, category_code="C")


@pytest.fixture(scope="module")
def forecast_dataset(tmp_path_factory):
    dataset = create_synthetic_dataset(tmp_path_factory.mktemp("forecast-synthetic"), seed=20260923)
    database = dataset["database_path"]
    snapshot = next(row for row in dataset["snapshots"] if row["supplier"] == "Systeme Electric")
    cleaning = run_cleaning(database, snapshot["snapshot_id"], dataset["manifest"]["as_of"])
    runs = {}
    for sku in ("SYN-A-001", "SYN-A-002", "SYN-A-003"):
        report = run_forecast(database, snapshot["run_id"], cleaning["run_id"], sku,
                              _config(), allow_scenario=True)
        runs[sku] = report["run_id"]
    return dataset, snapshot, cleaning, runs


def test_versioned_forecasts_are_repeatable_and_truth_is_evaluator_only(forecast_dataset):
    dataset, snapshot, cleaning, runs = forecast_dataset
    database = dataset["database_path"]
    repeated = run_forecast(database, snapshot["run_id"], cleaning["run_id"], "SYN-A-001",
                            _config(), allow_scenario=True)
    assert repeated["run_id"] == runs["SYN-A-001"]
    report = forecast_report(database, runs["SYN-A-002"])
    assert report["summary"]["training_protocol"] == {
        "current_month_excluded": True,
        "outer_origins": [f"2025-{month:02d}-01" for month in range(1, 13)],
        "cleaning_refitted_per_origin": True,
        "model_parameters_refitted_per_origin": True,
        "manual_decisions_replayed": False,
        "lost_demand_adjustment_is_causal": False,
        "oracle_used_by_model": False,
    }
    assert report["summary"]["seasonal_aggregate_decision"]["use"] is False
    evaluation = evaluate_forecasts(dataset["dataset_dir"], list(runs.values()))
    assert evaluation["passed"] is True
    assert evaluation["forecast_model_oracle_access"] is False
    assert {row["scenario"] for row in evaluation["reports"]} == {"stable", "seasonal", "growth"}
    assert all(row["metrics"]["wape"] <= 0.15 for row in evaluation["reports"])
    assert synthetic_report(dataset["dataset_dir"])["validation"]["model_fingerprint"] == "ok"
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_new_product_fallback_is_visible_and_never_claims_measured_superiority(forecast_dataset):
    dataset, snapshot, cleaning, _ = forecast_dataset
    fallback = {"category_code": "SYN-CAT-01", "monthly_demand": 120,
                "status": "scenario", "reason": "Явный fallback синтетической категории.",
                "author": "test"}
    report = run_forecast(dataset["database_path"], snapshot["run_id"], cleaning["run_id"],
                          "SYN-A-005", _config(fallback=fallback), allow_scenario=True)
    assert report["selected_model"] == "category_fallback"
    assert report["summary"]["history_months"] == 3
    assert report["summary"]["model_selection"]["measured"] is False
    assert any("Короткая история" in text for text in report["summary"]["limitations"])


def test_lost_demand_version_feeds_forecast_without_becoming_backlog(forecast_dataset):
    dataset, snapshot, cleaning, _ = forecast_dataset
    lost = run_lost_demand(dataset["database_path"], cleaning["run_id"], "SYN-A-009")
    report = run_forecast(
        dataset["database_path"], snapshot["run_id"], cleaning["run_id"], "SYN-A-009",
        _config(), allow_scenario=True, lost_demand_run_id=lost["run_id"],
    )
    assert report["lost_demand_run_id"] == lost["run_id"]
    assert report["summary"]["lost_demand_input"] == {
        "run_id": lost["run_id"], "applied": True,
        "historical_lost_demand_as_current_backlog": 0,
    }
    assert report["summary"]["training_protocol"]["lost_demand_adjustment_is_causal"] is True


def test_forecast_cli_reuses_version_and_reports_json(forecast_dataset, tmp_path, monkeypatch, capsys):
    dataset, snapshot, cleaning, runs = forecast_dataset
    config_path = tmp_path / "forecast.json"
    config_path.write_text(json.dumps(_config(), ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(Path(dataset["database_path"]).parent))
    assert main(["forecast", "--quality-run", str(snapshot["run_id"]),
                 "--cleaning-run", str(cleaning["run_id"]), "--sku", "SYN-A-001",
                 "--config", str(config_path), "--allow-scenario"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["run_id"] == runs["SYN-A-001"]
    assert main(["forecast-report", "--run", str(output["run_id"])]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["selected_model"] is not None


def test_published_forecast_schema6_migrates_without_losing_runs(forecast_dataset, tmp_path):
    dataset, _, _, runs = forecast_dataset
    target = tmp_path / "legacy-forecast.sqlite3"
    shutil.copyfile(dataset["database_path"], target)
    with sqlite3.connect(target) as connection:
        for table in ("order_events", "order_items", "order_versions", "order_projects",
                      "replenishment_items", "replenishment_runs",
                      "lost_demand_days", "lost_demand_months", "lost_demand_runs"):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("ALTER TABLE forecast_runs DROP COLUMN lost_demand_run_id")
        connection.execute("PRAGMA user_version=6")
    assert initialize_database(target).schema_version == SCHEMA_VERSION == 9
    assert initialize_database(target).schema_version == SCHEMA_VERSION
    assert forecast_report(target, runs["SYN-A-001"])["sku"] == "SYN-A-001"
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT lost_demand_run_id FROM forecast_runs LIMIT 1").fetchone()[0] is None
