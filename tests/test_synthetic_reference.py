"""Check the frozen manual oracle with independent arithmetic, not a model."""

import json
import math

from hackalem.config import PROJECT_ROOT
from hackalem.synthetic.generator import generate_dataset


def test_manual_reference_arithmetic_and_scenario_contract():
    spec = json.loads((PROJECT_ROOT / "hackalem/synthetic/validation_spec.json").read_text(encoding="utf-8"))
    assert spec["version"] == "synthetic-validation-1"
    assert len(spec["requirements"]) == 5
    assert len({r["id"] for r in spec["requirements"]}) == 5
    assert all(r["status"].startswith("pending_stage_") for r in spec["requirements"])
    cases = {c["id"]: c for c in spec["manual_cases"]}
    assert len(cases) == 10
    checks = []

    def check(name, value, expected):
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            assert math.isclose(value, expected, rel_tol=0, abs_tol=1e-9), (name, value, expected)
        else:
            assert value == expected, (name, value, expected)
        checks.append(name)

    x, y = cases["M01"]["inputs"], cases["M01"]["expected"]
    available = x["current_stock"] - x["reserved_stock"]
    check("M01.available", available, y["available_stock"])
    check("M01.need", x["constant_daily_demand"] * (x["lead_time_days"] + x["review_period_days"]) - available, y["order_purchase"])
    x, y = cases["M02"]["inputs"], cases["M02"]["expected"]
    h = x["lead_time_days"] + x["review_period_days"]
    for name in ("timely", "late"):
        eta = x[name + "_eta_day"]
        counted = x["incoming_quantity_accounting"] if eta <= h else 0
        check("M02." + name + ".need", x["constant_daily_demand"] * h - x["current_stock"] - counted, y[name + "_net_need"])
        balance = x["current_stock"]
        first = None
        day4_balance = None
        for day in range(1, h + 1):
            balance += x["incoming_quantity_accounting"] if day == eta else 0
            balance -= x["constant_daily_demand"]
            if balance < 0 and first is None:
                first = day
            if day == 4:
                day4_balance = balance
        check("M02." + name + ".first_deficit", first, y[name + "_first_deficit_day_without_new_order"])
        check("M02." + name + ".before_day5", max(0, -day4_balance), y[name + "_deficit_before_new_order_day_5"])
    x = cases["M03"]["inputs"]
    for y in cases["M03"]["expected"]["cases"]:
        q = math.ceil(max(y["need_accounting"] / x["unit_factor"], x["minimum_order_purchase"]) / x["order_multiple_purchase"]) * x["order_multiple_purchase"]
        check("M03.purchase." + str(y["need_accounting"]), q, y["order_purchase"])
        check("M03.accounting." + str(y["need_accounting"]), q * x["unit_factor"], y["order_accounting"])
    x, y = cases["M04"]["inputs"], cases["M04"]["expected"]
    check("M04.need", max(0, x["horizon_demand"] + x["safety_stock"] - x["available_stock"] - x["incoming_in_horizon"]), y["order_purchase"])
    x, y = cases["M05"]["inputs"], cases["M05"]["expected"]
    daily = x["observed_sales"] / x["confirmed_available_days"]
    check("M05.lost", daily * x["confirmed_unavailable_days"], y["estimated_lost_demand"])
    check("M05.order", daily * x["next_horizon_days"] - x["available_stock"] - x["incoming_in_horizon"], y["order_purchase"])
    x, y = cases["M06"]["inputs"], cases["M06"]["expected"]
    regular = x["period_days"] * x["regular_daily_demand"]
    check("M06.raw", regular + x["one_off_quantity"], y["one_off_case_raw_month"])
    check("M06.regular", regular, y["one_off_case_regular_month"])
    check("M06.repeated", regular + x["repeated_monthly_large_quantity"], y["repeated_case_regular_month"])
    x, y = cases["M07"]["inputs"], cases["M07"]["expected"]
    b = x["baseline"]
    base = b["constant_daily_demand"] * (b["lead_time_days"] + b["review_period_days"]) - (b["current_stock"] - b["reserved_stock"]) - b["incoming_in_horizon"]
    check("M07.baseline", base, y["baseline_order"])
    for mutation in x["mutations"]:
        z = dict(b)
        z.update(mutation["replace"])
        demand = z["constant_daily_demand"] * (1 + z["business_growth"])
        need = demand * (z["lead_time_days"] + z["review_period_days"] + z["safety_days"]) - (z["current_stock"] - z["reserved_stock"]) - z["incoming_in_horizon"]
        check("M07." + mutation["factor"], need, y["orders_by_factor"][mutation["factor"]])
    x, y = cases["M08"]["inputs"], cases["M08"]["expected"]
    check("M08.signed", x["transactions_positive_quantity"] + x["transactions_return_quantity"], y["chosen_signed_audit"])
    check("M08.gross", x["transactions_positive_quantity"], y["regular_positive_demand"])
    x, y = cases["M09"]["inputs"], cases["M09"]["expected"]
    available = x["current_stock_accounting"] - x["reserved_stock_accounting"]
    for kind, factor in (("purchase", x["unit_factor"]), ("accounting", 1)):
        expected = y["if_incoming_unit_" + kind]
        incoming = x["incoming_raw_quantity"] * factor
        need = x["horizon_demand_accounting"] - available - incoming
        check("M09." + kind + ".incoming", incoming, expected["incoming_accounting"])
        check("M09." + kind + ".need", need, expected["net_need_accounting"])
        check("M09." + kind + ".purchase", math.ceil(need / x["unit_factor"]), expected["order_purchase"])
    check("M10.order", cases["M10"]["expected"]["order_purchase"], None)

    dataset = generate_dataset(spec["dataset_contract"]["default_seed"])
    ids = {s["id"] for s in dataset["scenarios"]}
    declared = set(next(c for c in spec["dataset_checks"] if c["id"] == "D04")["expected"]["scenario_ids"])
    check("registry.scenario_ids", sorted(ids), sorted(declared))
    assert all(set(r["scenarios"]) <= ids for r in spec["requirements"])
    check("registry.sku_count", len(dataset["observed"]["products"]), spec["dataset_contract"]["sku_count"])
    check("registry.truth_days", len(dataset["truth"]["daily"]), 17 * 731)
    check("registry.history_days", len({d["date"] for d in dataset["truth"]["daily"]}), spec["dataset_contract"]["history_days"])
    stockout_sku = next(s["sku"] for s in dataset["scenarios"] if s["id"] == "stockout")
    for month in ("2024-08", "2025-08"):
        days = [d for d in dataset["truth"]["daily"] if d["sku"] == stockout_sku and d["date"].startswith(month)]
        check(month + ".regular", sum(d["regular_demand"] for d in days), 217)
        check(month + ".observed", sum(d["observed_sales"] for d in days), 119)
        check(month + ".lost", sum(d["lost_demand"] for d in days), 98)
    events = dataset["truth"]["events"]
    check("registry.oneoff", sum(e["quantity"] for e in events if e["kind"] == "one_off_purchase"), 500)
    check("registry.repeated_events", sum(e["kind"] == "recurring_regular_purchase" for e in events), 24)
    check("registry.repeated_quantity", sum(e["quantity"] for e in events if e["kind"] == "recurring_regular_purchase"), 24 * 80)
    check("registry.return", sum(e["quantity"] for e in events if e["kind"] == "return"), -7)
    check("registry.total_loss", sum(e["lost_demand"] for e in events if e["kind"] == "confirmed_stockout"), 196)
    assert len(checks) == 48
