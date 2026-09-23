"""Validated, explicit assumptions for one immutable input snapshot."""

import copy
import math
from datetime import date

REQUIRED_PARAMETERS = (
    "current_stock", "reserved_stock", "stock_date", "lead_time_days", "review_period_days",
    "category_code", "category_label", "stock_policy", "minimum_order", "order_multiple",
    "accounting_unit", "purchase_unit", "unit_factor", "business_growth",
)
OPTIONAL_PARAMETERS = ("blank_sales_policy", "eta_confirmations", "no_open_orders", "incoming_unit")
SOURCES = ("transactions", "monthly_sales", "current")


def configuration_template():
    return {"as_of": None, "defaults": {}, "skus": {}, "sales_choices": []}


def iso_date(value):
    if not isinstance(value, str):
        raise ValueError("Дата должна иметь формат YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Дата должна иметь формат YYYY-MM-DD.") from error
    if parsed.isoformat() != value:
        raise ValueError("Дата должна иметь формат YYYY-MM-DD.")
    return parsed


def months(start, end):
    first, last = iso_date(start), iso_date(end)
    if first.day != 1 or last.day != 1 or first > last:
        raise ValueError("Период выбора: первые числа месяцев, начало не позже конца.")
    count = (last.year - first.year) * 12 + last.month - first.month + 1
    if count > 1200:
        raise ValueError("Диапазон выбора не должен превышать 1200 месяцев.")
    return [f"{(first.year * 12 + first.month - 1 + n) // 12:04d}-{(first.month - 1 + n) % 12 + 1:02d}-01" for n in range(count)]


def _metadata(entry):
    if not isinstance(entry, dict) or entry.get("status") not in ("confirmed", "scenario"):
        raise ValueError("Каждому значению нужен status confirmed или scenario.")
    for key in ("reason", "author"):
        if not isinstance(entry.get(key), str) or not entry[key].strip():
            raise ValueError(f"Укажите непустое поле {key} для решения.")


def _number(value, minimum, *, strict=False, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Ожидается конечное число; неизвестное не заменяется нулём.")
    if value < minimum or (strict and value == minimum) or (integer and value != int(value)):
        raise ValueError("Число вне допустимого диапазона или должно быть целым.")


def _parameter(name, entry):
    if name not in REQUIRED_PARAMETERS + OPTIONAL_PARAMETERS:
        raise ValueError(f"Неизвестный параметр: {name}")
    _metadata(entry)
    if set(entry) - {"value", "status", "reason", "author", "evidence"} or "value" not in entry:
        raise ValueError(f"Неверные поля параметра {name}.")
    value = entry["value"]
    if name in ("current_stock", "reserved_stock"):
        _number(value, 0)
    elif name in ("minimum_order", "order_multiple", "unit_factor"):
        _number(value, 0, strict=True)
    elif name in ("lead_time_days", "review_period_days"):
        _number(value, 0 if name == "lead_time_days" else 1, integer=True)
    elif name == "business_growth":
        _number(value, -1)
    elif name == "stock_date":
        iso_date(value)
    elif name in ("category_code", "category_label", "accounting_unit", "purchase_unit", "incoming_unit"):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Параметр {name} должен быть непустой строкой.")
    elif name == "blank_sales_policy":
        if value not in ("zero", "preserve"):
            raise ValueError("Политика пустот: zero или preserve.")
    elif name == "no_open_orders":
        if not isinstance(value, bool):
            raise ValueError("no_open_orders должен быть явным true или false.")
    elif name == "stock_policy":
        if not isinstance(value, dict) or set(value) != {"mode", "safety_days"} or value["mode"] not in ("stock", "on_demand", "exclude"):
            raise ValueError("stock_policy: нужны mode (stock/on_demand/exclude) и safety_days.")
        _number(value["safety_days"], 0)
    elif name == "eta_confirmations":
        if not isinstance(value, list):
            raise ValueError("eta_confirmations должен быть списком поступлений.")
        seen = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"source_kind", "sheet", "cell", "eta", "meaning"}:
                raise ValueError("ETA требует source_kind, sheet, cell, eta и meaning.")
            if item["source_kind"] not in ("current", "incoming") or item["meaning"] not in ("expected", "deadline"):
                raise ValueError("Неверный тип источника или смысл ETA.")
            if not all(isinstance(item[key], str) and item[key].strip() for key in ("sheet", "cell")):
                raise ValueError("Укажите лист и ячейку поступления.")
            iso_date(item["eta"])
            identity = (item["source_kind"], item["sheet"], item["cell"])
            if identity in seen:
                raise ValueError("Повторное подтверждение одной ETA в конфигурации.")
            seen.add(identity)


def validate_configuration(payload, known_skus, available_sources):
    if not isinstance(payload, dict) or set(payload) - set(configuration_template()):
        raise ValueError("Неверные поля конфигурации: нужны as_of, defaults, skus, sales_choices.")
    result = configuration_template()
    result.update(copy.deepcopy(payload))
    if result["as_of"] is not None:
        iso_date(result["as_of"])
    if not isinstance(result["defaults"], dict) or not isinstance(result["skus"], dict):
        raise ValueError("defaults и skus должны быть объектами.")
    for name, entry in result["defaults"].items():
        _parameter(name, entry)
    for sku, parameters in result["skus"].items():
        if not isinstance(sku, str) or sku not in known_skus or not isinstance(parameters, dict):
            raise ValueError(f"Неизвестный SKU или неверные параметры: {sku}")
        for name, entry in parameters.items():
            _parameter(name, entry)
    if not isinstance(result["sales_choices"], list):
        raise ValueError("sales_choices должен быть списком.")
    intervals = {}
    for choice in result["sales_choices"]:
        _metadata(choice)
        if set(choice) != {"sku", "start", "end", "source", "scope", "status", "reason", "author"}:
            raise ValueError("Неверные поля выбора источника продаж.")
        if not isinstance(choice["sku"], str) or (choice["sku"] != "*" and choice["sku"] not in known_skus):
            raise ValueError("Выбор источника относится к неизвестному SKU.")
        if choice["source"] not in SOURCES or choice["source"] not in available_sources:
            raise ValueError("Выбранный источник продаж отсутствует в снимке.")
        if choice["scope"] != "source_report":
            raise ValueError("Поддержан охват source_report: весь исходный отчёт для выбранных SKU и месяцев.")
        periods = set(months(choice["start"], choice["end"]))
        previous = intervals.setdefault(choice["sku"], set())
        if previous & periods:
            raise ValueError("Пересекающиеся правила одного охвата SKU неоднозначны.")
        previous.update(periods)
    return result
