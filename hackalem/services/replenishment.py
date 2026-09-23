"""Versioned headless scenario calculations from checked stock and prepared demand."""

import json
import copy
from contextlib import closing
from datetime import timedelta
from math import isfinite
from pathlib import Path

from hackalem.domain.quality_config import iso_date
from hackalem.domain.replenishment import RULES_VERSION, calculate_replenishment
from hackalem.services.cleaning import cleaning_report, prepared_input
from hackalem.services.datasets import dataset_context
from hackalem.services.forecasting import forecast_report
from hackalem.services.quality import quality_report
from hackalem.services.systeme import _connect, _hash, _json, _now
from hackalem.storage import initialize_database


def run_replenishment(database_path, payload):
    """Save an immutable calculation. Explicit manual forecasts always mean scenario."""
    required = {"quality_run_id", "cleaning_run_id", "as_of", "supplier", "warehouse", "items"}
    allowed = required | {"scenario"}
    if (not isinstance(payload, dict) or not required <= set(payload)
            or not set(payload) <= allowed):
        raise ValueError("Расчёт требует quality_run_id, cleaning_run_id, as_of, supplier, warehouse, items.")
    if payload["warehouse"] != "all_selected_warehouses":
        raise ValueError("История пока охватывает только all_selected_warehouses; отдельный склад не поддержан.")
    if not isinstance(payload["items"], list) or not payload["items"]:
        raise ValueError("Расчёт требует непустой массив items.")
    scenario = payload.get("scenario")
    if scenario is not None:
        if not isinstance(scenario, dict) or set(scenario) != {
            "base_run_id", "demand_factor", "arrival_delay_days", "author", "reason"
        }:
            raise ValueError("Сценарий требует base_run_id, demand_factor, arrival_delay_days, author, reason.")
        if isinstance(scenario["base_run_id"], bool) or not isinstance(scenario["base_run_id"], int) or scenario["base_run_id"] < 1:
            raise ValueError("Сценарию нужен корректный base_run_id.")
        factor, delay = scenario["demand_factor"], scenario["arrival_delay_days"]
        if (isinstance(factor, bool) or not isinstance(factor, (int, float)) or
                not isfinite(factor) or not 0 <= factor <= 10):
            raise ValueError("Сценарный коэффициент спроса должен быть от 0 до 10.")
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay <= 365:
            raise ValueError("Сценарная задержка должна быть целым числом от 0 до 365 дней.")
        if not all(isinstance(scenario[key], str) and scenario[key].strip()
                   for key in ("author", "reason")):
            raise ValueError("Сценарию нужны автор и основание.")
    as_of = iso_date(payload["as_of"]).isoformat()
    initialize_database(Path(database_path))
    checked = quality_report(database_path, payload["quality_run_id"], limit=1)
    cleaned = cleaning_report(database_path, payload["cleaning_run_id"], limit=1)
    if (checked["snapshot_id"] != cleaned["snapshot_id"] or
            checked["summary"]["as_of"] != as_of or cleaned["as_of"] != as_of):
        raise ValueError("Версии качества, подготовки и дата расчёта должны относиться к одному снимку и срезу.")
    if checked["supplier"] != payload["supplier"]:
        raise ValueError("Поставщик не совпадает со снимком.")
    if scenario is not None:
        base = replenishment_report(database_path, scenario["base_run_id"])
        base_payload = base["input"]["payload"]
        stable_fields = ("quality_run_id", "cleaning_run_id", "as_of", "supplier",
                         "warehouse", "items")
        if (base["input"]["snapshot_id"] != checked["snapshot_id"] or
                any(base_payload.get(key) != payload.get(key) for key in stable_fields)):
            raise ValueError("Базовый расчёт сценария должен использовать те же данные, версии и товары.")
    skus = [item.get("sku") for item in payload["items"] if isinstance(item, dict)]
    if len(skus) != len(payload["items"]) or any(not isinstance(sku, str) or not sku for sku in skus) or len(skus) != len(set(skus)):
        raise ValueError("Каждый item требует уникальный непустой sku.")
    results = []
    for item in payload["items"]:
        sku = item["sku"]
        try:
            allowed = {"sku", "category_code", "forecast", "forecast_run_id", "project_commitments"}
            if not set(item) <= allowed or not {"sku", "category_code", "project_commitments"} <= set(item):
                raise ValueError("Item требует sku, category_code, project_commitments и один источник прогноза.")
            if ("forecast" in item) == ("forecast_run_id" in item):
                raise ValueError("Укажите ровно один источник: forecast_run_id или ручной forecast.")
            prepared = prepared_input(database_path, payload["quality_run_id"],
                                      payload["cleaning_run_id"], sku, allow_scenario=True,
                                      allow_missing_order_constraints=True,
                                      allow_overdue_arrivals=True)
            prepared = copy.deepcopy(prepared)
            if scenario and scenario["arrival_delay_days"]:
                for arrival in prepared.get("incoming", []):
                    eta = arrival.get("eta", {}).get("eta")
                    if eta:
                        arrival["eta"]["eta"] = (
                            iso_date(eta) + timedelta(days=scenario["arrival_delay_days"])
                        ).isoformat()
            if prepared["effective_values"].get("category_code") != item["category_code"]:
                raise ValueError("Категория не совпадает с проверенной политикой SKU.")
            source_forecast = None
            if "forecast_run_id" in item:
                source_forecast = forecast_report(database_path, item["forecast_run_id"])
                if (source_forecast["snapshot_id"] != checked["snapshot_id"] or
                        source_forecast["quality_run_id"] != payload["quality_run_id"] or
                        source_forecast["cleaning_run_id"] != payload["cleaning_run_id"] or
                        source_forecast["sku"] != sku or source_forecast["as_of"] != as_of):
                    raise ValueError("Прогноз относится к другому снимку, версиям, SKU или срезу.")
                summary = source_forecast["summary"]
                if not summary.get("forecasts"):
                    raise ValueError("Сохранённый прогноз не содержит будущих значений.")
                forecast = {
                    "basis": "regular_unreserved", "version": f"forecast-run:{item['forecast_run_id']}",
                    "source": "stage8_forecast", "author": "versioned forecasting service",
                    "reason": "Сохранённый прогноз этапа 8.", "granularity": "monthly",
                    "unit": summary["unit"],
                    "values": [{"period": row["period"], "quantity": row["prediction"]}
                               for row in summary["forecasts"]],
                    "adjustments": [summary["growth_decision"]],
                }
            else:
                forecast = copy.deepcopy(item["forecast"])
            if scenario:
                forecast = copy.deepcopy(forecast)
                for point in forecast["values"]:
                    point["quantity"] *= scenario["demand_factor"]
                forecast.setdefault("adjustments", []).append({
                    "kind": "ui_scenario", **scenario,
                })
            if not isinstance(forecast, dict) or forecast.get("unit") != prepared["effective_values"].get("accounting_unit"):
                raise ValueError("Единица прогноза должна совпадать с учётной единицей SKU.")
            result = calculate_replenishment(prepared, forecast, as_of=as_of,
                                             project_commitments=item["project_commitments"])
            result["dataset"] = prepared["dataset"]
            result["name"] = prepared.get("name")
            result["category_code"] = item["category_code"]
            result["supplier"] = payload["supplier"]
            result["warehouse"] = payload["warehouse"]
            result["scenario_parameters"] = scenario
            result["scenario"] = (source_forecast is None or prepared["status"] == "Сценарный расчёт" or
                                  source_forecast["status"] == "Сценарный прогноз" or
                                  prepared["dataset"]["kind"] == "synthetic" or scenario is not None)
            if result["status"] == "calculated" and result["scenario"]:
                result["status"] = "scenario"
            result["explanation"]["sources"].update({
                "quality_rules_version": prepared["quality_rules_version"],
                "cleaning_rules_version": prepared["cleaning_rules_version"],
                "forecast_version": forecast["version"],
                "forecast_run_id": item.get("forecast_run_id"),
                "forecast_rules_version": source_forecast.get("rules_version") if source_forecast else None,
                "forecast_model": source_forecast.get("selected_model") if source_forecast else None,
                "lost_demand_run_id": source_forecast.get("lost_demand_run_id") if source_forecast else None,
                "dataset": prepared["dataset"],
            })
        except (ValueError, KeyError, TypeError) as error:
            result = {"sku": sku, "status": "blocked", "scenario": True,
                      "reasons": [str(error)], "order_quantity": None}
        results.append(result)
    code_version = _hash(Path(__file__).read_bytes() +
                         (Path(__file__).parents[1] / "domain" / "replenishment.py").read_bytes())
    descriptor = {"payload": payload, "snapshot_id": checked["snapshot_id"],
                  "dataset": dataset_context(database_path), "rules_version": RULES_VERSION,
                  "code_version": code_version,
                  "quality_fingerprint": checked["fingerprint"],
                  "cleaning_fingerprint": cleaned["fingerprint"]}
    fingerprint = _hash(_json(descriptor).encode("utf-8"))
    summary = {"items": len(results), "scenario": sum(row["status"] == "scenario" for row in results),
               "incomplete_constraints": sum(row["status"] == "incomplete_constraints" for row in results),
               "blocked": sum(row["status"] == "blocked" for row in results),
               "urgent": sum(row.get("urgent_problem", False) for row in results)}
    with closing(_connect(database_path)) as connection, connection:
        previous = connection.execute("SELECT id FROM replenishment_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if previous:
            run_id = previous["id"]
        else:
            run_id = connection.execute("""INSERT INTO replenishment_runs
                (snapshot_id,quality_run_id,cleaning_run_id,fingerprint,created_at_utc,as_of,
                 rules_version,code_version,input_json,summary_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (checked["snapshot_id"], payload["quality_run_id"], payload["cleaning_run_id"],
                 fingerprint, _now(), as_of, RULES_VERSION, code_version,
                 _json(descriptor), _json(summary))).lastrowid
            connection.executemany("INSERT INTO replenishment_items VALUES (?,?,?)",
                                   ((run_id, row["sku"], _json(row)) for row in results))
    return replenishment_report(database_path, run_id)


def replenishment_report(database_path, run_id, *, sku=None):
    with closing(_connect(database_path)) as connection:
        run = connection.execute("SELECT * FROM replenishment_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Расчёт пополнения не найден.")
        result = dict(run)
        result["run_id"] = result.pop("id")
        result["input"] = json.loads(result.pop("input_json"))
        result["summary"] = json.loads(result.pop("summary_json"))
        if sku is None:
            rows = connection.execute("SELECT payload_json FROM replenishment_items WHERE run_id=? ORDER BY sku", (run_id,))
        else:
            rows = connection.execute("SELECT payload_json FROM replenishment_items WHERE run_id=? AND sku=?", (run_id, sku))
        result["items"] = [json.loads(row[0]) for row in rows]
        return result


def list_replenishment_runs(database_path, snapshot_id=None):
    with closing(_connect(database_path)) as connection:
        query = """SELECT r.id,r.snapshot_id,r.quality_run_id,r.cleaning_run_id,r.created_at_utc,
                   r.as_of,s.supplier,r.summary_json
            FROM replenishment_runs r JOIN snapshots s ON s.id=r.snapshot_id"""
        args = []
        if snapshot_id is not None:
            query += " WHERE r.snapshot_id=?"
            args.append(snapshot_id)
        rows = []
        for row in connection.execute(query + " ORDER BY r.id DESC", args):
            value = dict(row)
            value["summary"] = json.loads(value.pop("summary_json"))
            rows.append(value)
        return rows
