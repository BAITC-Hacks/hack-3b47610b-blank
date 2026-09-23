"""Pure synthetic input generator; validation truth is never an observed field.

The supplier names select existing adapters only. All products, customers,
documents, availability records and parameters in this module are fictional.
No model, forecast, order calculation, file access or global random state is used.
"""

from __future__ import annotations

from datetime import date, timedelta
from random import Random


GENERATOR_VERSION = "synthetic-1"
DEFAULT_SEED = 20260923
START = date(2024, 1, 1)
END = date(2025, 12, 31)
AS_OF = date(2026, 1, 1)
LABEL = "СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР"

# Scenario labels are consumed solely by this generator and the validation
# registry. Public products and transaction documents have neutral identities.
_SCENARIOS = (
    ("stable", "Стабильный спрос"),
    ("seasonal", "Повторяющийся сезонный пик"),
    ("growth", "Устойчивый рост"),
    ("intermittent", "Редкие продажи и подтверждённые нули"),
    ("new_product", "Новый товар с короткой историей"),
    ("one_off_client", "Один крупный заказ одного клиента"),
    ("repeated_large_client", "Повторяющиеся крупные заказы клиента"),
    ("return", "Возврат отдельным отрицательным документом"),
    ("stockout", "Подтверждённое полнодневное отсутствие"),
    ("unknown_blank", "Пустая запись без доказанного отсутствия"),
    ("no_open_orders", "Подтверждённое отсутствие открытых заказов"),
    ("timely_incoming", "Поступление до исчерпания запаса"),
    ("late_incoming", "Поступление после исчерпания запаса"),
    ("minimum_order", "Минимальный заказ отдельно от кратности"),
    ("order_multiple", "Кратность отдельно от минимального заказа"),
    ("unit_conversion", "Различные единицы учёта и закупки"),
    ("missing_critical", "Отсутствующий срок новой поставки"),
)
_SEASONAL_BASE = (6, 6, 8, 10, 14, 18, 20, 18, 13, 10, 8, 6)
_NEW_LAUNCH = date(2025, 10, 1)
_ONE_OFF_DATE = date(2025, 6, 17)
_RETURN_DATE = date(2025, 2, 17)
_BLANK_START, _BLANK_END = date(2025, 11, 10), date(2025, 11, 16)


def _parameters(scenario: str) -> dict:
    """Raw named parameters; the persistence adapter supplies decision metadata."""
    values = {
        "current_stock": 100,
        "reserved_stock": 10,
        "stock_date": AS_OF.isoformat(),
        "lead_time_days": 14,
        "review_period_days": 7,
        "category_code": "SYN-CAT-01",
        "category_label": "Синтетическая складская категория",
        "stock_policy": {"mode": "stock", "safety_days": 5},
        "minimum_order": 1,
        "order_multiple": 1,
        "accounting_unit": "шт",
        "purchase_unit": "шт",
        "unit_factor": 1,
        "business_growth": 0,
        "blank_sales_policy": "preserve",
        "no_open_orders": True,
        "incoming_unit": "шт",
    }
    if scenario == "timely_incoming":
        values.update(current_stock=50, reserved_stock=0, no_open_orders=False)
    elif scenario == "late_incoming":
        values.update(current_stock=15, reserved_stock=0, no_open_orders=False)
    elif scenario == "minimum_order":
        values.update(current_stock=30, reserved_stock=0, lead_time_days=7,
                      minimum_order=20, stock_policy={"mode": "stock", "safety_days": 3})
    elif scenario == "order_multiple":
        values.update(current_stock=25, reserved_stock=0, lead_time_days=7,
                      order_multiple=12, stock_policy={"mode": "stock", "safety_days": 0})
    elif scenario == "unit_conversion":
        values.update(current_stock=50, reserved_stock=10, lead_time_days=7,
                      purchase_unit="упак", incoming_unit="упак", unit_factor=4,
                      minimum_order=2, order_multiple=3,
                      stock_policy={"mode": "stock", "safety_days": 2})
    elif scenario == "missing_critical":
        del values["lead_time_days"]
    return values


def _base_demand(scenario: str, day: date, rng: Random) -> int:
    if scenario == "stable":
        return 10 + rng.randint(-1, 1)
    if scenario == "seasonal":
        return _SEASONAL_BASE[day.month - 1] + rng.randint(-1, 1)
    if scenario == "growth":
        month_index = (day.year - START.year) * 12 + day.month - START.month
        return 6 + month_index + rng.randint(-1, 1)
    if scenario == "intermittent":
        return (12 if day.day == 8 else 8) + rng.randint(-1, 1) if day.day in (8, 23) else 0
    if scenario in ("new_product", "one_off_client", "repeated_large_client"):
        return 4 + rng.randint(-1, 1)
    if scenario == "return":
        return 10 + rng.randint(-1, 1)
    return {"stockout": 7, "unknown_blank": 6, "minimum_order": 2,
            "order_multiple": 3, "unit_conversion": 6}.get(scenario, 5)


def _expectations(scenario: str) -> tuple[dict, list[str], list[int]]:
    # These are validation metadata, not model inputs or implemented algorithms.
    basic = ["base_need", "supplier_orders"]
    entries = {
        "stable": ({"daily_regular_min": 9, "daily_regular_max": 11,
                    "outlier_quantity_total": 0, "lost_demand_total": 0},
                   basic + ["seasonality_growth"], [8, 9]),
        "seasonal": ({"monthly_base": list(_SEASONAL_BASE), "peak_months": [6, 7, 8],
                      "low_months": [1, 2, 12], "profile_repeats_annually": True},
                     ["seasonality_growth"], [8]),
        "growth": ({"initial_month_daily_base": 6, "daily_base_increase_per_month": 1,
                    "business_growth": 0, "growth_counted_once": True},
                   ["seasonality_growth", "base_need"], [8, 9]),
        "intermittent": ({"positive_days_per_month": [8, 23], "positive_day_count": 48,
                          "other_days_are_confirmed_zero": True}, basic, [8]),
        "new_product": ({"launch_date": _NEW_LAUNCH.isoformat(), "observed_days": 92,
                          "pre_launch_transactions": 0, "pre_launch_demand_is_unknown": True},
                        basic, [8]),
        "one_off_client": ({"event_date": _ONE_OFF_DATE.isoformat(), "outlier_quantity_total": 500,
                             "outlier_document_count": 1, "outlier_customer_id": "SYN-C-901",
                             "customer_large_purchase_count": 1}, ["one_off_orders"], [6, 8]),
        "repeated_large_client": ({"recurring_quantity": 80, "recurring_day_of_month": 15,
                                    "recurring_document_count": 24, "customer_id": "SYN-C-902",
                                    "outlier_quantity_total": 0, "recurring_volume_is_regular": True},
                                   ["one_off_orders"], [6, 8]),
        "return": ({"return_date": _RETURN_DATE.isoformat(), "return_quantity_total": -7,
                    "negative_document_type": "Возврат от покупателя", "outlier_quantity_total": 0},
                   ["one_off_orders", "base_need"], [6]),
        "stockout": ({"stockout_day_count": 28, "daily_regular_demand": 7,
                      "lost_demand_total": 196, "observed_stockout_sales": 0,
                      "evidence_observed_hours_per_day": 24}, ["lost_demand"], [7, 8]),
        "unknown_blank": ({"blank_start": _BLANK_START.isoformat(), "blank_end": _BLANK_END.isoformat(),
                            "blank_day_count": 7, "confirmed_stockout_days": 0,
                            "lost_demand_total": 0, "blank_policy": "preserve"},
                           ["lost_demand", "base_need"], [7]),
        "no_open_orders": ({"incoming_order_count": 0, "no_open_orders": True}, basic, [9]),
        "timely_incoming": ({"incoming_order_count": 1, "eta": "2026-01-04",
                              "daily_regular_demand": 5, "available_stock": 50,
                              "arrival_before_stock_exhaustion": True}, basic, [9]),
        "late_incoming": ({"incoming_order_count": 1, "eta": "2026-01-20",
                            "daily_regular_demand": 5, "available_stock": 15,
                            "shortage_before_eta": True, "actual_receipt_not_observed": True}, basic, [9]),
        "minimum_order": ({"minimum_order": 20, "order_multiple": 1,
                            "minimum_and_multiple_are_distinct": True}, basic, [9]),
        "order_multiple": ({"minimum_order": 1, "order_multiple": 12,
                             "minimum_and_multiple_are_distinct": True}, basic, [9]),
        "unit_conversion": ({"accounting_unit": "шт", "purchase_unit": "упак",
                              "unit_factor": 4, "five_purchase_units_in_accounting_units": 20,
                              "minimum_order_purchase_units": 2, "order_multiple_purchase_units": 3}, basic, [9]),
        "missing_critical": ({"missing_parameters": ["lead_time_days"],
                               "expected_gate_status": "Не хватает данных",
                               "must_not_hide_other_skus": True}, basic, [5, 9]),
    }
    return entries[scenario]


def generate_dataset(seed: int = DEFAULT_SEED) -> dict:
    """Return one deterministic 24-month synthetic dataset and separate oracle.

    ``observed`` is the sole model-input partition. ``truth`` and ``scenarios``
    are validation-only. ``return_quantity`` is signed; true demand includes
    one-off gross demand, while regular_demand excludes that one-off component.
    Unknown transaction values remain None, and pre-launch dates have no sales
    documents. Complete availability observations describe the entire 24 hours.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Seed должен быть целым числом, а не логическим значением.")
    rng = Random(seed)
    manifest = {"dataset_kind": "synthetic", "dataset_id": f"{GENERATOR_VERSION}-seed-{seed}",
                "generator_version": GENERATOR_VERSION, "seed": seed,
                "start": START.isoformat(), "end": END.isoformat(),
                "as_of": AS_OF.isoformat(), "label": LABEL}
    observed = {"products": [], "transactions": [], "availability": [], "incoming": [], "parameters": {}}
    truth = {"daily": [], "events": []}
    scenarios = []
    days = [START + timedelta(days=offset) for offset in range((END - START).days + 1)]

    def event(sku: str, kind: str, **details) -> None:
        truth["events"].append({"event_id": f"SYN-E-{len(truth['events']) + 1:04d}",
                                "sku": sku, "kind": kind, **details})

    for index, (scenario, title) in enumerate(_SCENARIOS):
        group = "A" if index < 9 else "B"
        number = index + 1 if index < 9 else index - 8
        code = f"{group}-{number:03d}"
        sku = f"SYN-{code}"
        supplier = "Systeme Electric" if group == "A" else "IEK"
        launch = _NEW_LAUNCH if scenario == "new_product" else START
        observed["products"].append({"sku": sku, "name": f"Компонент {code}", "supplier": supplier,
                                      "unit": "шт", "article": f"SYN-ART-{code}", "launch_date": launch.isoformat()})
        observed["parameters"][sku] = _parameters(scenario)
        expected, must_haves, pending_stages = _expectations(scenario)
        scenarios.append({"id": scenario, "sku": sku, "supplier": supplier, "title": title,
                          "expected_properties": expected, "must_haves": must_haves,
                          "pending_checks": [{"stage": stage, "status": "pending"} for stage in pending_stages]})

        if scenario == "stockout":
            for year in (2024, 2025):
                event(sku, "confirmed_stockout", start=f"{year}-08-11", end=f"{year}-08-24",
                      lost_demand=98, observed_hours_per_day=24)
        elif scenario == "unknown_blank":
            event(sku, "unobserved_sales_recording", start=_BLANK_START.isoformat(), end=_BLANK_END.isoformat(),
                  actual_stockout=False, lost_demand=0)
        elif scenario == "new_product":
            event(sku, "launch", date=launch.isoformat())
        elif scenario == "missing_critical":
            event(sku, "missing_parameter", parameter="lead_time_days")

        for day in days:
            iso_day = day.isoformat()
            active = day >= launch
            stockout = scenario == "stockout" and day.month == 8 and 11 <= day.day <= 24
            unknown = scenario == "unknown_blank" and _BLANK_START <= day <= _BLANK_END
            observed["availability"].append({"sku": sku, "date": iso_day,
                                              "available": None if not active or unknown else not stockout,
                                              "observed_hours": None if not active or unknown else 24})
            if not active:
                truth["daily"].append({"sku": sku, "date": iso_day, "true_demand": None,
                                        "regular_demand": None, "observed_sales": None,
                                        "gross_observed_sales": None, "outlier_quantity": 0,
                                        "return_quantity": 0, "lost_demand": None,
                                        "available_in_truth": None, "in_scope": False})
                continue
            base = _base_demand(scenario, day, rng)
            recurrent = 80 if scenario == "repeated_large_client" and day.day == 15 else 0
            outlier = 500 if scenario == "one_off_client" and day == _ONE_OFF_DATE else 0
            returned = -7 if scenario == "return" and day == _RETURN_DATE else 0
            regular = base + recurrent
            gross_demand = regular + outlier
            gross_observed = None if unknown else 0 if stockout else gross_demand
            net_observed = None if unknown else gross_observed + returned

            def transaction(sequence: int, quantity: int | None, customer: str | None, document_type: str) -> str:
                document = f"SYN-D-{code}-{day:%Y%m%d}-{sequence:02d}"
                observed["transactions"].append({"sku": sku, "supplier": supplier, "date": iso_day,
                                                  "document_number": document, "document_type": document_type,
                                                  "customer_id": customer, "quantity": quantity,
                                                  "state": "blank" if quantity is None else "value",
                                                  "unit": "шт", "warehouse": "SYN-WH-01"})
                return document

            base_observed = None if unknown else 0 if stockout else base
            customer = f"SYN-C-{rng.randint(1, 12):03d}" if base_observed else None
            transaction(1, base_observed, customer, "Расходная накладная")
            if recurrent:
                document = transaction(2, recurrent, "SYN-C-902", "Расходная накладная")
                event(sku, "recurring_regular_purchase", date=iso_day, customer_id="SYN-C-902",
                      document_number=document, quantity=recurrent, is_outlier=False)
            if outlier:
                document = transaction(2, outlier, "SYN-C-901", "Расходная накладная")
                event(sku, "one_off_purchase", date=iso_day, customer_id="SYN-C-901",
                      document_number=document, quantity=outlier, is_outlier=True)
            if returned:
                document = transaction(2, returned, "SYN-C-003", "Возврат от покупателя")
                event(sku, "return", date=iso_day, document_number=document, quantity=returned, is_outlier=False)
            truth["daily"].append({"sku": sku, "date": iso_day, "true_demand": gross_demand,
                                    "regular_demand": regular, "observed_sales": net_observed,
                                    "gross_observed_sales": gross_observed, "outlier_quantity": outlier,
                                    "return_quantity": returned, "lost_demand": gross_demand if stockout else 0,
                                    "available_in_truth": not stockout, "in_scope": True})

        if scenario in ("timely_incoming", "late_incoming"):
            eta = "2026-01-04" if scenario == "timely_incoming" else "2026-01-20"
            order = f"SYN-O-{code}-01"
            observed["incoming"].append({"sku": sku, "supplier": supplier, "order_number": order,
                                         "order_date": "2025-12-20", "eta": eta, "quantity": 60, "unit": "шт"})
            event(sku, "planned_incoming", order_number=order, known_at="2025-12-20", eta=eta,
                  timing_relative_to_stock="before_exhaustion" if scenario == "timely_incoming" else "after_exhaustion",
                  actual_receipt_date=None)

    return {"manifest": manifest, "observed": observed, "truth": truth, "scenarios": scenarios}
