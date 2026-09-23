"""Versioned regular-demand preparation over immutable transaction observations."""

import json
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from hackalem.domain.cleaning import RULES_VERSION, aggregate_months, classify_documents, validate_rules
from hackalem.domain.quality_config import iso_date
from hackalem.services.datasets import dataset_context
from hackalem.services.systeme import _code_manifest, _connect, _hash, _json, _now, _snapshot
from hackalem.storage import initialize_database


def _observed_documents(connection, snapshot_id, as_of):
    customer_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='synthetic_customers'"
    ).fetchone() is not None
    customer_join = ("LEFT JOIN synthetic_customers c ON c.file_id=t.file_id AND c.sheet=t.sheet AND c.row=t.row"
                     if customer_table else "")
    customer_column = "c.customer_id" if customer_table else "NULL"
    rows = connection.execute(f"""SELECT t.*, {customer_column} AS customer_id
        FROM transactions t JOIN snapshot_files sf ON sf.file_id=t.file_id
        {customer_join} WHERE sf.snapshot_id=? AND sf.source_kind='transactions'
        ORDER BY t.file_id, t.sheet, t.row""", (snapshot_id,))
    grouped = defaultdict(list)
    for row in rows:
        row = dict(row)
        date = (row["occurred_at"] or "")[:10]
        # An invalid date is preserved as a review item; a current/future day is not history.
        try:
            valid_date = iso_date(date).isoformat()
        except ValueError:
            valid_date = None
        if valid_date and (valid_date >= as_of or valid_date[:7] >= as_of[:7]):
            continue
        doc = (row["document_number"] or "").strip()
        group = (row["file_id"], row["sheet"], row["sku"], valid_date,
                 doc if doc else f"__row_{row['row']}")
        grouped[group].append(row)
    result = []
    for (file_id, sheet, sku, date, _), lines in grouped.items():
        anchor = min(line["row"] for line in lines)
        types = {(line["document_type"] or "").strip() for line in lines}
        units = {(line["unit"] or "").strip().casefold() for line in lines}
        customers = {line["customer_id"] for line in lines}
        all_valid = all(line["state"] == "value" and line["quantity"] is not None for line in lines)
        state = "value" if all_valid and len(types) == len(units) == len(customers) == 1 and date else "partial"
        known = sum(line["quantity"] for line in lines if line["state"] == "value" and line["quantity"] is not None)
        result.append({
            "document_key": f"{file_id}:{sheet}:{anchor}", "sku": sku,
            "period": date[:7] + "-01" if date else None, "occurred_on": date,
            "document_number": lines[0]["document_number"],
            "document_type": next(iter(types)) if len(types) == 1 else None,
            "unit": lines[0]["unit"] if len(units) == 1 else None,
            "customer_id": next(iter(customers)) if len(customers) == 1 else None,
            "source_state": state, "raw_quantity": known if state == "value" else None,
            "known_signed_subtotal": known,
            "lineage": [{"file_id": line["file_id"], "sheet": line["sheet"],
                         "row": line["row"], "cell": line["cell"], "state": line["state"],
                         "quantity": line["quantity"]} for line in lines],
        })
    return sorted(result, key=lambda item: (item["sku"], item["period"] or "", item["document_key"]))


def run_cleaning(database_path, snapshot_id, as_of, *, policy="review_only", decisions=None):
    """Create or reuse an immutable preparation run for one explicit closed history."""
    database_path = Path(database_path)
    initialize_database(database_path)
    as_of = iso_date(as_of).isoformat()
    chosen = validate_rules(policy, [] if decisions is None else decisions)
    decisions = [chosen[key] for key in sorted(chosen)]
    code_version, _ = _code_manifest()
    fingerprint = _hash(_json({"snapshot": snapshot_id, "as_of": as_of, "rules": RULES_VERSION,
                               "code": code_version, "policy": policy, "decisions": decisions}).encode())
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        prior = connection.execute("SELECT id FROM cleaning_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if prior:
            return cleaning_report(database_path, prior["id"])
        observed = _observed_documents(connection, snapshot_id, as_of)
    classified = classify_documents(observed, policy=policy, decisions=decisions)
    monthly = aggregate_months(classified)
    summary = {"document_count": len(classified), "sku_count": len({row["sku"] for row in classified}),
               "month_count": len(monthly), "status_counts": dict(Counter(row["status"] for row in classified)),
               "month_state_counts": dict(Counter(row["state"] for row in monthly)),
               "client_analysis": "available" if any(row["customer_id"] for row in classified) else "unavailable",
               "description": "Проверяемая подготовка регулярного спроса; исходные строки неизменны."}
    with closing(_connect(database_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        prior = connection.execute("SELECT id FROM cleaning_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if prior:
            run_id = prior["id"]
        else:
            run_id = connection.execute("""INSERT INTO cleaning_runs
                (snapshot_id,fingerprint,created_at_utc,as_of,rules_version,code_version,policy,decisions_json,summary_json)
                VALUES (?,?,?,?,?,?,?,?,?)""", (snapshot_id, fingerprint, _now(), as_of, RULES_VERSION,
                                                code_version, policy, _json(decisions), _json(summary))).lastrowid
            connection.executemany("INSERT INTO cleaning_documents VALUES (?,?,?,?,?,?)",
                                   ((run_id, row["document_key"], row["sku"], row["period"], row["status"], _json(row))
                                    for row in classified))
            connection.executemany("INSERT INTO cleaning_months VALUES (?,?,?,?,?)",
                                   ((run_id, row["sku"], row["period"], row["state"], _json(row)) for row in monthly))
            connection.executemany("INSERT INTO cleaning_decisions VALUES (?,?,?,?,?,?)",
                                   ((run_id, entry["document_key"], entry["action"], entry["author"], entry["reason"],
                                     entry["project_commitment_quantity"]) for entry in decisions))
    return cleaning_report(database_path, run_id)


def list_cleaning_runs(database_path, snapshot_id):
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        return [dict(row) for row in connection.execute(
            "SELECT id,snapshot_id,created_at_utc,as_of,policy FROM cleaning_runs WHERE snapshot_id=? ORDER BY id DESC",
            (snapshot_id,))]


def cleaning_report(database_path, run_id, *, sku=None, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("Лимит отчёта должен быть от 1 до 10000.")
    with closing(_connect(database_path)) as connection:
        run = connection.execute("SELECT * FROM cleaning_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Результат подготовки не найден.")
        result = dict(run)
        result["run_id"] = result.pop("id")
        result["summary"] = json.loads(result.pop("summary_json"))
        result["decisions"] = json.loads(result.pop("decisions_json"))
        result["supplier"] = _snapshot(connection, run["snapshot_id"])["supplier"]
        result["dataset"] = dataset_context(database_path)
        for name, table in (("documents", "cleaning_documents"), ("months", "cleaning_months")):
            where, args = "run_id=?", [run_id]
            if sku is not None:
                where += " AND sku=?"
                args.append(sku)
            ordering = ("CASE WHEN status IN ('needs_review','candidate','high_confidence_candidate') THEN 0 ELSE 1 END, sku,period,document_key"
                        if name == "documents" else "sku,period")
            result[name] = [json.loads(row[0]) for row in connection.execute(
                f"SELECT payload_json FROM {table} WHERE {where} ORDER BY {ordering} LIMIT ?", (*args, limit))]
            result[name + "_total"] = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
        return result


def prepared_input(database_path, quality_run_id, cleaning_run_id, sku, *, allow_scenario=False,
                   allow_missing_order_constraints=False, allow_overdue_arrivals=False):
    """Require both versioned gates before a future forecast consumes prepared sales."""
    from hackalem.services.quality import ENOUGH, SCENARIO, quality_report

    checked = quality_report(database_path, quality_run_id, sku=sku, limit=1)
    if not checked["skus"]:
        raise ValueError("SKU отсутствует в проверке качества.")
    prepared = cleaning_report(database_path, cleaning_run_id, sku=sku, limit=10000)
    if checked["snapshot_id"] != prepared["snapshot_id"]:
        raise ValueError("Проверка качества и подготовка относятся к разным снимкам.")
    if checked["summary"]["as_of"] != prepared["as_of"]:
        raise ValueError("Даты среза проверки качества и подготовки должны совпадать.")
    original = checked["skus"][0]
    history = checked["selected_sales"]
    if not history or any(row["source"] != "transactions" for row in history):
        raise ValueError("Для подготовленной истории нужен явный выбор транзакций за каждый месяц.")
    months = {row["period"]: row for row in prepared["months"]}
    cleaned = []
    for row in history:
        month = months.get(row["period"])
        if month is None or month["state"] != "value" or month["regular_quantity"] is None:
            raise ValueError(f"Регулярный спрос {sku} за {row['period']} не подготовлен или требует решения.")
        cleaned.append({**row, "raw_quantity": row["quantity"], "quantity": month["regular_quantity"],
                        "state": "value", "return_quantity": month["return_quantity"],
                        "removed_component": month["removed_component"],
                        "project_commitment_quantity": month["project_commitment_quantity"],
                        "cleaning_document_keys": month["source_document_keys"]})
    reasons = [reason for reason in original["reasons"] if not reason.startswith("NEGATIVE_SELECTED_SALES:")]
    if allow_missing_order_constraints:
        reasons = [reason for reason in reasons if reason not in (
            "PARAMETER_MISSING: Не задан параметр minimum_order.",
            "PARAMETER_MISSING: Не задан параметр order_multiple.")]
    if allow_overdue_arrivals:
        reasons = [reason for reason in reasons if not reason.startswith("ETA_OVERDUE:")]
    if reasons:
        raise ValueError("Не хватает данных: " + "; ".join(reasons))
    status = SCENARIO if (original["assumptions"] or checked["dataset"]["kind"] == "synthetic" or
                          ((allow_missing_order_constraints or allow_overdue_arrivals) and original["reasons"])) else ENOUGH
    if status == SCENARIO and allow_scenario is not True:
        raise ValueError("Сценарный расчёт не допускается в подтверждённый реальный заказ.")
    return {**original, "status": status, "eligible_for_calculation": True,
            "eligible_for_confirmed_order": status == ENOUGH, "reasons": [],
            "raw_history": history, "history": cleaned,
            "quality_run_id": quality_run_id, "cleaning_run_id": cleaning_run_id,
            "snapshot_id": checked["snapshot_id"], "dataset": checked["dataset"],
            "quality_rules_version": checked["rules_version"], "cleaning_rules_version": prepared["rules_version"]}
