"""Versioned source decisions, explicit parameters, and a pre-calculation gate."""

import json
from collections import Counter, defaultdict
from contextlib import closing

from hackalem.domain.quality_config import (
    REQUIRED_PARAMETERS, configuration_template, months, validate_configuration,
)
from hackalem.services.systeme import _code_manifest, _connect, _hash, _json, _now, _snapshot
from hackalem.storage import initialize_database
from hackalem.services.datasets import dataset_context

RULES_VERSION = "quality-1"
ENOUGH = "Достаточно данных"
SCENARIO = "Сценарный расчёт"
MISSING = "Не хватает данных"


def _known_skus(connection, snapshot_id):
    codes = set()
    for table in ("products", "transactions", "monthly_values", "measures", "incoming_orders"):
        codes.update(row[0] for row in connection.execute(
            f"SELECT DISTINCT p.sku FROM {table} p JOIN snapshot_files s ON s.file_id=p.file_id WHERE s.snapshot_id=?",
            (snapshot_id,),
        ) if row[0])
    return codes


def save_configuration(database_path, snapshot_id, payload):
    initialize_database(database_path)
    with closing(_connect(database_path)) as connection, connection:
        _snapshot(connection, snapshot_id)
        sources = {row[0] for row in connection.execute("SELECT source_kind FROM snapshot_files WHERE snapshot_id=?", (snapshot_id,))}
        validated = validate_configuration(payload, _known_skus(connection, snapshot_id), sources)
        serialized = _json(validated)
        fingerprint = _hash(_json({"snapshot": snapshot_id, "payload": validated}).encode())
        connection.execute(
            """INSERT OR IGNORE INTO quality_configurations (snapshot_id, fingerprint, created_at_utc, payload_json)
            VALUES (?, ?, ?, ?)""", (snapshot_id, fingerprint, _now(), serialized),
        )
        row = dict(connection.execute("SELECT * FROM quality_configurations WHERE fingerprint=?", (fingerprint,)).fetchone())
        row["payload"] = json.loads(row.pop("payload_json"))
        return row


def list_configurations(database_path, snapshot_id):
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        return [dict(row) for row in connection.execute(
            "SELECT id, snapshot_id, created_at_utc FROM quality_configurations WHERE snapshot_id=? ORDER BY id DESC", (snapshot_id,))]


def get_configuration(database_path, configuration_id):
    with closing(_connect(database_path)) as connection:
        row = connection.execute("SELECT * FROM quality_configurations WHERE id=?", (configuration_id,)).fetchone()
        if row is None:
            raise ValueError("Конфигурация не найдена.")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result


def _incoming(connection, snapshot_id):
    result = defaultdict(list)
    for row in connection.execute(
        """SELECT s.source_kind, i.file_id, i.sheet, i.cell, i.sku, i.quantity, i.state,
        i.order_number, i.order_date, i.eta_deadline, i.unit
        FROM incoming_orders i JOIN snapshot_files s ON s.file_id=i.file_id WHERE s.snapshot_id=?""", (snapshot_id,),
    ):
        result[row["sku"]].append(dict(row))
    for row in connection.execute(
        """SELECT s.source_kind, m.file_id, m.sheet, m.cell, m.sku, m.number AS quantity, m.state
        FROM measures m JOIN snapshot_files s ON s.file_id=m.file_id
        WHERE s.snapshot_id=? AND m.metric='incoming_quantity'""", (snapshot_id,),
    ):
        result[row["sku"]].append(dict(row))
    return result


def _effective_parameters(payload, sku):
    result = {key: {**value, "origin": "defaults"} for key, value in payload["defaults"].items()}
    result.update({key: {**value, "origin": f"skus.{sku}"} for key, value in payload["skus"].get(sku, {}).items()})
    return result


def _choose_sales(payload, sku):
    choices = {}
    # A named SKU overrides a wildcard; ties within either scope were rejected.
    for scope in ("*", sku):
        for choice in payload["sales_choices"]:
            if choice["sku"] == scope:
                for period in months(choice["start"], choice["end"]):
                    choices[period] = choice
    return choices


def _unit(value):
    return value.strip().casefold().rstrip(".") if isinstance(value, str) else None


def _evaluate(product, payload, sales_index, incoming, audit_issues):
    sku = product["sku"]
    parameters = _effective_parameters(payload, sku)
    reasons, assumptions, selected, resolved_incoming = [], [], [], []

    def reason(code, explanation):
        text = f"{code}: {explanation}"
        if text not in reasons:
            reasons.append(text)

    for key in REQUIRED_PARAMETERS:
        if key not in parameters:
            reason("PARAMETER_MISSING", f"Не задан параметр {key}.")
    if payload["as_of"] is None:
        reason("AS_OF_MISSING", "Не задана дата актуального среза.")
    values = {key: entry["value"] for key, entry in parameters.items()}
    if "stock_date" in values and payload["as_of"] and values["stock_date"] != payload["as_of"]:
        reason("STOCK_DATE_MISMATCH", "Дата введённого актуального остатка не совпадает с датой среза.")
    if {"current_stock", "reserved_stock"} <= values.keys() and values["reserved_stock"] > values["current_stock"]:
        reason("RESERVE_EXCEEDS_STOCK", "Резерв больше физического остатка.")
    if values.get("accounting_unit") and product.get("units") and _unit(values["accounting_unit"]) not in {_unit(unit) for unit in product["units"]}:
        reason("ACCOUNTING_UNIT_CONFLICT", "Введённая учётная единица не соответствует исходным наблюдениям; нужен явный перевод исходной истории.")
    if values.get("accounting_unit") and _unit(values["accounting_unit"]) == _unit(values.get("purchase_unit")) and values.get("unit_factor", 1) != 1:
        reason("UNIT_FACTOR_CONFLICT", "Для одинаковых учётной и закупочной единиц коэффициент должен быть равен 1.")
    for key, entry in parameters.items():
        if entry["status"] == "scenario":
            assumptions.append(f"{key}: {entry['reason']}")
    choices = _choose_sales(payload, sku)
    if not choices:
        reason("SALES_SOURCE_MISSING", "Не выбран источник и период продаж.")
    chosen_sources = {choice["source"] for choice in choices.values()}
    for issue in audit_issues:
        if issue["code"] == "FACT_PERIOD_INVALID" and issue.get("evidence", {}).get("source_kind") in chosen_sources:
            reason("SELECTED_PERIOD_INVALID", "В выбранном источнике есть факт без определённого месяца; полнота выбранного периода не подтверждена.")
    if len(product.get("articles", [])) > 1:
        reason("SKU_ARTICLE_AMBIGUOUS", "Один SKU связан с несколькими артикулами; перед закупкой требуется однозначное соответствие.")
    for period, choice in sorted(choices.items()):
        observation = sales_index.get((choice["source"], sku, period))
        record = {"sku": sku, "period": period, "source": choice["source"], "source_kind": choice["source"],
                  "quantity": None, "state": "missing", "choice": choice, "provenance": None}
        if choice["status"] == "scenario":
            text = f"Продажи {choice['source']} {period}: {choice['reason']}"
            if text not in assumptions:
                assumptions.append(text)
        if payload["as_of"] and period[:7] >= payload["as_of"][:7]:
            reason("PERIOD_NOT_CLOSED", f"Месяц {period[:7]} не закрыт на дату среза; выберите завершённый период.")
        if observation is None:
            reason("SELECTED_OBSERVATION_MISSING", f"Нет наблюдения {choice['source']} за {period}; другой источник автоматически не подставлен.")
        else:
            record.update(quantity=observation["quantity"], state=observation["state"], provenance=observation.get("provenance"))
            record["original_state"] = observation["state"]
            zero_policy = parameters.get("blank_sales_policy")
            only_blanks = observation.get("blank_count", 0) > 0 and observation.get("error_count", 0) == 0
            if observation["state"] in ("missing", "blank", "partial") and only_blanks and zero_policy and zero_policy["value"] == "zero":
                record.update(quantity=observation["quantity"] if observation["quantity"] is not None else 0, state="value")
                record["blank_policy"] = zero_policy
            if record["state"] != "value" or record["quantity"] is None:
                reason("SELECTED_VALUE_UNRESOLVED", f"{choice['source']} {period}: выбранное значение {record['state']}, без замены альтернативой.")
            elif record["quantity"] < 0 or observation.get("negative_count", 0) > 0:
                reason("NEGATIVE_SELECTED_SALES", f"Отрицательные продажи {choice['source']} за {period} требуют отдельной обработки возвратов.")
            observation_units = {_unit(unit) for unit in observation.get("units", [])}
            if len(observation_units) > 1:
                reason("SALES_UNIT_CONFLICT", f"{choice['source']} {period}: смешаны единицы продаж.")
            elif observation_units and values.get("accounting_unit") and _unit(values["accounting_unit"]) not in observation_units:
                reason("SALES_UNIT_CONFLICT", f"{choice['source']} {period}: единица выбранной истории отличается от учётной.")
        selected.append(record)
    positive = [row for row in incoming if row["state"] == "value" and row["quantity"] is not None and row["quantity"] > 0]
    unresolved = [row for row in incoming if row["state"] == "error" or (row["quantity"] is not None and row["quantity"] < 0)]
    if unresolved:
        reason("INCOMING_UNRESOLVED", "В пути есть ошибочное или отрицательное количество; отсутствие заказов не устраняет ошибку.")
    eta_by_cell = {(item["source_kind"], item["sheet"], item["cell"]): item for item in values.get("eta_confirmations", [])}
    extra = set(eta_by_cell) - {(row["source_kind"], row["sheet"], row["cell"]) for row in positive}
    if extra:
        reason("ETA_REFERENCE_INVALID", "Подтверждение ETA ссылается на чужую или отсутствующую строку поступления.")
    if not positive:
        if values.get("no_open_orders") is not True:
            reason("INCOMING_COVERAGE_UNCONFIRMED", "Нет подтверждения отсутствия открытых заказов; пустые количества не считаются нулями.")
    else:
        if values.get("no_open_orders") is True:
            reason("OPEN_ORDERS_CONFLICT", "Заявлено отсутствие заказов, но в источнике есть положительные поступления.")
        eta_entry = parameters.get("eta_confirmations")
        incoming_unit = _unit(values.get("incoming_unit"))
        if incoming_unit is None:
            reason("INCOMING_UNIT_UNCONFIRMED", "Не задана единица количеств ожидаемых поставок.")
        elif incoming_unit not in {_unit(values.get("accounting_unit")), _unit(values.get("purchase_unit"))}:
            reason("INCOMING_UNIT_CONFLICT", "Единица пути не соответствует учётной или закупочной единице.")
        for arrival in positive:
            if arrival.get("unit") and incoming_unit and _unit(arrival["unit"]) != incoming_unit:
                reason("INCOMING_UNIT_CONFLICT", "Единица исходной строки поступления отличается от введённой единицы пути.")
            key = arrival["source_kind"], arrival["sheet"], arrival["cell"]
            eta = eta_by_cell.get(key)
            if eta is None:
                reason("ETA_UNCONFIRMED", f"Не подтверждена полная дата поступления {key}.")
                continue
            if payload["as_of"] and eta["eta"] < payload["as_of"]:
                reason("ETA_OVERDUE", f"Поступление {key} просрочено; нужен актуальный срок или факт приёмки.")
            factor = (1 if incoming_unit and incoming_unit == _unit(values.get("accounting_unit")) else
                      values.get("unit_factor") if incoming_unit and incoming_unit == _unit(values.get("purchase_unit")) else None)
            resolved_incoming.append({**arrival, "eta": eta, "confirmation": eta_entry,
                                      "unit_confirmation": parameters.get("incoming_unit"),
                                      "accounting_quantity": arrival["quantity"] * factor if factor is not None else None})
    status = MISSING if reasons else SCENARIO if assumptions else ENOUGH
    effective = {**values}
    if {"current_stock", "reserved_stock"} <= values.keys():
        effective["available_stock"] = values["current_stock"] - values["reserved_stock"]
    return {
        "sku": sku, "name": product.get("name"), "status": status,
        "eligible_for_calculation": status != MISSING,
        "eligible_for_confirmed_order": status == ENOUGH,
        "reasons": reasons, "assumptions": assumptions, "parameters": parameters,
        "effective_values": effective, "sources": product.get("sources", []),
        "incoming": resolved_incoming, "history": selected,
    }


def run_quality(database_path, snapshot_id, configuration_id=None):
    initialize_database(database_path)
    if configuration_id is None:
        configuration_id = save_configuration(database_path, snapshot_id, configuration_template())["id"]
    configuration = get_configuration(database_path, configuration_id)
    if configuration["snapshot_id"] != snapshot_id:
        raise ValueError("Конфигурация относится к другому снимку.")
    code_version, _ = _code_manifest()
    fingerprint = _hash(_json({"snapshot": snapshot_id, "configuration": configuration["fingerprint"],
                              "rules": RULES_VERSION, "code": code_version}).encode())
    with closing(_connect(database_path)) as connection:
        existing = connection.execute("SELECT id FROM quality_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
    if existing:
        return quality_report(database_path, existing["id"])
    from hackalem.services.reconciliation import analyze_snapshot
    audit = analyze_snapshot(database_path, snapshot_id)
    sales_index = {(row["source_kind"], row["sku"], row["period"]): row for row in audit["sales"]}
    with closing(_connect(database_path)) as connection:
        incoming = _incoming(connection, snapshot_id)
    issues_by_sku = defaultdict(list)
    for issue in audit["issues"]:
        issues_by_sku[issue.get("sku")].append(issue)
    evaluations = [_evaluate(product, configuration["payload"], sales_index, incoming[product["sku"]], issues_by_sku[product["sku"]]) for product in audit["skus"]]
    dataset = dataset_context(database_path)
    if dataset['kind'] == 'synthetic':
        for item in evaluations:
            item['eligible_for_confirmed_order'] = False
            item['assumptions'].append('Синтетический набор: входы предназначены только для проверочного сценария.')
            if item['status'] == ENOUGH:
                item['status'] = SCENARIO
    summary = {
        "dataset": dataset,
        "sku_count": len(evaluations), "status_counts": dict(Counter(item["status"] for item in evaluations)),
        "comparison_counts": dict(Counter(item["kind"] for item in audit["comparisons"])),
        "issue_count": len(audit["issues"]), "coverage": audit["coverage"],
        "as_of": configuration["payload"]["as_of"],
        "description": "Готовность входных данных; прогноз и заказ на этапе 4 не создаются.",
    }
    with closing(_connect(database_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        # A concurrent identical run must not create a second result.
        existing = connection.execute("SELECT id FROM quality_runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            run_id = existing["id"]
        else:
            cursor = connection.execute(
                """INSERT INTO quality_runs (snapshot_id, configuration_id, fingerprint, created_at_utc,
                rules_version, code_version, summary_json) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (snapshot_id, configuration_id, fingerprint, _now(), RULES_VERSION, code_version, _json(summary)),
            )
            run_id = cursor.lastrowid
            for item in evaluations:
                history = item.pop("history")
                connection.execute("INSERT INTO quality_skus VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   (run_id, item["sku"], item["name"], item["status"], item["eligible_for_calculation"], item["eligible_for_confirmed_order"], _json(item)))
                connection.executemany("INSERT INTO quality_selected_sales VALUES (?, ?, ?, ?)",
                                       ((run_id, item["sku"], row["period"], _json(row)) for row in history))
            connection.executemany("INSERT INTO quality_comparisons VALUES (?, ?, ?, ?, ?, ?)",
                                   ((run_id, n, item["sku"], item["period"], item["kind"], _json(item)) for n, item in enumerate(audit["comparisons"])))
            connection.executemany("INSERT INTO quality_issues VALUES (?, ?, ?, ?, ?, ?, ?)",
                                   ((run_id, n, item.get("sku"), item["code"], item["severity"], item["message"], _json(item.get("evidence", {}))) for n, item in enumerate(audit["issues"])))
    return quality_report(database_path, run_id)


def list_quality_runs(database_path, snapshot_id):
    with closing(_connect(database_path)) as connection:
        _snapshot(connection, snapshot_id)
        return [dict(row) for row in connection.execute(
            "SELECT id, configuration_id, created_at_utc FROM quality_runs WHERE snapshot_id=? ORDER BY id DESC", (snapshot_id,))]


def quality_report(database_path, run_id, sku=None, limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("Лимит отчёта должен быть от 1 до 10000.")
    with closing(_connect(database_path)) as connection:
        run = connection.execute("SELECT * FROM quality_runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Результат проверки не найден.")
        result = dict(run)
        result["run_id"] = result.pop("id")
        result["summary"] = json.loads(result.pop("summary_json"))
        result["supplier"] = _snapshot(connection, run["snapshot_id"])["supplier"]
        result["dataset"] = dataset_context(database_path)
        result["limit"] = limit
        for name, table in (("skus", "quality_skus"), ("comparisons", "quality_comparisons"), ("issues", "quality_issues")):
            where = "run_id=?"
            args = [run_id]
            if sku is not None:
                where += " AND sku=?"
                args.append(sku)
            if name == "comparisons":
                ordering = "CASE WHEN kind='equal' THEN 1 ELSE 0 END, ordinal"
            elif name == "issues":
                ordering = "CASE WHEN code LIKE '%13%' THEN 0 ELSE 1 END, severity, ordinal"
            else:
                ordering = "sku"
            rows = connection.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY {ordering} LIMIT ?", (*args, limit))
            result[name] = []
            for row in rows:
                if name == "issues":
                    value = dict(row)
                    value["evidence"] = json.loads(value.pop("evidence_json"))
                else:
                    value = json.loads(row["payload_json"])
                result[name].append(value)
            result[f"{name}_total"] = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
        if sku is not None:
            result["selected_sales"] = [json.loads(row[0]) for row in connection.execute(
                "SELECT payload_json FROM quality_selected_sales WHERE run_id=? AND sku=? ORDER BY period", (run_id, sku))]
        return result


def calculation_input(database_path, run_id, sku, allow_scenario=False):
    """Future calculators must enter here, never through unchecked raw imports.

    This is an input gate, not the implementation of a forecast or an order.
    """
    report = quality_report(database_path, run_id, sku=sku, limit=1)
    if not report["skus"]:
        raise ValueError("SKU отсутствует в результате проверки.")
    item = report["skus"][0]
    if item["status"] == MISSING:
        raise ValueError("Не хватает данных: " + "; ".join(item["reasons"]))
    if item["status"] == SCENARIO and allow_scenario is not True:
        raise ValueError("Сценарный расчёт не допускается в подтверждённый реальный заказ.")
    return {**item, "run_id": run_id, "snapshot_id": report["snapshot_id"],
            "dataset": report["dataset"],
            "configuration_id": report["configuration_id"], "rules_version": report["rules_version"],
            "code_version": report["code_version"], "history": report.get("selected_sales", [])}
