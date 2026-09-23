"""Small, deterministic monthly forecasting models without UI or oracle access."""

from __future__ import annotations

from datetime import date
from math import isfinite
from statistics import mean


RULES_VERSION = "monthly-forecast-1"
MODEL_NAMES = ("seasonal_analog", "damped_level_trend", "intermittent_sba")
BASELINE_NAMES = ("last_complete_month", "mean_last_3", "same_month_previous_year")


def _month(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Месяц должен иметь формат YYYY-MM-01.") from error
    if parsed.day != 1 or parsed.isoformat() != value:
        raise ValueError("Месяц должен иметь формат YYYY-MM-01.")
    return parsed


def add_month(value: str, offset: int) -> str:
    parsed = _month(value)
    serial = parsed.year * 12 + parsed.month - 1 + offset
    return f"{serial // 12:04d}-{serial % 12 + 1:02d}-01"


def validate_config(payload: dict) -> dict:
    expected = {
        "horizon_months", "warehouse_scope", "growth_application",
        "short_history_fallback", "seasonal_aggregate_policy",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise ValueError("Настройка прогноза должна содержать пять явно заданных разделов.")
    horizon = payload["horizon_months"]
    if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 24:
        raise ValueError("Горизонт прогноза должен быть целым числом от 1 до 24 месяцев.")
    if payload["warehouse_scope"] != "source_report":
        raise ValueError("Поддержан только явно выбранный охват source_report; фильтр складов ещё не согласован.")

    growth = payload["growth_application"]
    growth_fields = {"mode", "start", "end", "scope", "status", "reason", "author"}
    if not isinstance(growth, dict) or set(growth) != growth_fields:
        raise ValueError("Политика прироста требует режим, период, охват, статус, автора и основание.")
    if growth["mode"] not in ("replace_trend", "additional") or growth["scope"] != "source_report":
        raise ValueError("Прирост задаётся как replace_trend/additional для охвата source_report.")
    if _month(growth["start"]) > _month(growth["end"]):
        raise ValueError("Начало периода прироста должно быть не позже конца.")
    _decision_metadata(growth)

    fallback = payload["short_history_fallback"]
    if fallback is not None:
        fallback_fields = {"category_code", "monthly_demand", "status", "reason", "author"}
        if not isinstance(fallback, dict) or set(fallback) != fallback_fields:
            raise ValueError("Fallback требует категорию, месячный спрос, статус, автора и основание.")
        value = fallback["monthly_demand"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
            raise ValueError("Fallback спрос должен быть конечным неотрицательным числом.")
        if not isinstance(fallback["category_code"], str) or not fallback["category_code"].strip():
            raise ValueError("Fallback требует код подтверждённой категории.")
        _decision_metadata(fallback)

    seasonal = payload["seasonal_aggregate_policy"]
    seasonal_fields = {"use", "unit_status", "reason", "author"}
    if not isinstance(seasonal, dict) or set(seasonal) != seasonal_fields:
        raise ValueError("Нужно явно зафиксировать решение по агрегатам сезонности.")
    if seasonal["use"] is not False or seasonal["unit_status"] != "unknown":
        raise ValueError("Агрегаты без установленной единицы и связи с SKU нельзя использовать в количественном прогнозе.")
    if not all(isinstance(seasonal[key], str) and seasonal[key].strip() for key in ("reason", "author")):
        raise ValueError("Решению по сезонным агрегатам нужны автор и основание.")
    return payload


def _decision_metadata(entry: dict) -> None:
    if entry["status"] not in ("confirmed", "scenario"):
        raise ValueError("Решение должно иметь status confirmed или scenario.")
    if not all(isinstance(entry[key], str) and entry[key].strip() for key in ("reason", "author")):
        raise ValueError("Решению нужны непустые автор и основание.")


def _validate_history(history: list[dict]) -> tuple[list[str], list[float]]:
    if not isinstance(history, list):
        raise ValueError("История должна быть списком завершённых месяцев.")
    ordered = sorted(history, key=lambda row: row.get("period", ""))
    periods, values = [], []
    for row in ordered:
        period, value = row.get("period"), row.get("quantity")
        _month(period)
        if row.get("state", "value") != "value" or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Месяц {period} не содержит готового регулярного спроса.")
        if not isfinite(value) or value < 0:
            raise ValueError(f"Регулярный спрос {period} должен быть конечным и неотрицательным.")
        periods.append(period)
        values.append(float(value))
    if len(set(periods)) != len(periods):
        raise ValueError("История содержит повторный месяц.")
    if periods and any(periods[index] != add_month(periods[0], index) for index in range(len(periods))):
        raise ValueError("Месячная история содержит разрыв; отсутствующий месяц нельзя заменить нулём.")
    return periods, values


def _holt(values: list[float], horizon: int, alpha: float, beta: float, phi: float) -> list[float]:
    if not values:
        return []
    level = values[0]
    trend = values[1] - values[0] if len(values) > 1 else 0.0
    for value in values[1:]:
        previous = level
        level = alpha * value + (1 - alpha) * (level + phi * trend)
        trend = beta * (level - previous) + (1 - beta) * phi * trend
        cap = max(abs(level) * 0.20, 1.0)
        trend = max(-cap, min(cap, trend))
    return [max(0.0, level + trend * sum(phi ** step for step in range(1, lead + 1)))
            for lead in range(1, horizon + 1)]


def _score(actual: list[float], predicted: list[float]) -> float:
    denominator = sum(abs(value) for value in actual)
    errors = [abs(left - right) for left, right in zip(actual, predicted)]
    return sum(errors) / denominator if denominator else mean(errors) if errors else float("inf")


def _tuned_holt(values: list[float], horizon: int) -> tuple[list[float], dict]:
    grid = [(a, b, p) for a in (0.2, 0.5, 0.8) for b in (0.1, 0.3) for p in (0.8, 0.95)]
    best = grid[0]
    best_score = float("inf")
    if len(values) >= 7:
        origins = range(max(6, len(values) - 6), len(values))
        for parameters in grid:
            predictions = [_holt(values[:origin], 1, *parameters)[0] for origin in origins]
            score = _score([values[origin] for origin in origins], predictions)
            if score < best_score:
                best_score, best = score, parameters
    return _holt(values, horizon, *best), {"alpha": best[0], "beta": best[1], "phi": best[2]}


def _croston(values: list[float], alpha: float) -> float:
    positives = [(index, value) for index, value in enumerate(values) if value > 0]
    if not positives:
        return 0.0
    first_index, level = positives[0]
    interval = float(first_index + 1)
    last = first_index
    for index, value in positives[1:]:
        level += alpha * (value - level)
        observed_interval = index - last
        interval += alpha * (observed_interval - interval)
        last = index
    return max(0.0, (1 - alpha / 2) * level / interval)


def _tuned_croston(values: list[float], horizon: int) -> tuple[list[float], dict]:
    best, best_score = 0.1, float("inf")
    if len(values) >= 7:
        origins = range(max(6, len(values) - 6), len(values))
        for alpha in (0.1, 0.2, 0.4):
            predictions = [_croston(values[:origin], alpha) for origin in origins]
            score = _score([values[origin] for origin in origins], predictions)
            if score < best_score:
                best, best_score = alpha, score
    value = _croston(values, best)
    return [value] * horizon, {"alpha": best, "sba_correction": True}


def _predict(model: str, values: list[float], horizon: int) -> tuple[list[float], dict]:
    if model == "seasonal_analog":
        if len(values) < 12:
            return [], {}
        return [values[len(values) - 12 + lead % 12] for lead in range(horizon)], {
            "lag_months": 12,
        }
    if model == "damped_level_trend":
        return _tuned_holt(values, horizon)
    if model == "intermittent_sba":
        return _tuned_croston(values, horizon)
    raise ValueError("Неизвестная модель прогноза.")


def _baseline(name: str, values: list[float]) -> float | None:
    if not values:
        return None
    if name == "last_complete_month":
        return values[-1]
    if name == "mean_last_3":
        return mean(values[-3:])
    if name == "same_month_previous_year":
        return values[-12] if len(values) >= 12 else None
    raise ValueError("Неизвестная базовая модель.")


def _metrics(points: list[dict]) -> dict:
    usable = [point for point in points if point.get("actual") is not None and point.get("prediction") is not None]
    if not usable:
        return {"count": 0, "wape": None, "mae_units": None, "bias_units": None}
    actual = [point["actual"] for point in usable]
    predicted = [point["prediction"] for point in usable]
    errors = [prediction - observed for observed, prediction in zip(actual, predicted)]
    denominator = sum(abs(value) for value in actual)
    return {
        "count": len(usable),
        "wape": sum(abs(value) for value in errors) / denominator if denominator else None,
        "mae_units": mean(abs(value) for value in errors),
        "bias_units": mean(errors),
    }


def forecast_monthly(history: list[dict], forecast_start: str, config: dict, *,
                     business_growth: float, category_code: str,
                     origin_histories: dict[str, list[dict]] | None = None) -> dict:
    """Compare three small models with rolling origins and forecast completed history.

    ``origin_histories`` lets the service provide cleaning re-fitted using only
    documents known before each origin. This core never reads files or truth.
    """
    config = validate_config(config)
    periods, values = _validate_history(history)
    _month(forecast_start)
    if periods and forecast_start != add_month(periods[-1], 1):
        raise ValueError("Прогноз должен начинаться после последнего завершённого месяца.")
    if isinstance(business_growth, bool) or not isinstance(business_growth, (int, float)) or not isfinite(business_growth) or business_growth <= -1:
        raise ValueError("Бизнес-прирост должен быть конечным числом больше −1.")

    limitations = [
        "Сезонные агрегаты поставщика исключены: единица и связь с SKU не установлены.",
        "Коррекция отсутствия товара требует отдельной проверенной истории; модель сама не определяет дни stockout.",
    ]
    fallback = config["short_history_fallback"]
    if len(values) < 12:
        if fallback is None:
            return {
                "status": "insufficient_history", "selected_model": None,
                "history_months": len(values), "backtest": [], "forecasts": [],
                "metrics": {}, "baselines": {},
                "limitations": limitations + ["Меньше 12 завершённых месяцев и явный fallback не задан."],
                "model_selection": {"measured": False, "reason": "short_history_without_fallback"},
            }
        if fallback["category_code"] != category_code:
            raise ValueError("Категория fallback не совпадает с категорией товара.")
        selected = "category_fallback"
        future_values = [float(fallback["monthly_demand"])] * config["horizon_months"]
        selection = {"measured": False, "reason": "explicit_short_history_fallback"}
        backtest, metrics, baselines = [], {}, {}
        limitations.append("Короткая история: использован явный fallback категории/сценария; преимущество не измерено.")
    else:
        first_origin = max(12, len(values) - 12)
        candidate_points = {model: [] for model in MODEL_NAMES}
        baseline_points = {name: [] for name in BASELINE_NAMES}
        for index in range(first_origin, len(values)):
            origin = periods[index]
            origin_history = origin_histories.get(origin) if origin_histories else history[:index]
            origin_periods, training = _validate_history(origin_history)
            if not origin_periods or origin_periods[-1] != periods[index - 1]:
                raise ValueError(f"Обучающая история для {origin} не заканчивается предыдущим месяцем.")
            for model in MODEL_NAMES:
                prediction, parameters = _predict(model, training, 1)
                if prediction:
                    candidate_points[model].append({"period": origin, "actual": values[index],
                                                    "prediction": prediction[0], "parameters": parameters})
            for name in BASELINE_NAMES:
                prediction = _baseline(name, training)
                if prediction is not None:
                    baseline_points[name].append({"period": origin, "actual": values[index], "prediction": prediction})
        candidate_metrics = {name: _metrics(points) for name, points in candidate_points.items()}
        eligible = [name for name in MODEL_NAMES if candidate_metrics[name]["count"]]
        if business_growth != 0 and config["growth_application"]["mode"] == "replace_trend":
            eligible = [name for name in eligible if name != "damped_level_trend"]
        selected = min(eligible, key=lambda name: (
            candidate_metrics[name]["wape"] if candidate_metrics[name]["wape"] is not None
            else candidate_metrics[name]["mae_units"], MODEL_NAMES.index(name)
        ))
        future_values, final_parameters = _predict(selected, values, config["horizon_months"])
        backtest = candidate_points[selected]
        metrics = candidate_metrics[selected]
        metrics["parameters_refitted_on_full_history"] = final_parameters
        baselines = {name: _metrics(points) for name, points in baseline_points.items()}
        scored_baselines = {name: item for name, item in baselines.items() if item["count"] and item["wape"] is not None}
        best_baseline = min(scored_baselines, key=lambda name: scored_baselines[name]["wape"]) if scored_baselines else None
        difference = metrics["wape"] - baselines[best_baseline]["wape"] if best_baseline and metrics["wape"] is not None else None
        selection = {
            "measured": metrics["count"] > 0,
            "candidate_metrics": candidate_metrics,
            "best_baseline": best_baseline,
            "wape_difference_to_best_baseline": difference,
            "outperforms_best_baseline": difference is not None and difference < 0,
        }

    forecasts = []
    growth = config["growth_application"]
    for offset, value in enumerate(future_values):
        period = add_month(forecast_start, offset)
        applied = growth["start"] <= period <= growth["end"] and business_growth != 0
        adjusted = value * (1 + business_growth) if applied else value
        forecasts.append({
            "period": period, "base_prediction": value, "prediction": adjusted,
            "business_growth": business_growth if applied else 0,
            "growth_mode": growth["mode"], "growth_applied_count": 1 if applied else 0,
        })
    return {
        "status": "forecast", "selected_model": selected,
        "history_months": len(values), "backtest": backtest, "forecasts": forecasts,
        "metrics": metrics, "baselines": baselines, "model_selection": selection,
        "limitations": limitations,
    }
