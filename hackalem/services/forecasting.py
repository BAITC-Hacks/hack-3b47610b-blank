"""Versioned monthly forecasts over the checked, re-cleaned regular history."""

from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from hackalem.domain.cleaning import aggregate_months, classify_documents
from hackalem.domain.forecasting import RULES_VERSION, add_month, forecast_monthly, validate_config
from hackalem.services.cleaning import cleaning_report, prepared_input
from hackalem.services.datasets import dataset_context
from hackalem.services.lost_demand import adjusted_history
from hackalem.services.systeme import _code_manifest, _connect, _hash, _json, _now, _snapshot
from hackalem.storage import initialize_database


def forecast_config_template(as_of: str | None = None) -> dict:
    start = (as_of[:7] + "-01") if isinstance(as_of, str) and len(as_of) >= 7 else "2026-01-01"
    return {
        "horizon_months": 12,
        "warehouse_scope": "source_report",
        "growth_application": {
            "mode": "replace_trend", "start": start, "end": add_month(start, 11),
            "scope": "source_report", "status": "scenario",
            "reason": "Укажите подтверждённый период и смысл бизнес-прироста.", "author": "replace-me",
        },
        "short_history_fallback": None,
        "seasonal_aggregate_policy": {
            "use": False, "unit_status": "unknown",
            "reason": "Единица и связь агрегата с SKU не подтверждены.", "author": "system-default",
        },
    }


def _documents(connection, cleaning_run_id: int, sku: str) -> list[dict]:
    return [json.loads(row[0]) for row in connection.execute(
        "SELECT payload_json FROM cleaning_documents WHERE run_id=? AND sku=? ORDER BY period,document_key",
        (cleaning_run_id, sku),
    )]


def _origin_histories(documents: list[dict], periods: list[str], policy: str,
                      decisions: list[dict]) -> dict[str, list[dict]]:
    """Re-run automatic cleaning inside every outer training split."""
    result = {}
    for origin in periods[12:]:
        training = [row for row in documents if row.get("period") and row["period"] < origin]
        keys = {row["document_key"] for row in training}
        split_decisions = [entry for entry in decisions if entry["document_key"] in keys]
        classified = classify_documents(training, policy=policy, decisions=split_decisions)
        monthly = {row["period"]: row for row in aggregate_months(classified)}
        rows = []
        for period in periods:
            if period >= origin:
                break
            row = monthly.get(period)
            if row is None or row["state"] != "value" or row["regular_quantity"] is None:
                raise ValueError(f"Очистка на обучающей части {origin} не подготовила месяц {period}.")
            rows.append({"period": period, "quantity": row["regular_quantity"], "state": "value"})
        result[origin] = rows
    return result


def run_forecast(database_path: Path, quality_run_id: int, cleaning_run_id: int,
                 sku: str, config: dict, *, allow_scenario: bool = False,
                 lost_demand_run_id: int | None = None) -> dict:
    database_path = Path(database_path)
    initialize_database(database_path)
    config = validate_config(config)
    checked = prepared_input(
        database_path, quality_run_id, cleaning_run_id, sku, allow_scenario=allow_scenario,
    )
    preparation = cleaning_report(database_path, cleaning_run_id, sku=sku, limit=1)
    history = [{"period": row["period"], "quantity": row["quantity"], "state": row["state"]}
               for row in checked["history"]]
    lost_input = None
    if lost_demand_run_id is not None:
        lost_input = adjusted_history(database_path, lost_demand_run_id)
        if (lost_input["snapshot_id"] != checked["snapshot_id"] or lost_input["sku"] != sku or
                lost_input["as_of"] != preparation["as_of"]):
            raise ValueError("Оценка упущенного спроса относится к другому снимку, SKU или срезу.")
        adjusted = {row["period"]: row["quantity"] for row in lost_input["history"]}
        if set(adjusted) != {row["period"] for row in history}:
            raise ValueError("Периоды оценки упущенного спроса не совпадают с подготовленной историей.")
        history = [{**row, "quantity": adjusted[row["period"]]} for row in history]
    if not history:
        raise ValueError("Для прогноза нет завершённой подготовленной истории.")
    as_of_month = preparation["as_of"][:7] + "-01"
    if any(row["period"] >= as_of_month for row in history):
        raise ValueError("Текущий или будущий месяц нельзя включать в месячное обучение.")
    forecast_start = add_month(history[-1]["period"], 1)
    if forecast_start != as_of_month:
        raise ValueError("Выбранная история должна завершаться месяцем перед датой среза; пропуски не заполняются.")
    parameters = checked["parameters"]
    growth_entry = parameters["business_growth"]
    category_entry = parameters["category_code"]
    with closing(_connect(database_path)) as connection:
        documents = _documents(connection, cleaning_run_id, sku)
    origins = _origin_histories(
        documents, [row["period"] for row in history], preparation["policy"], preparation["decisions"],
    )
    if lost_input is not None:
        adjusted = {row["period"]: row["quantity"] for row in lost_input["history"]}
        origins = {origin: [{**row, "quantity": adjusted[row["period"]]} for row in rows]
                   for origin, rows in origins.items()}
    result = forecast_monthly(
        history, forecast_start, config,
        business_growth=growth_entry["value"], category_code=category_entry["value"],
        origin_histories=origins,
    )
    if preparation["decisions"]:
        result["limitations"].append(
            "Ручные решения очистки не имеют исторической даты действия; backtest не доказывает prospective-преимущество модели."
        )
        result["model_selection"]["measured"] = False
        result["model_selection"]["outperforms_best_baseline"] = False
        result["model_selection"]["reason"] = "manual_decisions_without_historical_effective_date"
    if lost_input is None:
        result["limitations"].append(
            "Версия этапа 7 не указана: прогноз использует наблюдаемый регулярный спрос без поправки на доказанный stockout."
        )
    input_is_scenario = checked["status"] == "Сценарный расчёт" or bool(
        lost_input and lost_input["scenario"]
    )
    policy_is_scenario = config["growth_application"]["status"] == "scenario" or (
        config["short_history_fallback"] is not None
        and config["short_history_fallback"]["status"] == "scenario"
    )
    status = ("Не хватает данных" if result["status"] == "insufficient_history" else
              "Сценарный прогноз" if input_is_scenario or policy_is_scenario else
              "Расчётный прогноз")
    if status == "Сценарный прогноз" and allow_scenario is not True:
        raise ValueError("Сценарный прогноз требует явного allow_scenario=True.")
    result.update({
        "sku": sku, "status_label": status, "as_of": preparation["as_of"],
        "forecast_start": forecast_start, "unit": parameters["accounting_unit"]["value"],
        "warehouse_scope": config["warehouse_scope"],
        "category": {"code": category_entry["value"],
                     "label": parameters["category_label"]["value"],
                     "stock_policy": parameters["stock_policy"]},
        "growth_decision": {"parameter": growth_entry, "application": config["growth_application"]},
        "lost_demand_input": {"run_id": lost_demand_run_id,
                              "applied": lost_input is not None,
                              "historical_lost_demand_as_current_backlog": 0},
        "seasonal_aggregate_decision": config["seasonal_aggregate_policy"],
        "training_protocol": {
            "current_month_excluded": True,
            "outer_origins": [point["period"] for point in result["backtest"]],
            "cleaning_refitted_per_origin": True,
            "model_parameters_refitted_per_origin": True,
            "manual_decisions_replayed": bool(preparation["decisions"]),
            "lost_demand_adjustment_is_causal": lost_input is not None,
            "oracle_used_by_model": False,
        },
    })
    code_version, _ = _code_manifest()
    fingerprint = _hash(_json({
        "quality_run": quality_run_id, "cleaning_run": cleaning_run_id,
        "lost_demand_run": lost_demand_run_id, "sku": sku,
        "config": config, "rules": RULES_VERSION, "code": code_version,
    }).encode())
    with closing(_connect(database_path)) as connection, connection:
        prior = connection.execute("SELECT id FROM forecast_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if prior:
            return forecast_report(database_path, prior["id"])
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute("SELECT id FROM forecast_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if prior:
            run_id = prior["id"]
        else:
            run_id = connection.execute(
                """INSERT INTO forecast_runs
                (snapshot_id,quality_run_id,cleaning_run_id,lost_demand_run_id,sku,fingerprint,created_at_utc,as_of,
                 rules_version,code_version,status,selected_model,config_json,summary_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (checked["snapshot_id"], quality_run_id, cleaning_run_id, lost_demand_run_id,
                 sku, fingerprint, _now(),
                 preparation["as_of"], RULES_VERSION, code_version, status, result["selected_model"],
                 _json(config), _json(result)),
            ).lastrowid
            points = []
            for point in result["backtest"]:
                points.append((run_id, "backtest", point["period"], result["selected_model"],
                               point["actual"], point["prediction"], _json(point)))
            for point in result["forecasts"]:
                points.append((run_id, "forecast", point["period"], result["selected_model"],
                               None, point["prediction"], _json(point)))
            connection.executemany("INSERT INTO forecast_points VALUES (?,?,?,?,?,?,?)", points)
    return forecast_report(database_path, run_id)


def list_forecast_runs(database_path: Path, snapshot_id: int, sku: str | None = None) -> list[dict]:
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        query = "SELECT id,snapshot_id,sku,created_at_utc,as_of,status,selected_model FROM forecast_runs WHERE snapshot_id=?"
        args: list[object] = [snapshot_id]
        if sku is not None:
            query += " AND sku=?"
            args.append(sku)
        return [dict(row) for row in connection.execute(query + " ORDER BY id DESC", args)]


def forecast_report(database_path: Path, run_id: int) -> dict:
    with closing(_connect(database_path)) as connection:
        row = connection.execute("SELECT * FROM forecast_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ValueError("Версия прогноза не найдена.")
        result = dict(row)
        result["run_id"] = result.pop("id")
        result["config"] = json.loads(result.pop("config_json"))
        result["summary"] = json.loads(result.pop("summary_json"))
        result["supplier"] = _snapshot(connection, row["snapshot_id"])["supplier"]
        result["dataset"] = dataset_context(database_path)
        result["points"] = [json.loads(item[0]) for item in connection.execute(
            "SELECT payload_json FROM forecast_points WHERE run_id=? ORDER BY kind,period", (run_id,),
        )]
        return result
