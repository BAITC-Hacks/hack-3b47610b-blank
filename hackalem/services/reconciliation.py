"""Read-only diagnostics for one explicit supplier snapshot.

Alternative sales reports stay separate. This module neither selects an
authoritative source nor changes parameters, readiness or imported observations.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime
from itertools import combinations
from pathlib import Path
from typing import Any


SALES_SOURCES = ("transactions", "monthly_sales", "current")


def _numeric(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _period(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
    return f"{parsed.year:04d}-{parsed.month:02d}-01"


def _unit(value: str) -> str:
    return value.strip().casefold().rstrip(".")


def _issue(issues: list[dict], sku: str | None, code: str, message: str,
           evidence: dict, severity: str = "warning") -> None:
    issues.append(dict(sku=sku, code=code, severity=severity, message=message, evidence=evidence))


def _evidence(row: Any, table: str) -> dict:
    return {"file_id": row["file_id"], "source_kind": row["source_kind"],
            "table": table, "sheet": row["sheet"], "row": row["row"],
            "cell": row["cell"]}


def _rows(connection: sqlite3.Connection, snapshot_id: int, table: str):
    # Table names come exclusively from the constants in this module.
    return connection.execute(
        f"SELECT x.*, s.source_kind FROM {table} x "
        "JOIN snapshot_files s ON s.file_id=x.file_id WHERE s.snapshot_id=? "
        "ORDER BY s.source_kind, x.sheet, x.row", (snapshot_id,),
    )


def _register(registry: dict, source_skus: dict, row: Any, table: str,
              issues: list[dict]) -> str | None:
    raw_sku = row["sku"]
    if raw_sku is None or not str(raw_sku).strip():
        _issue(issues, None, "SKU_MISSING", "Факт не содержит кода товара; его источник сохранён.",
               _evidence(row, table), "error")
        return None
    sku = str(raw_sku)
    record = registry.setdefault(sku, {"names": set(), "articles": set(), "units": set(), "sources": set()})
    record["sources"].add(row["source_kind"])
    source_skus.setdefault(row["source_kind"], set()).add(sku)
    keys = row.keys()
    for column, field in (("name", "names"), ("article", "articles"), ("unit", "units")):
        if column in keys and row[column] is not None and str(row[column]).strip():
            record[field].add(str(row[column]).strip())
    return sku


def _new_aggregate(row: Any, table: str, sku: str, period: str) -> dict:
    return dict(source_kind=row["source_kind"], sku=sku, period=period,
                file_id=row["file_id"], table=table, value_count=0,
                blank_count=0, error_count=0, negative_count=0, total=0,
                record_count=0, units=set(), warehouses=set(), document_types=set(),
                locations=defaultdict(lambda: {"rows": set(), "cells": set()}))


def _add_aggregate(aggregate: dict, row: Any, units: set[str]) -> None:
    aggregate["record_count"] += 1
    state, number = row["state"], row["quantity"]
    if state == "value" and _numeric(number):
        aggregate["value_count"] += 1
        aggregate["total"] += number
        aggregate["negative_count"] += int(number < 0)
    elif state == "blank":
        aggregate["blank_count"] += 1
    else:
        aggregate["error_count"] += 1
    aggregate["units"].update(units)
    for field, target in (("warehouse", "warehouses"), ("document_type", "document_types")):
        if field in row.keys() and row[field]:
            aggregate[target].add(row[field])
    location = aggregate["locations"][row["sheet"]]
    location["rows"].add(row["row"])
    if aggregate["table"] != "transactions":
        location["cells"].add(row["cell"])


def _finish_aggregate(aggregate: dict, issues: list[dict]) -> dict:
    values, blanks, errors = (aggregate[name] for name in ("value_count", "blank_count", "error_count"))
    quantity = aggregate["total"] if values else None
    state = "partial" if values and (blanks or errors) else "value" if values else "error" if errors else "missing"
    provenance = {"file_id": aggregate["file_id"], "source_kind": aggregate["source_kind"],
                  "table": aggregate["table"], "sku": aggregate["sku"], "period": aggregate["period"],
                  "locations": [{"sheet": sheet, "rows": sorted(location["rows"]),
                                 **({"cells": sorted(location["cells"])} if location["cells"] else {})}
                                for sheet, location in sorted(aggregate["locations"].items())]}
    if aggregate["table"] == "monthly_values" and aggregate["record_count"] > 1:
        state, quantity = "ambiguous", None
        _issue(issues, aggregate["sku"], "DUPLICATE_MONTHLY_VALUE",
               "Для одного SKU и месяца в месячном источнике есть несколько строк; они не сложены автоматически.",
               {**provenance, "record_count": aggregate["record_count"]}, "error")
    if len({_unit(x) for x in aggregate["units"]}) > 1:
        state, quantity = "ambiguous", None
        _issue(issues, aggregate["sku"], "SALES_UNIT_CONFLICT",
               "В одном месячном агрегате разные единицы; суммарное количество не сформировано.",
               {**provenance, "units": sorted(aggregate["units"])}, "error")
    return dict(source_kind=aggregate["source_kind"], sku=aggregate["sku"], period=aggregate["period"],
                quantity=quantity, state=state,
                **{field: aggregate[field] for field in ("value_count", "blank_count", "error_count", "negative_count")},
                units=sorted(aggregate["units"]), warehouses=sorted(aggregate["warehouses"]),
                document_types=sorted(aggregate["document_types"]), provenance=provenance)


def _comparisons(sales: list[dict], sales_sources: list[str], source_skus: dict,
                 issues: list[dict]) -> list[dict]:
    indexed = {source: {} for source in sales_sources}
    for item in sales:
        indexed[item["source_kind"]][item["sku"], item["period"]] = item
    result = []
    diagnostics = defaultdict(list)
    for left_source, right_source in combinations(sales_sources, 2):
        left, right = indexed[left_source], indexed[right_source]
        for sku, period in sorted(left.keys() | right.keys()):
            a, b = left.get((sku, period)), right.get((sku, period))
            if sku not in source_skus[right_source]:
                kind = "assortment_left_only"
            elif sku not in source_skus[left_source]:
                kind = "assortment_right_only"
            elif a is None:
                kind = "period_right_only"
            elif b is None:
                kind = "period_left_only"
            elif a["state"] != "value" or b["state"] != "value":
                kind = "missing_value"
            elif a["units"] and b["units"] and {_unit(x) for x in a["units"]} != {_unit(x) for x in b["units"]}:
                kind = "unit_conflict"
            elif math.isclose(a["quantity"], b["quantity"], rel_tol=0, abs_tol=1e-9):
                kind = "equal"
            else:
                kind = "quantity_mismatch"
            comparable = kind in ("equal", "quantity_mismatch")
            comparison = dict(sku=sku, period=period, left_source=left_source, right_source=right_source,
                              left_value=a["quantity"] if a else None,
                              right_value=b["quantity"] if b else None,
                              left_state=a["state"] if a else "missing", right_state=b["state"] if b else "missing",
                              difference=a["quantity"] - b["quantity"] if comparable else None, kind=kind)
            result.append(comparison)
            if kind != "equal":
                diagnostics[sku, left_source, right_source, kind].append(comparison)
    messages = {
        "assortment_left_only": "SKU отсутствует в правом источнике целиком; это отличие ассортимента, а не нулевые продажи.",
        "assortment_right_only": "SKU отсутствует в левом источнике целиком; это отличие ассортимента, а не нулевые продажи.",
        "period_left_only": "SKU присутствует в обоих источниках, но для месяца нет записи в правом источнике.",
        "period_right_only": "SKU присутствует в обоих источниках, но для месяца нет записи в левом источнике.",
        "missing_value": "Сравнение количества недоступно: есть пустые, ошибочные, частичные или неоднозначные значения.",
        "unit_conflict": "Единицы двух источников различаются; количественная разность без подтверждённого перевода не рассчитана.",
        "quantity_mismatch": "Количество одного SKU за месяц различается между альтернативными отчётами; источник не выбран автоматически.",
    }
    for (sku, left, right, kind), items in sorted(diagnostics.items()):
        _issue(issues, sku, "SALES_COMPARISON_UNIT_CONFLICT" if kind == "unit_conflict" else "SALES_" + kind.upper(), messages[kind],
               {"left_source": left, "right_source": right, "kind": kind,
                "periods": [x["period"] for x in items], "count": len(items), "examples": items[:3]})
    return result


def _constraint_checks(registry: dict, measures: dict, supplier: str, issues: list[dict]) -> dict:
    source, metric = ("minimums", "minimum_order") if supplier == "IEK" else ("multiples", "order_multiple")
    counts = Counter()
    for sku in sorted(registry):
        observations = measures.get((source, sku, metric), [])
        evidence = {"source_kind": source, "metric": metric,
                    "observations": [{**_evidence(row, "measures"), "number": row["number"], "state": row["state"]}
                                     for row in observations], "archive_terms_active": False}
        if not observations:
            counts["missing"] += 1
            _issue(issues, sku, "MINIMUM_ORDER_MISSING" if metric == "minimum_order" else "ORDER_MULTIPLE_MISSING",
                   "В выделенном источнике нет ограничения для SKU; архивное условие или единица не подставлены.", evidence, "error")
        elif len(observations) != 1:
            counts["ambiguous"] += 1
            _issue(issues, sku, "ORDER_CONSTRAINT_AMBIGUOUS", "Ограничение заказа представлено повторными строками источника; значение не выбрано автоматически.", evidence, "error")
        else:
            row = observations[0]
            valid = row["state"] == "value" and _numeric(row["number"]) and row["number"] > 0
            if metric == "order_multiple" and valid:
                valid = float(row["number"]).is_integer()
            counts["value" if valid else "unavailable"] += 1
            if not valid:
                _issue(issues, sku, "ORDER_CONSTRAINT_UNAVAILABLE", "Ограничение заказа пустое, ошибочное или недопустимое; оно не заменено единицей.", evidence, "error")
    if supplier != "IEK":
        _issue(issues, None, "MINIMUM_ORDER_NOT_SUPPLIED", "Источник Systeme содержит кратность без отдельного подтверждённого минимального заказа.",
               {"source_kind": "multiples", "metric": "order_multiple", "sku_count": len(registry)})
    return {"source_kind": source, "metric": metric, "counts": dict(counts), "business_confirmed": False}


def _stock_checks(measures: dict, monthly_stock: dict, files: dict, issues: list[dict]) -> None:
    current = files.get("current")
    if current is None:
        return
    stock_skus = {sku for source, sku, metric in measures if source == "current"}
    for sku in sorted(stock_skus):
        components = {metric: measures.get(("current", sku, metric), [])
                      for metric in ("stock", "reserved_stock", "free_stock")}
        evidence = {metric: [{**_evidence(row, "measures"), "number": row["number"], "state": row["state"]}
                             for row in rows] for metric, rows in components.items()}
        if not all(len(rows) == 1 and rows[0]["state"] == "value" and _numeric(rows[0]["number"])
                   for rows in components.values()):
            _issue(issues, sku, "CURRENT_STOCK_IDENTITY_UNAVAILABLE", "Для проверки остаток − резерв = свободный остаток нужны три однозначных значения.", evidence)
            continue
        stock, reserved, free = (components[name][0]["number"] for name in ("stock", "reserved_stock", "free_stock"))
        difference = stock - reserved - free
        if not math.isclose(difference, 0, rel_tol=0, abs_tol=1e-9):
            _issue(issues, sku, "CURRENT_STOCK_IDENTITY_MISMATCH", "Остаток минус резерв не равен свободному остатку; источник не исправлен.",
                   {**evidence, "difference": difference}, "error")
        period = _period(current.get("snapshot_date"))
        historical = monthly_stock.get((sku, period), []) if period else []
        if len(historical) == 1 and historical[0]["state"] == "value" and _numeric(historical[0]["quantity"]):
            row = historical[0]
            if not math.isclose(row["quantity"], stock, rel_tol=0, abs_tol=1e-9):
                _issue(issues, sku, "STOCK_SNAPSHOT_DIFFERENCE",
                       "Месячный остаток и оперативный срез различаются. Даты и охват могут различаться; эти значения не взаимозаменены.",
                       {"monthly": {**_evidence(row, "monthly_values"), "period": period,
                                    "series": row["series"], "quantity": row["quantity"]},
                        "current": {**_evidence(components["stock"][0], "measures"),
                                    "snapshot_date": current["snapshot_date"], "quantity": stock}})


def analyze_snapshot(database_path: Path, snapshot_id: int) -> dict:
    """Diagnose selected source versions using a read-only, consistent SQL view."""
    uri = Path(database_path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=60)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        snapshot = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if snapshot is None:
            raise ValueError(f"Снимок №{snapshot_id} не найден.")
        supplier = snapshot["supplier"]
        files = {row["source_kind"]: dict(row) for row in connection.execute(
            "SELECT f.* FROM import_files f JOIN snapshot_files s ON s.file_id=f.id WHERE s.snapshot_id=?",
            (snapshot_id,))}
        if any(row["supplier"] != supplier for row in files.values()):
            raise ValueError("Снимок содержит версии другого поставщика.")
        issues: list[dict] = []
        registry: dict[str, dict] = {}
        source_skus = {kind: set() for kind in files if kind != "seasonality"}
        source_units = defaultdict(set)
        periods = defaultdict(set)
        for row in _rows(connection, snapshot_id, "products"):
            sku = _register(registry, source_skus, row, "products", issues)
            if sku is not None and row["unit"]:
                source_units[row["source_kind"], sku].add(row["unit"])
        for row in connection.execute(
            "SELECT i.*, s.source_kind FROM import_issues i JOIN snapshot_files s ON s.file_id=i.file_id "
            "WHERE s.snapshot_id=? ORDER BY i.id", (snapshot_id,)):
            _issue(issues, row["sku"], row["code"], row["message"],
                   {"origin": "import", "import_issue_id": row["id"], "file_id": row["file_id"],
                    "source_kind": row["source_kind"], "sheet": row["sheet"],
                    "row": row["row"], "cell": row["cell"]}, row["severity"])
        aggregates = {}
        quality_groups = defaultdict(lambda: {"count": 0, "examples": []})
        monthly_stock = defaultdict(list)
        for table in ("transactions", "monthly_values", "incoming_orders"):
            for row in _rows(connection, snapshot_id, table):
                sku = _register(registry, source_skus, row, table, issues)
                if sku is None:
                    continue
                quality = ("missing" if row["state"] == "blank" else
                           "error" if row["state"] != "value" or not _numeric(row["quantity"]) else
                           "negative" if row["quantity"] < 0 else None)
                if quality:
                    group = quality_groups[sku, row["source_kind"], table, quality]
                    group["count"] += 1
                    if len(group["examples"]) < 3:
                        group["examples"].append({**_evidence(row, table), "state": row["state"], "quantity": row["quantity"]})
                if table == "incoming_orders":
                    continue
                raw_period = row["occurred_at"] if table == "transactions" else row["period"]
                period = _period(raw_period)
                if period is None:
                    _issue(issues, sku, "FACT_PERIOD_INVALID", "Факт сохранён, но дата не позволяет отнести его к месяцу.",
                           {**_evidence(row, table), "source_date": raw_period}, "error")
                    continue
                periods[row["source_kind"]].add(period)
                if table == "monthly_values" and row["series"] != "sales":
                    monthly_stock[sku, period].append(dict(row))
                    continue
                if row["source_kind"] not in SALES_SOURCES:
                    continue
                key = row["source_kind"], sku, period
                if key not in aggregates:
                    aggregates[key] = _new_aggregate(row, table, sku, period)
                aggregate = aggregates[key]
                unit_values = ({row["unit"]} if table == "transactions" and row["unit"] else
                               source_units[row["source_kind"], sku])
                _add_aggregate(aggregate, row, unit_values)
        measures = defaultdict(list)
        for row in _rows(connection, snapshot_id, "measures"):
            sku = _register(registry, source_skus, row, "measures", issues)
            if sku is not None:
                measures[row["source_kind"], sku, row["metric"]].append(dict(row))
        quality_messages = {
            "missing": "В источнике есть пустые количества; отсутствие не интерпретируется как нулевой спрос или остаток.",
            "error": "В источнике есть ошибочные количества; они сохранены и не заменены нулём.",
            "negative": "В источнике есть отрицательные количества; они сохранены со знаком, смысл операции требует отдельного решения.",
        }
        for (sku, source, table, quality), group in sorted(quality_groups.items()):
            _issue(issues, sku, "SOURCE_QUANTITY_" + quality.upper(), quality_messages[quality],
                   {"source_kind": source, "table": table, **group}, "error" if quality == "error" else "warning")
        article_codes = defaultdict(set)
        for sku, entry in registry.items():
            for article in entry["articles"]:
                article_codes[article].add(sku)
            if len(entry["articles"]) > 1:
                _issue(issues, sku, "SKU_ARTICLE_AMBIGUOUS", "Один код связан с несколькими артикулами; соответствие не выбрано автоматически.",
                       {"articles": sorted(entry["articles"]), "sources": sorted(entry["sources"])}, "error")
            if len({_unit(value) for value in entry["units"]}) > 1:
                _issue(issues, sku, "ACCOUNTING_UNIT_CONFLICT", "Для SKU указаны разные учётные единицы; количества не преобразованы автоматически.",
                       {"units": sorted(entry["units"]), "sources": sorted(entry["sources"])}, "error")
        for article, codes in sorted(article_codes.items()):
            if len(codes) > 1:
                for sku in sorted(codes):
                    _issue(issues, sku, "ARTICLE_SKU_AMBIGUOUS", "Один артикул связан с несколькими внутренними кодами; товары не объединены.",
                           {"article": article, "skus": sorted(codes)})
        for row in connection.execute(
            "SELECT c.article, COUNT(*) AS n FROM catalog_items c JOIN snapshot_files s ON s.file_id=c.file_id "
            "WHERE s.snapshot_id=? GROUP BY c.article HAVING COUNT(*)>1", (snapshot_id,)):
            for sku in sorted(article_codes.get(row["article"], [])):
                _issue(issues, sku, "CATALOG_ARTICLE_AMBIGUOUS", "Архивный каталог содержит несколько строк артикула; условие не выбрано.",
                       {"article": row["article"], "catalog_rows": row["n"], "archive_terms_active": False})
        sales = [_finish_aggregate(aggregate, issues) for _, aggregate in sorted(aggregates.items())]
        sales_sources = [source for source in SALES_SOURCES if source in files]
        comparisons = _comparisons(sales, sales_sources, source_skus, issues)
        constraints = _constraint_checks(registry, measures, supplier, issues)
        _stock_checks(measures, monthly_stock, files, issues)
        pairs = []
        for left, right in combinations(sorted(source_skus), 2):
            a, b = source_skus[left], source_skus[right]
            pairs.append(dict(left_source=left, right_source=right, common_count=len(a & b),
                              left_only_count=len(a - b), right_only_count=len(b - a),
                              left_only=sorted(a - b), right_only=sorted(b - a)))
        for sku, entry in sorted(registry.items()):
            missing = sorted(set(source_skus) - entry["sources"])
            if missing:
                _issue(issues, sku, "SKU_SOURCE_MISSING", "SKU отсутствует в части источников и сохранён в общем ассортименте.",
                       {"missing_sources": missing, "present_sources": sorted(entry["sources"])})
        coverage = dict(sku_count=len(registry),
                        sources={source: dict(sku_count=len(codes), sku_codes=sorted(codes),
                                              missing_skus=sorted(set(registry) - codes),
                                              first_period=min(periods[source]) if periods[source] else None,
                                              last_period=max(periods[source]) if periods[source] else None)
                                 for source, codes in sorted(source_skus.items())},
                        matrix={left: {right: len(a & b) for right, b in sorted(source_skus.items())}
                                for left, a in sorted(source_skus.items())},
                        pairs=pairs, sales_sources=sales_sources,
                        comparison_counts=dict(Counter(row["kind"] for row in comparisons)),
                        order_constraints=constraints)
        skus = [dict(sku=sku, name=sorted(entry["names"])[0] if entry["names"] else None,
                     articles=sorted(entry["articles"]), units=sorted(entry["units"]),
                     sources=sorted(entry["sources"])) for sku, entry in sorted(registry.items())]
        return dict(snapshot_id=snapshot_id, supplier=supplier, skus=skus, sales=sales,
                    comparisons=comparisons, issues=issues, coverage=coverage)
