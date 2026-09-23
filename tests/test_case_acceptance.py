"""Stage 12: case-level acceptance over persisted services and fixed oracles.

Thresholds come from the unchanged stage-5 validation spec. Paired sources are
new, isolated synthetic databases; these tests never rewrite imported data.
"""

from copy import deepcopy
import json
from math import isfinite
from pathlib import Path

import pytest

from hackalem.config import PROJECT_ROOT
from hackalem.services.cleaning import cleaning_report, run_cleaning
from hackalem.services.forecasting import forecast_config_template, run_forecast
from hackalem.services.lost_demand import run_lost_demand
from hackalem.services.replenishment import replenishment_report, run_replenishment
from hackalem.services.synthetic import (
    _populate, create_synthetic_dataset, evaluate_forecasts, synthetic_report,
)
from hackalem.synthetic.generator import generate_dataset


AS_OF = "2026-01-01"
SPEC = json.loads((PROJECT_ROOT / "hackalem/synthetic/validation_spec.json").read_text(encoding="utf-8"))
ACCEPTANCE = {row["id"]: row["acceptance"] for row in SPEC["requirements"]}
ARITHMETIC_TOLERANCE = 1e-9


def _config():
    config = forecast_config_template(AS_OF)
    config["growth_application"].update(
        author="acceptance-test", reason="Fixed synthetic business condition from stage-5 specification."
    )
    return config


def _calculate(database, snapshot, cleaned, sku, *, lost_demand_run_id=None):
    forecast = run_forecast(
        database, snapshot["run_id"], cleaned["run_id"], sku, _config(),
        allow_scenario=True, lost_demand_run_id=lost_demand_run_id,
    )
    payload = {
        "quality_run_id": snapshot["run_id"], "cleaning_run_id": cleaned["run_id"],
        "as_of": AS_OF, "supplier": snapshot["supplier"],
        "warehouse": "all_selected_warehouses",
        "items": [{"sku": sku, "category_code": forecast["summary"]["category"]["code"],
                   "forecast_run_id": forecast["run_id"], "project_commitments": []}],
    }
    result = run_replenishment(database, payload)
    item = result["items"][0]
    assert item["status"] == "scenario", item
    assert item["scenario"] is True
    assert replenishment_report(database, result["run_id"]) == result
    return forecast, result, item


def _observed_subset(bundle, sku):
    """Only model-observable fields enter the synthetic persistence adapter."""
    observed = bundle["observed"]
    return {
        "manifest": deepcopy(bundle["manifest"]),
        "observed": {
            key: ({sku: deepcopy(value[sku])} if key == "parameters" else
                  [deepcopy(row) for row in value if row["sku"] == sku])
            for key, value in observed.items()
        },
    }


@pytest.fixture(scope="module")
def case_dataset(tmp_path_factory):
    dataset = create_synthetic_dataset(tmp_path_factory.mktemp("case-acceptance"), seed=20260923)
    snapshot = next(row for row in dataset["snapshots"] if row["supplier"] == "Systeme Electric")
    cleaned = run_cleaning(
        dataset["database_path"], snapshot["snapshot_id"], AS_OF, policy="exclude_high_confidence",
    )
    return dataset, snapshot, cleaned


@pytest.fixture(scope="module")
def sensitivity_results(tmp_path_factory):
    """Independent arithmetic example: 300 + 60 - 90 - 100 = 170 -> 180.

    All sources use 24 months of observed daily sales and the real service
    chain. Changes are large enough to remain visible after a multiple of 12.
    """
    source = _observed_subset(generate_dataset(20260923), "SYN-A-001")
    observed = source["observed"]
    for transaction in observed["transactions"]:
        transaction["quantity"] = 10
    observed["parameters"]["SYN-A-001"].update(
        lead_time_days=15, review_period_days=15, order_multiple=12,
        stock_policy={"mode": "stock", "safety_days": 6}, no_open_orders=False,
    )
    observed["incoming"] = [{
        "sku": "SYN-A-001", "order_number": "SYN-SENSITIVITY-1",
        "order_date": "2025-12-15", "eta": "2026-01-04", "quantity": 100, "unit": "шт",
    }]
    root = tmp_path_factory.mktemp("sensitivity-sources")
    results = {}
    for variant in ("base", "sales", "stock", "incoming", "category", "growth"):
        paired = deepcopy(source)
        paired["manifest"]["dataset_id"] += "-acceptance-" + variant
        data = paired["observed"]
        parameters = data["parameters"]["SYN-A-001"]
        if variant == "sales":
            for transaction in data["transactions"]:
                transaction["quantity"] = 12
        elif variant == "stock":
            parameters["current_stock"] = 124
        elif variant == "incoming":
            data["incoming"][0]["quantity"] = 124
        elif variant == "category":
            parameters.update(category_code="SYN-CAT-ON-DEMAND",
                              category_label="Synthetic on-demand category",
                              stock_policy={"mode": "on_demand", "safety_days": 0})
        elif variant == "growth":
            parameters["business_growth"] = 0.2
        database = root / variant / "hackalem.sqlite3"
        snapshot = _populate(database, paired)[0]
        cleaned = run_cleaning(database, snapshot["snapshot_id"], AS_OF)
        results[variant] = _calculate(database, snapshot, cleaned, "SYN-A-001")
    return results


@pytest.mark.parametrize("source,component,value,raw_need,order", [
    ("sales", "regular_forecast", 360, 242, 252),
    ("stock", "available_stock", 114, 146, 156),
    ("incoming", "timely_incoming", 124, 146, 156),
    ("category", "safety_stock", 0, 110, 120),
    ("growth", "regular_forecast", 360, 242, 252),
])
def test_each_business_input_changes_need_and_rounded_order(
    sensitivity_results, source, component, value, raw_need, order,
):
    base = sensitivity_results["base"][2]
    assert base["explanation"]["raw_need_accounting"] == pytest.approx(170, abs=ARITHMETIC_TOLERANCE)
    assert base["order_quantity"] == 180
    forecast, _, changed = sensitivity_results[source]
    assert changed["explanation"][component] == pytest.approx(value, abs=ARITHMETIC_TOLERANCE)
    assert changed["explanation"]["raw_need_accounting"] == pytest.approx(raw_need, abs=ARITHMETIC_TOLERANCE)
    assert changed["order_quantity"] == order
    assert changed["order_quantity"] != base["order_quantity"]
    assert changed["explanation"]["sources"]["forecast_run_id"] == forecast["run_id"]
    if source == "growth":
        assert {point["growth_applied_count"] for point in forecast["summary"]["forecasts"]} == {1}


def test_seasonality_and_growth_pass_frozen_held_out_thresholds(case_dataset):
    dataset, snapshot, cleaned = case_dataset
    forecasts = [_calculate(dataset["database_path"], snapshot, cleaned, sku)[0]
                 for sku in ("SYN-A-001", "SYN-A-002", "SYN-A-003")]
    evaluation = evaluate_forecasts(dataset["dataset_dir"], [row["run_id"] for row in forecasts])
    assert evaluation["passed"] is True
    thresholds = ACCEPTANCE["seasonality_growth"]
    for row in evaluation["reports"]:
        assert row["metrics"]["count"] == 12
        assert row["metrics"]["finite_nonnegative_fraction"] == thresholds["finite_nonnegative_forecast_fraction"]
        assert isfinite(row["metrics"]["wape"])
        if row["scenario"] == "seasonal":
            predicted = int(row["metrics"]["predicted_peak_month"][5:7])
            actual = int(row["metrics"]["actual_peak_month"][5:7])
            distance = abs(predicted - actual)
            assert min(distance, 12 - distance) <= thresholds["seasonal_peak_month_circular_distance_max"]
    print("case_forecast_metrics=" + json.dumps(
        {row["scenario"]: row["metrics"] for row in evaluation["reports"]}, sort_keys=True,
    ))


def test_stockout_correction_improves_actual_forecast_and_is_not_current_backlog(case_dataset):
    dataset, snapshot, cleaned = case_dataset
    database = dataset["database_path"]
    raw_forecast, _, raw_item = _calculate(database, snapshot, cleaned, "SYN-A-009")
    lost = run_lost_demand(database, cleaned["run_id"], "SYN-A-009")
    adjusted_forecast, _, adjusted_item = _calculate(
        database, snapshot, cleaned, "SYN-A-009", lost_demand_run_id=lost["run_id"],
    )
    # Truth is loaded only by this assertion, after both models have finished.
    truth = json.loads((Path(dataset["dataset_dir"]) / "validation/truth.json").read_text(encoding="utf-8"))
    target = sum(row["regular_demand"] for row in truth["daily"]
                 if row["sku"] == "SYN-A-009" and row["date"].startswith("2025-08"))
    raw = next(row["prediction"] for row in raw_forecast["summary"]["backtest"]
               if row["period"] == "2025-08-01")
    corrected = next(row["prediction"] for row in adjusted_forecast["summary"]["backtest"]
                     if row["period"] == "2025-08-01")
    assert corrected > raw
    assert abs(corrected - target) / target <= ACCEPTANCE["lost_demand"]["corrected_month_relative_error_max"]
    assert 1 - abs(corrected - target) / abs(raw - target) >= ACCEPTANCE["lost_demand"]["absolute_error_reduction_vs_observed_min"]
    metrics = evaluate_forecasts(dataset["dataset_dir"], [raw_forecast["run_id"], adjusted_forecast["run_id"]])["reports"]
    assert metrics[1]["metrics"]["wape"] < metrics[0]["metrics"]["wape"]
    # January demand is identical in both histories: the lost August demand
    # must not become an extra backlog added to today's January order.
    assert adjusted_item["explanation"]["raw_need_accounting"] == raw_item["explanation"]["raw_need_accounting"]
    assert adjusted_item["order_quantity"] == raw_item["order_quantity"]
    assert adjusted_item["explanation"]["unreserved_project_due"] == 0
    assert adjusted_forecast["summary"]["lost_demand_input"]["historical_lost_demand_as_current_backlog"] == 0
    assert synthetic_report(dataset["dataset_dir"])["validation"]["model_fingerprint"] == "ok"
    print("case_stockout_metrics=" + json.dumps({
        "raw_prediction": raw, "adjusted_prediction": corrected, "target": target,
        "raw_wape": metrics[0]["metrics"]["wape"], "adjusted_wape": metrics[1]["metrics"]["wape"],
    }, sort_keys=True))


def test_one_off_client_does_not_inflate_regular_replenishment(case_dataset, tmp_path):
    dataset, snapshot, cleaned = case_dataset
    original_forecast, _, original_item = _calculate(dataset["database_path"], snapshot, cleaned, "SYN-A-006")
    paired = _observed_subset(generate_dataset(20260923), "SYN-A-006")
    paired["manifest"]["dataset_id"] += "-without-one-off"
    transactions = paired["observed"]["transactions"]
    one_off = [row for row in transactions if row["customer_id"] == "SYN-C-901"]
    assert len(one_off) == 1 and one_off[0]["quantity"] == ACCEPTANCE["one_off_orders"]["true_one_off_quantity"]
    paired["observed"]["transactions"] = [row for row in transactions if row not in one_off]
    database = tmp_path / "paired" / "hackalem.sqlite3"
    paired_snapshot = _populate(database, paired)[0]
    paired_cleaned = run_cleaning(database, paired_snapshot["snapshot_id"], AS_OF, policy="exclude_high_confidence")
    baseline_forecast, _, baseline_item = _calculate(database, paired_snapshot, paired_cleaned, "SYN-A-006")
    maximum_change = ACCEPTANCE["one_off_orders"]["paired_regular_forecast_or_need_relative_change_max"]
    for original, baseline in zip(original_forecast["summary"]["forecasts"], baseline_forecast["summary"]["forecasts"], strict=True):
        assert baseline["prediction"] > 0
        assert abs(original["prediction"] - baseline["prediction"]) / baseline["prediction"] <= maximum_change
    assert baseline_item["explanation"]["raw_need_accounting"] > 0
    assert abs(original_item["explanation"]["raw_need_accounting"] - baseline_item["explanation"]["raw_need_accounting"]) / baseline_item["explanation"]["raw_need_accounting"] <= maximum_change
    assert abs(original_item["order_quantity"] - baseline_item["order_quantity"]) / baseline_item["order_quantity"] <= maximum_change
    assert synthetic_report(dataset["dataset_dir"])["validation"]["model_fingerprint"] == "ok"
    print("case_one_off_metrics=" + json.dumps({
        "with_one_off_raw_need": original_item["explanation"]["raw_need_accounting"],
        "without_one_off_raw_need": baseline_item["explanation"]["raw_need_accounting"],
        "with_one_off_order": original_item["order_quantity"],
        "without_one_off_order": baseline_item["order_quantity"],
    }, sort_keys=True))


def test_repeated_large_purchases_and_growth_are_retained_through_forecast(case_dataset):
    dataset, snapshot, cleaned = case_dataset
    for sku, threshold in (("SYN-A-007", "repeated_large_retained_fraction_min"),
                           ("SYN-A-003", "growth_regular_quantity_retained_fraction_min")):
        cleaning = cleaning_report(dataset["database_path"], cleaned["run_id"], sku=sku, limit=10000)
        original = sum(row["raw_signed_quantity"] for row in cleaning["months"])
        retained = sum(row["regular_quantity"] for row in cleaning["months"])
        assert retained / original >= ACCEPTANCE["one_off_orders"][threshold]
        forecast, _, item = _calculate(dataset["database_path"], snapshot, cleaned, sku)
        assert item["order_quantity"] > 0
        if sku == "SYN-A-007":
            large = [row for row in cleaning["documents"] if row["customer_id"] == "SYN-C-902"]
            assert len(large) == 24
            assert sum(row["regular_quantity"] for row in large) == 24 * 80
        else:
            predictions = [row["prediction"] for row in forecast["summary"]["forecasts"]]
            assert sum(predictions[-3:]) > sum(predictions[:3])
        print(f"case_retention_{sku}=" + json.dumps({"raw": original, "retained": retained,
                                                     "order": item["order_quantity"]}, sort_keys=True))
