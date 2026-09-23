"""Read IEK workbooks into the shared import contract, without calculating orders.

Sources and their Excel caches remain immutable. Opening balances, minimums,
order deadlines and archived supplier terms retain their distinct meanings.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from hackalem.importers.systeme import (
    ParsedWorkbook,
    _MONTH_NUMBER,
    _cell,
    _issue,
    _measure,
    _month,
    _normalize,
    _number,
    _parse_date,
    _read_raw,
    _seasonal,
    _snapshot,
    _state,
    _text,
    _transactions,
)


RULES_VERSION = "iek-1"
_XML_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML_DOCUMENT_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass
class ParsedIEKWorkbook(ParsedWorkbook):
    incoming_orders: list[dict[str, Any]] = field(default_factory=list)
    catalog_items: list[dict[str, Any]] = field(default_factory=list)
    external_sources: list[dict[str, Any]] = field(default_factory=list)
    row_metadata: list[dict[str, Any]] = field(default_factory=list)


def _labels(record: dict[str, Any]) -> dict[str, str]:
    labels = {}
    for column, cell in record["cells"].items():
        label = _normalize(cell["value"])
        # Both Latin c and Cyrillic с occur in exported headings.
        if label in {"код 1c", "код1с", "код1c"}:
            label = "код 1с"
        if label:
            labels[label] = column
    return labels


def _detect(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in rows:
        if record["row"] > 12:
            break
        labels = _labels(record)
        months = {
            column: period
            for column, cell in record["cells"].items()
            if (period := _month(cell["value"])) is not None
        }
        names = set(labels)
        kind = None
        if {"дата", "номер", "документ", "код", "номенклатура", "ед.", "склад", "количество"} <= names:
            kind = "transactions"
        elif {"код 1с", "артикул поставщика", "наименование", "мин. разр. к отгр."} <= names:
            kind = "minimums"
        elif {"код 1с", "артикул иэк", "наименование"} <= names:
            kind = "incoming"
        elif {"номенклатура", "номенклатура.код"} <= names and months:
            if "ед." in names or "ед.изм" in names:
                # The balance basis is supplied by the report, not its filename.
                opening = any(
                    _normalize(_cell(following, column)["value"]) == "нач. остаток"
                    for following in rows
                    if record["row"] < following["row"] <= record["row"] + 3
                    for column in months
                )
                if not opening:
                    continue
                kind = "monthly_stock"
            else:
                kind = "monthly_sales"
        elif "год" in names:
            season_months = {
                column: _MONTH_NUMBER[_normalize(cell["value"]).rstrip(".")]
                for column, cell in record["cells"].items()
                if _normalize(cell["value"]).rstrip(".") in _MONTH_NUMBER
            }
            if set(season_months.values()) == set(range(1, 13)):
                return dict(kind="seasonality", row=record["row"], labels=labels, months=season_months)
        if kind:
            return dict(kind=kind, row=record["row"], labels=labels, months=months)
    return None


def _read_row_metadata(path: Path, result: ParsedIEKWorkbook) -> None:
    """Record every physical worksheet row, including hidden empty rows."""
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships.findall(f"{{{_XML_PACKAGE_RELS}}}Relationship")
            if item.attrib.get("TargetMode") != "External"
        }
        for sheet in workbook.findall(f"{{{_XML_MAIN}}}sheets/{{{_XML_MAIN}}}sheet"):
            target = targets[sheet.attrib[f"{{{_XML_DOCUMENT_RELS}}}id"]]
            member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            with archive.open(member) as stream:
                for _, element in ET.iterparse(stream, events=("end",)):
                    if element.tag == f"{{{_XML_MAIN}}}row":
                        result.row_metadata.append(
                            dict(sheet=sheet.attrib["name"], row=int(element.attrib["r"]),
                                 hidden=int(element.attrib.get("hidden", "0").lower() in {"1", "true"}))
                        )
                    element.clear()


def _product_rows(result: ParsedIEKWorkbook, rows: list[dict[str, Any]], schema: dict[str, Any]):
    labels, kind = schema["labels"], schema["kind"]
    direct = kind in {"minimums", "incoming"}
    code_column = labels["код 1с" if direct else "номенклатура.код"]
    name_column = labels["наименование" if direct else "номенклатура"]
    article_column = labels.get("артикул поставщика" if kind == "minimums" else "артикул иэк")
    unit_column = labels.get("ед.", labels.get("ед.изм"))
    seen = set()
    for record in rows:
        if record["row"] <= schema["row"]:
            continue
        if any(_normalize(_cell(record, column)["value"]) == "итого" for column in ("A", name_column, code_column)):
            continue
        source = _cell(record, code_column)
        sku = _text(record, code_column)
        name = _text(record, name_column)
        location = f"{code_column}{record['row']}"
        if kind == "incoming" and (not sku or sku in {"0", "1", "0.0", "1.0"}):
            _issue(result, "INCOMING_SERVICE_ROW", "Служебная строка пути с пустым кодом или кодом 0/1 сохранена только в исходных данных.",
                   sheet=record["sheet"], row=record["row"], cell=location)
            continue
        if not sku or _state(source) == "error":
            if name or direct:
                _issue(result, "SKU_MISSING", "Нет корректного кода товара; исходная строка сохранена.",
                       sheet=record["sheet"], row=record["row"], cell=location, severity="error")
            continue
        if not isinstance(source["value"], str):
            _issue(result, "SKU_STORED_AS_NUMBER", "Код Excel был числом и сохранён строкой; ведущие нули требуют проверки.",
                   sheet=record["sheet"], row=record["row"], cell=location)
        if sku in seen:
            _issue(result, "DUPLICATE_SKU", "Повтор кода сохранён в фактах; справочник листа использует первое вхождение.",
                   sheet=record["sheet"], row=record["row"], cell=location)
        else:
            seen.add(sku)
            result.products.append(
                dict(sheet=record["sheet"], row=record["row"], sku=sku, name=name,
                     article=_text(record, article_column), unit=_text(record, unit_column), cell=location)
            )
        yield record, sku


def _quantities(result: ParsedIEKWorkbook, rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    labels, kind = schema["labels"], schema["kind"]
    sheet = rows[0]["sheet"]
    if kind == "monthly_stock":
        _issue(result, "OPENING_STOCK_NOT_CURRENT", "Источник содержит начальные месячные остатки; это не текущий доступный запас и не история дней отсутствия.",
               sheet=sheet, row=schema["row"])
    if kind == "minimums":
        _issue(result, "MINIMUM_NOT_MULTIPLE", "Минимальная отгрузка сохранена отдельно от кратности и единицы закупки; действующие условия не подтверждены.",
               sheet=sheet, row=schema["row"], cell=f"{labels['мин. разр. к отгр.']}{schema['row']}")
    for record, sku in _product_rows(result, rows, schema):
        for column, period in schema["months"].items():
            quantity, state = _number(record, column, result, negative_warning=True)
            result.monthly_values.append(
                dict(sheet=sheet, row=record["row"], sku=sku, period=period,
                     series="opening_stock" if kind == "monthly_stock" else "sales",
                     quantity=quantity, state=state, cell=f"{column}{record['row']}")
            )
        if kind == "minimums":
            column = labels["мин. разр. к отгр."]
            _measure(result, record, sku, column, "minimum_order")
            measure = result.measures[-1]
            if measure["state"] != "value":
                # A cached #N/A already has a precise source-cell error. Avoid
                # reporting the same unavailable minimum as two errors.
                already_reported = any(
                    issue["sheet"] == sheet and issue.get("cell") == measure["cell"]
                    and issue["severity"] == "error" for issue in result.issues
                )
                if not already_reported:
                    _issue(result, "MINIMUM_ORDER_UNAVAILABLE", "Минимальная отгрузка отсутствует или содержит ошибку; замена на 1 не выполнена.",
                           sheet=sheet, row=record["row"], cell=measure["cell"], severity="error")
            elif measure["number"] <= 0:
                _issue(result, "INVALID_MINIMUM_ORDER", "Минимальная отгрузка должна быть положительной; исходное значение сохранено.",
                       sheet=sheet, row=record["row"], cell=measure["cell"], severity="error")
        elif "итого" in labels:
            metric = "reported_opening_stock_total" if kind == "monthly_stock" else "reported_total_sales"
            _measure(result, record, sku, labels["итого"], metric)


def _order_header(value: Any) -> dict[str, str | None]:
    text = _normalize(value)
    number_match = re.search(r"\b([a-zа-я]+-\d+)\s+от\b", text)
    order_number = number_match[1].upper() if number_match else None
    order_date = None
    date_match = re.search(r"\bот\s+(\d{1,2})\s+([а-я]+)\.?\s+(\d{4})\b", text)
    if date_match and date_match[2] in _MONTH_NUMBER:
        try:
            order_date = date(int(date_match[3]), _MONTH_NUMBER[date_match[2]], int(date_match[1])).isoformat()
        except ValueError:
            pass
    else:
        date_match = re.search(r"\bот\s+(\d{1,2}\.\d{1,2}\.\d{4})\b", text)
        parsed = _parse_date(date_match[1]) if date_match else None
        order_date = parsed[:10] if parsed else None
    deadline_match = re.search(r"поступление\s+до\s+(\d{1,2}\.\d{1,2}\.\d{4})\b", text)
    parsed = _parse_date(deadline_match[1]) if deadline_match else None
    return dict(order_number=order_number, order_date=order_date,
                eta_deadline=parsed[:10] if parsed else None)


def _incoming(result: ParsedIEKWorkbook, rows: list[dict[str, Any]], schema: dict[str, Any]) -> None:
    labels = schema["labels"]
    header = next(record for record in rows if record["row"] == schema["row"])
    identity_columns = {labels[name] for name in ("код 1с", "артикул иэк", "наименование")}
    order_headers = {}
    for column, source in header["cells"].items():
        if column in identity_columns or _normalize(source["value"]) in {"", "№"}:
            continue
        details = _order_header(source["value"])
        order_headers[column] = details
        if any(value is None for value in details.values()):
            _issue(result, "INCOMING_HEADER_INCOMPLETE", "В заголовке заказа не распознаны номер, дата заказа или полный срок «поступление до»; отсутствующие поля не угаданы.",
                   sheet=header["sheet"], row=header["row"], cell=f"{column}{header['row']}", severity="error")
    _issue(result, "ETA_DEADLINE_NOT_RECEIPT", "«Поступление до» является обещанным крайним сроком, а не фактом приёмки или сроком новой поставки.",
           sheet=header["sheet"], row=header["row"])
    _issue(result, "INCOMING_UNIT_UNKNOWN", "Единица количеств пути не указана отдельным полем; перевод в единицу учёта требует подтверждения.",
           sheet=header["sheet"], row=header["row"])
    if not result.snapshot_date:
        _issue(result, "SNAPSHOT_DATE_MISSING", "В имени нет однозначной полной даты среза пути; дата не назначена.",
               sheet=header["sheet"], row=header["row"])
    for record, sku in _product_rows(result, rows, schema):
        name = _text(record, labels["наименование"]) or ""
        if "закупаются" in _normalize(name):
            _issue(result, "PURCHASE_UNIT_NOTE", "Название содержит указание на другую единицу закупки; автоматический перевод не выполнен.",
                   sheet=record["sheet"], row=record["row"], cell=f"{labels['наименование']}{record['row']}")
        for column, details in order_headers.items():
            if _state(_cell(record, column)) == "blank":
                continue
            quantity, state = _number(record, column, result, negative_warning=True)
            result.incoming_orders.append(
                dict(sheet=record["sheet"], row=record["row"], sku=sku,
                     article=_text(record, labels["артикул иэк"]), **details,
                     quantity=quantity, state=state, unit=None,
                     cell=f"{column}{record['row']}", header_cell=f"{column}{header['row']}")
            )


def _attach_issue_skus(result: ParsedIEKWorkbook, schemas: dict[str, dict | None], groups: dict[str, list[dict]]) -> None:
    """Make cell-level problems addressable by SKU without altering source cells."""
    relevant = {(issue["sheet"], issue["row"]) for issue in result.issues if issue.get("row") is not None and not issue.get("sku")}
    lookup = {}
    for sheet, schema in schemas.items():
        if schema is None or schema["kind"] == "seasonality":
            continue
        key = "код" if schema["kind"] == "transactions" else "код 1с" if schema["kind"] in {"minimums", "incoming"} else "номенклатура.код"
        column = schema["labels"][key]
        for record in groups[sheet]:
            if record["row"] <= schema["row"] or (sheet, record["row"]) not in relevant:
                continue
            source = _cell(record, column)
            sku = _text(record, column)
            if sku and _state(source) == "value" and not (schema["kind"] == "incoming" and sku in {"0", "1", "0.0", "1.0"}):
                lookup[sheet, record["row"]] = sku
    for issue in result.issues:
        sku = lookup.get((issue["sheet"], issue.get("row")))
        if sku and not issue.get("sku"):
            issue["sku"] = sku


def parse_workbook(path: Path) -> ParsedIEKWorkbook:
    """Detect an IEK source by its headers and preserve all original observations."""
    path = Path(path)
    result = ParsedIEKWorkbook(source_kind="")
    groups = _read_raw(path, result)
    _read_row_metadata(path, result)
    schemas = {name: _detect(rows) for name, rows in groups.items()}
    primary = {schema["kind"] for schema in schemas.values() if schema and schema["kind"] != "seasonality"}
    if len(primary) > 1:
        raise ValueError("В книге неоднозначный набор основных таблиц IEK: " + ", ".join(sorted(primary)))
    if primary:
        result.source_kind = next(iter(primary))
    elif any(schema is not None for schema in schemas.values()):
        result.source_kind = "seasonality"
    else:
        raise ValueError("Структура книги IEK не распознана по заголовкам.")
    if result.source_kind == "incoming":
        result.snapshot_date = _snapshot(path)
    for name, rows in groups.items():
        schema = schemas[name]
        if schema is None:
            if rows:
                _issue(result, "UNRECOGNIZED_SHEET", "Лист сохранён в исходных строках, но его структура не преобразована в факты.", sheet=name)
        elif schema["kind"] == "seasonality":
            _seasonal(result, rows, schema)
        elif schema["kind"] == "transactions":
            _transactions(result, rows, schema)
        elif schema["kind"] == "incoming":
            _incoming(result, rows, schema)
        else:
            _quantities(result, rows, schema)
    if result.source_kind == "minimums":
        from hackalem.importers.iek_cache import append_external_cache

        append_external_cache(path, result)
    _attach_issue_skus(result, schemas, groups)
    return result
