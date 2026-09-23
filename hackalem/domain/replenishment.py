"""Deterministic, unit-aware replenishment calculation for an explicit forecast."""

import calendar
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING
from math import isfinite

from hackalem.domain.quality_config import iso_date

RULES_VERSION = "replenishment-1"


def _number(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{label}: требуется конечное число.")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"{label}: требуется {'положительное' if positive else 'неотрицательное'} число.")
    return value


def _daily_forecast(forecast, start, end):
    if not isinstance(forecast, dict) or forecast.get("basis") != "regular_unreserved":
        raise ValueError("Прогноз должен иметь basis=regular_unreserved: резерв исключён из спроса.")
    if not all(isinstance(forecast.get(key), str) and forecast[key].strip()
               for key in ("version", "source", "author", "reason")):
        raise ValueError("Прогнозу нужны version, source, author и reason.")
    granularity = forecast.get("granularity")
    if granularity not in ("daily", "monthly") or not isinstance(forecast.get("values"), list):
        raise ValueError("Прогноз требует granularity=daily|monthly и values.")
    values = {}
    for row in forecast["values"]:
        if not isinstance(row, dict) or set(row) != ({"date", "quantity"} if granularity == "daily" else {"period", "quantity"}):
            raise ValueError("Строка прогноза требует дату/месяц и quantity.")
        key = row["date"] if granularity == "daily" else row["period"]
        day = iso_date(key)
        if granularity == "monthly" and day.day != 1:
            raise ValueError("Период месячного прогноза должен быть первым днём месяца.")
        if key in values:
            raise ValueError("Период прогноза повторяется: " + key)
        values[key] = _number(row["quantity"], "Прогноз")
    daily = {}
    day = start + timedelta(days=1)
    while day <= end:
        key = day.isoformat() if granularity == "daily" else day.replace(day=1).isoformat()
        if key not in values:
            raise ValueError("Прогноз не покрывает день " + day.isoformat())
        daily[day.isoformat()] = (values[key] if granularity == "daily" else
                                  values[key] / calendar.monthrange(day.year, day.month)[1])
        day += timedelta(days=1)
    return daily


def calculate_replenishment(prepared, forecast, *, as_of, project_commitments=()):
    """Calculate one SKU; all quantities entering the calendar use accounting units."""
    start = iso_date(as_of)
    values = prepared["effective_values"]
    if values.get("stock_date") != as_of:
        raise ValueError("Остаток должен иметь дату выбранного среза.")
    lead, review = values.get("lead_time_days"), values.get("review_period_days")
    if isinstance(lead, bool) or not isinstance(lead, int) or lead < 0 or isinstance(review, bool) or not isinstance(review, int) or review < 1:
        raise ValueError("Срок поставки L и период пересмотра R должны быть целыми днями.")
    if lead + review > 730:
        raise ValueError("Горизонт расчёта ограничен 730 днями.")
    horizon = start + timedelta(days=lead + review)
    new_eta = start + timedelta(days=lead)
    physical = _number(values.get("current_stock"), "Физический остаток")
    reserved = _number(values.get("reserved_stock"), "Резерв")
    if reserved > physical:
        raise ValueError("Резерв больше физического остатка.")
    available = physical - reserved
    factor = _number(values.get("unit_factor"), "Коэффициент единиц", positive=True)
    policy = values.get("stock_policy")
    if not isinstance(policy, dict) or policy.get("mode") not in ("stock", "on_demand", "exclude"):
        raise ValueError("Требуется явная политика запаса.")
    if policy["mode"] == "exclude":
        raise ValueError("Товар исключён политикой запаса.")
    safety_days = _number(policy.get("safety_days"), "Дни страхового запаса")
    daily = _daily_forecast(forecast, start, horizon)
    regular_demand = sum(daily.values())
    safety = regular_demand / (lead + review) * safety_days if policy["mode"] == "stock" else 0

    commitments = []
    for row in project_commitments:
        if not isinstance(row, dict) or row.get("reservation") not in ("already_reserved", "unreserved"):
            raise ValueError("Проектное обязательство требует reservation=already_reserved|unreserved.")
        due = iso_date(row.get("due"))
        quantity = _number(row.get("quantity"), "Проектное обязательство")
        if not isinstance(row.get("source"), str) or not row["source"].strip():
            raise ValueError("Проектное обязательство требует источник.")
        commitments.append({"due": due.isoformat(), "quantity": quantity,
                            "reservation": row["reservation"], "source": row["source"]})
    project_due = sum(row["quantity"] for row in commitments if row["reservation"] == "unreserved" and start < iso_date(row["due"]) <= horizon)

    arrivals, excluded = [], []
    for row in prepared.get("incoming", []):
        eta = row.get("eta", {}).get("eta") if isinstance(row.get("eta"), dict) else None
        quantity = row.get("accounting_quantity")
        if not eta or quantity is None:
            excluded.append({"reason": "unconfirmed", "source": row.get("cell")})
            continue
        day = iso_date(eta)
        _number(quantity, "Поступление")
        arrival = {"eta": eta, "quantity": quantity, "unit": values["accounting_unit"],
                   "source_kind": row.get("source_kind"), "sheet": row.get("sheet"),
                   "cell": row.get("cell"), "meaning": row["eta"].get("meaning")}
        if day <= start:
            excluded.append({**arrival, "reason": "overdue_not_received"})
        elif day <= horizon:
            arrivals.append(arrival)
        else:
            excluded.append({**arrival, "reason": "after_horizon"})
    timely = sum(row["quantity"] for row in arrivals)
    raw = max(0, regular_demand + project_due + safety - available - timely)
    raw_purchase = raw / factor
    minimum = values.get("minimum_order")
    multiple = values.get("order_multiple")
    if minimum is not None:
        _number(minimum, "MOQ")
    if multiple is not None:
        _number(multiple, "Кратность", positive=True)
    if raw == 0:
        order_purchase = 0
    elif multiple is None:
        order_purchase = None
    else:
        target = max(Decimal(str(raw_purchase)), Decimal(str(minimum or 0)))
        pack = Decimal(str(multiple))
        order_purchase = float(pack * (target / pack).to_integral_value(rounding=ROUND_CEILING))
    order_accounting = order_purchase * factor if order_purchase is not None else None

    arrivals_by_day = {}
    for row in arrivals:
        arrivals_by_day[row["eta"]] = arrivals_by_day.get(row["eta"], 0) + row["quantity"]
    project_by_day = {}
    for row in commitments:
        if row["reservation"] == "unreserved":
            project_by_day[row["due"]] = project_by_day.get(row["due"], 0) + row["quantity"]
    balance, planned_balance = available, (available + order_accounting if lead == 0 and order_accounting is not None else available)
    projected, first_risk, urgent_risk, planned_risk = [], None, None, None
    for day, demand in daily.items():
        movement = arrivals_by_day.get(day, 0) - demand - project_by_day.get(day, 0)
        balance += movement
        if order_accounting is not None:
            planned_balance += movement + (order_accounting if day == new_eta.isoformat() else 0)
            if planned_balance < -1e-9 and planned_risk is None:
                planned_risk = day
        if balance < -1e-9 and first_risk is None:
            first_risk = day
        if balance < -1e-9 and iso_date(day) < new_eta and urgent_risk is None:
            urgent_risk = day
        projected.append({"date": day, "regular_demand": demand,
                          "project_commitment": project_by_day.get(day, 0),
                          "existing_arrivals": arrivals_by_day.get(day, 0),
                          "new_order_arrival": order_accounting if day == new_eta.isoformat() else 0,
                          "balance_without_new_order": balance,
                          "balance_with_new_order": planned_balance if order_accounting is not None else None})

    accuracy = "day_from_daily_forecast" if forecast["granularity"] == "daily" else "approximate_day_uniform_monthly_distribution"
    status = "calculated" if minimum is not None and multiple is not None else "incomplete_constraints"
    explanation = {
        "formula": "max(0, regular_demand + unreserved_project_commitments + safety_stock - available_stock - timely_incoming)",
        "accounting_unit": values["accounting_unit"], "purchase_unit": values["purchase_unit"],
        "unit_factor": factor, "physical_stock": physical, "reserved_stock": reserved,
        "available_stock": available, "regular_forecast": regular_demand,
        "forecast_version": forecast["version"], "forecast_source": forecast["source"],
        "forecast_adjustments": forecast.get("adjustments", []),
        "project_commitments": commitments, "unreserved_project_due": project_due,
        "safety_stock": safety, "safety_days": safety_days,
        "safety_assumption": "average_horizon_daily_demand_times_safety_days",
        "timely_incoming": timely, "arrivals": arrivals, "excluded_arrivals": excluded,
        "raw_need_accounting": raw, "raw_need_purchase": raw_purchase,
        "minimum_order": minimum, "order_multiple": multiple,
        "rounded_order_purchase": order_purchase, "rounded_order_accounting": order_accounting,
        "rounding_added_accounting": None if order_accounting is None else order_accounting - raw,
        "first_risk_date": first_risk, "urgent_risk_date": urgent_risk,
        "first_risk_with_order_date": planned_risk,
        "minimum_balance_without_new_order": min(row["balance_without_new_order"] for row in projected),
        "risk_level": "urgent_before_new_order" if urgent_risk else "shortage_in_horizon" if first_risk else "no_shortage_in_horizon",
        "risk_date_precision": accuracy, "new_order_eta": new_eta.isoformat(),
        "assumptions": ["Прогноз задан вручную: " + forecast["reason"],
                        "Страховой запас задан в днях без статистической калибровки."] +
                       (["Месячный прогноз равномерно распределён по календарным дням месяца; день риска приблизителен."]
                        if forecast["granularity"] == "monthly" else []),
        "sources": {"quality_run_id": prepared.get("quality_run_id"),
                    "cleaning_run_id": prepared.get("cleaning_run_id"),
                    "snapshot_id": prepared.get("snapshot_id")},
    }
    explanation["text"] = (
        f"{regular_demand:g} + {project_due:g} + {safety:g} − {available:g} − {timely:g} = "
        f"{raw:g} {values['accounting_unit']}; после перевода и округления: "
        f"{order_purchase:g} {values['purchase_unit']}" if order_purchase is not None else
        f"Потребность {raw:g} {values['accounting_unit']}; кратность неизвестна, итоговый заказ не рассчитан."
    )
    return {"sku": prepared["sku"], "status": status, "as_of": as_of,
            "horizon_end": horizon.isoformat(), "lead_time_days": lead,
            "review_period_days": review, "order_quantity": order_purchase,
            "urgent_problem": urgent_risk is not None, "explanation": explanation,
            "calendar": projected}
