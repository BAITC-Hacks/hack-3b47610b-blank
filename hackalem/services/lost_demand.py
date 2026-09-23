"""Versioned lost-demand assessment without access to synthetic oracle truth."""

import calendar
import json
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, timedelta
from math import isfinite
from pathlib import Path

from hackalem.domain.lost_demand import RULES_VERSION, estimate_days
from hackalem.domain.quality_config import iso_date
from hackalem.services.datasets import dataset_context
from hackalem.services.systeme import _code_manifest, _connect, _hash, _json, _now
from hackalem.storage import initialize_database


def _scenario_evidence(intervals, as_of):
    if not isinstance(intervals, list) or not intervals:
        raise ValueError("Сценарные интервалы должны быть непустым JSON-массивом.")
    expanded = {}
    for item in intervals:
        required = {"start", "end", "state", "observed_hours_per_day", "scope", "author", "reason"}
        if not isinstance(item, dict) or set(item) != required:
            raise ValueError("Интервал требует start, end, state, observed_hours_per_day, scope, author, reason.")
        start, end = iso_date(item["start"]), iso_date(item["end"])
        if start > end or end >= iso_date(as_of) or (end - start).days > 3660:
            raise ValueError("Интервал должен предшествовать дате среза и не превышать 3661 день.")
        if item["state"] not in ("available", "out_of_stock", "unknown"):
            raise ValueError("Состояние интервала: available, out_of_stock или unknown.")
        if item["scope"] != "all_selected_warehouses":
            raise ValueError("Пока поддерживается только охват all_selected_warehouses для выбранного SKU.")
        hours = item["observed_hours_per_day"]
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or not isfinite(hours) or not 0 <= hours <= 24:
            raise ValueError("Длительность наблюдения должна быть от 0 до 24 часов за день.")
        if not all(isinstance(item[key], str) and item[key].strip() for key in ("author", "reason")):
            raise ValueError("Интервал требует автора и непустое основание.")
        day = start
        while day <= end:
            key = day.isoformat()
            if key in expanded:
                raise ValueError("Сценарные интервалы пересекаются: " + key)
            expanded[key] = {"date": key, "available": {"available": True, "out_of_stock": False,
                                                          "unknown": None}[item["state"]],
                             "observed_hours": hours, "author": item["author"], "reason": item["reason"]}
            day += timedelta(days=1)
    return [expanded[key] for key in sorted(expanded)]


def _synthetic_evidence(connection, sku, as_of):
    return [{**dict(row), "available": None if row["available"] is None else bool(row["available"])}
            for row in connection.execute("""SELECT a.date,a.available,a.observed_hours
        FROM synthetic_availability a JOIN synthetic_products p ON p.sku=a.sku
        WHERE a.sku=? AND a.date>=p.launch_date AND a.date<? ORDER BY a.date""", (sku, as_of))]


def _daily_sales(connection, cleaning_run_id, sku, as_of):
    grouped = defaultdict(list)
    for row in connection.execute("""SELECT payload_json FROM cleaning_documents
        WHERE run_id=? AND sku=?""", (cleaning_run_id, sku)):
        item = json.loads(row[0])
        day = item["occurred_on"]
        if day and day < as_of:
            grouped[day].append(item)
    result = {}
    for day, items in grouped.items():
        ready = all(item["regular_quantity"] is not None and
                    item["status"] not in ("candidate", "high_confidence_candidate", "needs_review")
                    for item in items)
        result[day] = {"state": "value" if ready else "needs_review",
                       "quantity": sum(item["regular_quantity"] for item in items) if ready else None,
                       "document_keys": [item["document_key"] for item in items]}
    return result


def _monthly_results(connection, cleaning_run_id, sku, as_of, days, source):
    by_month = defaultdict(list)
    for item in days:
        by_month[item["period"]].append(item)
    result = []
    for record in connection.execute("""SELECT payload_json FROM cleaning_months
        WHERE run_id=? AND sku=? ORDER BY period""", (cleaning_run_id, sku)):
        prepared = json.loads(record[0])
        period = prepared["period"]
        if period[:7] >= as_of[:7]:
            continue
        evidence = by_month[period]
        known_absent = [item for item in evidence if item["available"] is False and item["observed_hours"] == 24]
        unresolved = [item for item in evidence if item["state"] not in ("available", "estimated")]
        days_in_month = calendar.monthrange(int(period[:4]), int(period[5:7]))[1]
        coverage = "full_month" if len(evidence) == days_in_month else "partial_month"
        observed = prepared["regular_quantity"] if prepared["state"] == "value" else None
        estimated = sum(item["estimated_lost_quantity"] for item in known_absent
                        if item["state"] == "estimated")
        if source == "real_without_daily_availability":
            state, adjusted, lost = "exact_unavailable", None, None
            reason = "В реальных файлах нет полнодневного журнала наличия или полного журнала движений."
        elif observed is None or any(item["state"] != "estimated" for item in known_absent):
            state, adjusted, lost = "needs_review", None, None
            reason = "Исходный регулярный спрос или подтверждённое отсутствие не разрешены."
        elif source == "synthetic_full_day" and (coverage != "full_month" or unresolved):
            state, adjusted, lost = "needs_review", None, None
            reason = "Нет полной дневной истории наличия и известных продаж за месяц."
        else:
            lost = estimated
            adjusted = observed + lost
            state = "scenario" if source == "manual_scenario" else "value"
            reason = ("Сценарная оценка только для вручную указанных полных дней; охват прочих дней не подтверждён."
                      if state == "scenario" else "Историческая поправка для обучения, не задолженность к новому заказу.")
        result.append({"sku": sku, "period": period, "state": state, "reason": reason,
                       "evidence_coverage": coverage, "evidence_days": len(evidence),
                       "confirmed_stockout_days": len(known_absent),
                       "unknown_or_unresolved_days": len(unresolved),
                       "observed_regular_quantity": observed, "estimated_lost_quantity": lost,
                       "adjusted_training_quantity": adjusted,
                       "historical_lost_demand_as_current_backlog": 0,
                       "method": RULES_VERSION, "source_document_keys": prepared["source_document_keys"]})
    return result


def run_lost_demand(database_path, cleaning_run_id, sku, *, scenario_intervals=None):
    """Persist one SKU assessment. An earlier cutoff requires an earlier cleaning run."""
    database_path = Path(database_path)
    initialize_database(database_path)
    with closing(_connect(database_path)) as connection:
        cleaning = connection.execute("SELECT * FROM cleaning_runs WHERE id=?", (cleaning_run_id,)).fetchone()
        if cleaning is None:
            raise ValueError("Версия подготовки регулярного спроса не найдена.")
        as_of, snapshot_id = cleaning["as_of"], cleaning["snapshot_id"]
        known = connection.execute("""SELECT 1 FROM products p JOIN snapshot_files sf ON sf.file_id=p.file_id
            WHERE sf.snapshot_id=? AND p.sku=? LIMIT 1""", (snapshot_id, sku)).fetchone()
        if known is None:
            raise ValueError("SKU отсутствует в выбранном снимке.")
    dataset = dataset_context(database_path)
    if scenario_intervals is not None:
        evidence = _scenario_evidence(scenario_intervals, as_of)
        source = "manual_scenario"
        descriptor = {"kind": source, "intervals": scenario_intervals}
    elif dataset["kind"] == "synthetic":
        source = "synthetic_full_day"
        descriptor = {"kind": source, "dataset_id": dataset["dataset_id"]}
        with closing(_connect(database_path)) as connection:
            evidence = _synthetic_evidence(connection, sku, as_of)
    else:
        source = "real_without_daily_availability"
        descriptor = {"kind": source}
        evidence = []
    code_version, _ = _code_manifest()
    fingerprint = _hash(_json({"cleaning_run": cleaning_run_id, "snapshot": snapshot_id, "sku": sku,
                               "as_of": as_of, "rules": RULES_VERSION, "code": code_version,
                               "evidence": descriptor}).encode())
    with closing(_connect(database_path)) as connection:
        previous = connection.execute("SELECT id FROM lost_demand_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if previous:
            return lost_demand_report(database_path, previous["id"])
        sales = _daily_sales(connection, cleaning_run_id, sku, as_of)
        days = estimate_days(evidence, sales, as_of)
        months = _monthly_results(connection, cleaning_run_id, sku, as_of, days, source)
    unresolved_count = sum(item["state"] in ("needs_review", "exact_unavailable") for item in months)
    summary = {"source": source, "status_counts": dict(Counter(item["state"] for item in months)),
               "month_count": len(months), "evidence_day_count": len(days),
               "confirmed_stockout_days": sum(item["confirmed_stockout_days"] for item in months),
               "estimated_lost_quantity": (None if unresolved_count or not months else
                                           sum(item["estimated_lost_quantity"] for item in months)),
               "unresolved_months": unresolved_count,
               "full_day_evidence_available": source == "synthetic_full_day" and bool(months) and all(
                   item["state"] == "value" for item in months),
               "note": ("Точная коррекция недоступна: нет полного дневного журнала наличия."
                        if source == "real_without_daily_availability" else
                        "Сценарная поправка предназначена только для обучения; вручную введённые дни не подтверждены источником."
                        if source == "manual_scenario" else
                        "Историческая поправка предназначена только для обучения прогноза; текущий заказ не увеличен.")}
    with closing(_connect(database_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        previous = connection.execute("SELECT id FROM lost_demand_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if previous:
            run_id = previous["id"]
        else:
            run_id = connection.execute("""INSERT INTO lost_demand_runs
                (snapshot_id,cleaning_run_id,sku,fingerprint,created_at_utc,as_of,rules_version,
                 code_version,evidence_json,summary_json) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id, cleaning_run_id, sku, fingerprint, _now(), as_of, RULES_VERSION,
                 code_version, _json(descriptor), _json(summary))).lastrowid
            connection.executemany("INSERT INTO lost_demand_days VALUES (?,?,?,?)",
                                   ((run_id, row["date"], row["state"], _json(row)) for row in days))
            connection.executemany("INSERT INTO lost_demand_months VALUES (?,?,?,?)",
                                   ((run_id, row["period"], row["state"], _json(row)) for row in months))
    return lost_demand_report(database_path, run_id)


def list_lost_demand_runs(database_path, cleaning_run_id):
    with closing(_connect(database_path)) as connection:
        return [dict(row) for row in connection.execute("""SELECT id,cleaning_run_id,sku,as_of,created_at_utc
            FROM lost_demand_runs WHERE cleaning_run_id=? ORDER BY id DESC""", (cleaning_run_id,))]


def lost_demand_report(database_path, run_id, *, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("Лимит отчёта должен быть от 1 до 10000.")
    with closing(_connect(database_path)) as connection:
        run = connection.execute("SELECT * FROM lost_demand_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Оценка упущенного спроса не найдена.")
        result = dict(run)
        result["run_id"] = result.pop("id")
        result["summary"] = json.loads(result.pop("summary_json"))
        result["evidence"] = json.loads(result.pop("evidence_json"))
        result["dataset"] = dataset_context(database_path)
        result["months"] = [json.loads(row[0]) for row in connection.execute(
            "SELECT payload_json FROM lost_demand_months WHERE run_id=? ORDER BY period", (run_id,))]
        result["days_total"] = connection.execute(
            "SELECT COUNT(*) FROM lost_demand_days WHERE run_id=?", (run_id,)).fetchone()[0]
        result["days"] = [json.loads(row[0]) for row in connection.execute("""SELECT payload_json
            FROM lost_demand_days WHERE run_id=? ORDER BY
            CASE WHEN state IN ('estimated','insufficient_history','extrapolation_limit','needs_review') THEN 0 ELSE 1 END,
            day LIMIT ?""", (run_id, limit))]
        return result


def adjusted_history(database_path, run_id):
    """Training-only history; never treat historical lost demand as backlog."""
    report = lost_demand_report(database_path, run_id, limit=1)
    if report["summary"]["source"] == "real_without_daily_availability":
        raise ValueError("Точная коррекция недоступна: нет полного журнала наличия.")
    if not report["months"] or any(row["state"] not in ("value", "scenario") for row in report["months"]):
        raise ValueError("История содержит месяцы, требующие проверки.")
    return {"run_id": run_id, "snapshot_id": report["snapshot_id"], "sku": report["sku"],
            "as_of": report["as_of"], "scenario": report["summary"]["source"] == "manual_scenario" or
            report["dataset"]["kind"] == "synthetic",
            "history": [{"period": item["period"], "observed_regular_quantity": item["observed_regular_quantity"],
                         "estimated_lost_quantity": item["estimated_lost_quantity"],
                         "quantity": item["adjusted_training_quantity"]} for item in report["months"]],
            "historical_lost_demand_as_current_backlog": 0}
