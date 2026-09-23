"""Extract archived catalog observations embedded in XLSX external-link XML.

Only ZIP members are read. Relationship targets are retained as metadata and
never opened, resolved on disk, downloaded or used to update Excel links.
Archived prices and shipment conditions are not applied to current products.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl.utils import column_index_from_string


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_NUMERIC_FIELDS = ("pack_quantity", "order_multiple", "minimum_order", "base_price")
_TEXT_FIELDS = ("article", "name", "category", "group_name", "subgroup",
                "subsubgroup", "purchase_unit", "ntin", "status", "packaging")


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value))).strip().casefold().replace("ё", "е")


def _issue(result: Any, code: str, message: str, *, sheet: str,
           row: int | None = None, cell: str | None = None,
           severity: str = "warning") -> None:
    result.issues.append(dict(severity=severity, code=code, sheet=sheet,
                              row=row, cell=cell, message=message))


def _price_date(target: str | None) -> str | None:
    """Use one complete, valid date in the referenced filename; never infer year."""
    if not target:
        return None
    # Parsing/unquoting this string does not access its URL or filesystem path.
    try:
        basename = unquote(urlsplit(target).path).replace("\\", "/").rsplit("/", 1)[-1]
    except ValueError:
        return None
    matches = re.findall(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})(?!\d)", basename)
    if len(matches) != 1:
        return None
    day, month, year = matches[0]
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _targets(archive: ZipFile, xml_path: str) -> dict[str, str]:
    path = PurePosixPath(xml_path)
    relationship_path = str(path.parent / "_rels" / (path.name + ".rels"))
    if relationship_path not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(relationship_path))
    return {element.attrib["Id"]: element.attrib["Target"]
            for element in root
            if element.tag.rsplit("}", 1)[-1] == "Relationship"
            and element.get("Type", "").endswith("/externalLinkPath")
            and "Id" in element.attrib and "Target" in element.attrib}


def _value(element: ET.Element) -> tuple[Any, str, bool]:
    """Return the XML-typed cached scalar and whether its encoding is invalid."""
    data_type = element.get("t", "n")
    value = element.find(f"{{{_MAIN}}}v")
    text = value.text if value is not None else None
    if text is None:
        return None, data_type, False
    if data_type in ("str", "e", "d"):
        return text, data_type, False
    if data_type == "b":
        return (text == "1", data_type, False) if text in ("0", "1") else (text, data_type, True)
    if data_type == "n":
        try:
            numeric = Decimal(text)
            if not numeric.is_finite():
                raise InvalidOperation
            parsed = int(numeric) if numeric == numeric.to_integral_value() else float(numeric)
            if not math.isfinite(parsed):
                raise InvalidOperation
            return parsed, data_type, False
        except (InvalidOperation, ValueError, OverflowError):
            return text, data_type, True
    # A shared-string index cannot be resolved against the containing workbook:
    # the referenced external workbook has its own (unavailable) string table.
    return text, data_type, True


def _cell_state(cell: dict[str, Any] | None) -> str:
    if cell is None:
        return "blank"
    if cell.get("cached_error"):
        return "error"
    if cell["value"] is None or cell["value"] == "":
        return "blank"
    return "error" if cell["data_type"] == "e" else "value"


def _text(cell: dict[str, Any] | None) -> str | None:
    if _cell_state(cell) != "value":
        return None
    return str(cell["value"]).strip()


def _header(cells: dict[str, Any]) -> dict[str, str] | None:
    """Find catalog columns by their labels, including the original abbreviations."""
    result: dict[str, str] = {}
    exact = {"артикул": "article", "наименование": "name", "категория": "category",
             "группа": "group_name", "ед.": "purchase_unit", "ед": "purchase_unit",
             "ntin": "ntin", "статус": "status", "ту": "packaging",
             "ктр.": "pack_quantity", "ктр": "pack_quantity",
             "кратн.": "order_multiple", "кратн": "order_multiple",
             "кратность": "order_multiple", "мин. разр. к отгр.": "minimum_order",
             "мин. разр. к отгр": "minimum_order"}
    for col, cell in cells.items():
        text = _normalize(_text(cell))
        if text in exact:
            result[exact[text]] = col
        elif text.startswith("подподгруппа"):
            result["subsubgroup"] = col
        elif text.startswith("подгруппа"):
            result["subgroup"] = col
        elif text.startswith("базовая цена"):
            result["base_price"] = col
    required = {"article", "name", "purchase_unit", "order_multiple", "minimum_order", "base_price"}
    return result if required <= result.keys() else None


def _number(result: Any, cells: dict[str, Any], column: str | None,
            sheet: str, row: int) -> tuple[int | float | None, str]:
    cell = cells.get(column) if column else None
    state = _cell_state(cell)
    if state != "value":
        return None, state
    value = cell["value"]
    if isinstance(value, str) and _normalize(value) == "(пусто)":
        _issue(result, "EXTERNAL_CACHE_BLANK_MARKER", "Числовое поле содержит явный маркер «(пусто)»; сохранено отсутствие значения, исходный текст доступен в ячейке.",
               sheet=sheet, row=row, cell=f"{column}{row}")
        return None, "blank"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _issue(result, "EXTERNAL_CACHE_INVALID_NUMBER", "В числовом поле архивного прайса сохранён текст; значение не заменено нулём.",
               sheet=sheet, row=row, cell=f"{column}{row}", severity="error")
        return None, "error"
    return value, "value"


def _catalog_row(result: Any, cells: dict[str, Any], header: dict[str, str],
                 *, sheet: str, row: int, price_date: str | None,
                 currency: str | None, articles: set[str]) -> None:
    article_col = header["article"]
    article = _text(cells.get(article_col))
    if not article or _normalize(article) in ("артикул", "итого"):
        return
    location = f"{article_col}{row}"
    if article in articles:
        _issue(result, "DUPLICATE_CATALOG_ARTICLE", "Повторный артикул архивного прайса сохранён отдельной строкой источника.",
               sheet=sheet, row=row, cell=location)
    articles.add(article)
    if not isinstance(cells[article_col]["value"], str):
        _issue(result, "CATALOG_ARTICLE_STORED_AS_NUMBER", "Артикул в XML был числом; строковое представление сохранено, ведущие нули требуют проверки.",
               sheet=sheet, row=row, cell=location)
    item = {field: _text(cells.get(header.get(field))) for field in _TEXT_FIELDS}
    states = {}
    for field in _NUMERIC_FIELDS:
        item[field], states[field] = _number(result, cells, header.get(field), sheet, row)
    item.update(sheet=sheet, row=row, currency=currency, price_date=price_date,
                states_json=json.dumps(states, ensure_ascii=False, sort_keys=True), cell=location)
    result.catalog_items.append(item)


def _append_link(archive: ZipFile, xml_path: str, result: Any) -> None:
    link_name = PurePosixPath(xml_path).name
    targets = _targets(archive, xml_path)
    external_target = next(iter(targets.values())) if len(targets) == 1 else None
    price_date = _price_date(external_target)
    sheet_names: list[str] = []
    sheet: dict[str, Any] | None = None
    used_names = {s["sheet"] for s in result.sheets}
    with archive.open(xml_path) as stream:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            tag = element.tag.rsplit("}", 1)[-1]
            if event == "start" and tag == "externalBook":
                relationship = element.get(_REL_ID)
                if relationship is not None:
                    external_target = targets.get(relationship)
                    price_date = _price_date(external_target)
            elif event == "end" and tag == "sheetName":
                sheet_names.append(element.get("val", ""))
                element.clear()
            elif event == "start" and tag == "sheetData":
                sheet_id = element.get("sheetId", "unknown")
                try:
                    source_name = sheet_names[int(sheet_id)] if int(sheet_id) >= 0 else ""
                except (ValueError, IndexError):
                    source_name = ""
                name = f"{link_name}/{source_name or 'sheetId=' + sheet_id}"
                if name in used_names:
                    name += f" [sheetId={sheet_id}]"
                used_names.add(name)
                sheet = dict(name=name, max_row=0, max_column=0, rows=0, header=None,
                             currency=None, articles=set())
                if not source_name:
                    _issue(result, "EXTERNAL_CACHE_SHEET_NAME_MISSING", "Название листа внешнего кэша отсутствует; в происхождении сохранён его sheetId.", sheet=name)
            elif event == "end" and tag == "row" and sheet is not None:
                try:
                    row = int(element.attrib["r"])
                    if row <= 0:
                        raise ValueError
                except (KeyError, ValueError) as error:
                    raise ValueError(f"Некорректный номер строки внешнего кэша {xml_path}") from error
                cells: dict[str, Any] = {}
                for source_cell in element:
                    if source_cell.tag.rsplit("}", 1)[-1] != "cell":
                        continue
                    coordinate = source_cell.get("r", "")
                    match = re.fullmatch(r"([A-Z]+)([1-9]\d*)", coordinate)
                    if not match or int(match[2]) != row:
                        _issue(result, "EXTERNAL_CACHE_INVALID_COORDINATE", "Координата ячейки внешнего кэша некорректна или не совпадает с номером строки.",
                               sheet=sheet["name"], row=row, cell=coordinate or None, severity="error")
                        continue
                    column = match[1]
                    value, data_type, invalid = _value(source_cell)
                    item = dict(value=value, formula=None, data_type=data_type, number_format="General")
                    if invalid:
                        item["cached_error"] = True
                        _issue(result, "EXTERNAL_CACHE_INVALID_VALUE", "XML-тип и значение внешней ячейки не согласованы или тип не поддерживается; исходное значение сохранено.",
                               sheet=sheet["name"], row=row, cell=coordinate, severity="error")
                    elif data_type == "e" and value not in (None, ""):
                        _issue(result, "EXTERNAL_CACHE_CELL_ERROR", "В архивном прайсе сохранена ошибка Excel.",
                               sheet=sheet["name"], row=row, cell=coordinate, severity="error")
                    cells[column] = item
                    sheet["max_column"] = max(sheet["max_column"], column_index_from_string(column))
                sheet["max_row"] = max(sheet["max_row"], row)
                if cells:
                    sheet["rows"] += 1
                    result.raw_rows.append(dict(sheet=sheet["name"], row=row, cells=cells))
                    candidate_header = _header(cells) if sheet["header"] is None else None
                    if candidate_header:
                        sheet["header"] = candidate_header
                        price_label = _normalize(_text(cells[candidate_header["base_price"]]))
                        sheet["currency"] = "KZT" if re.search(r"\bтенге\b", price_label) else None
                        if sheet["currency"] is None:
                            _issue(result, "EXTERNAL_CACHE_CURRENCY_UNKNOWN", "В заголовке цены нет явного указания тенге; валюта архивной цены не назначена.",
                                   sheet=sheet["name"], row=row, cell=f"{candidate_header['base_price']}{row}")
                    elif sheet["header"] is not None:
                        _catalog_row(result, cells, sheet["header"], sheet=sheet["name"], row=row,
                                     price_date=price_date, currency=sheet["currency"], articles=sheet["articles"])
                element.clear()
            elif event == "end" and tag == "sheetData" and sheet is not None:
                name = sheet["name"]
                result.sheets.append(dict(sheet=name, state="external_cache", declared_dimension=None,
                                          max_row=sheet["max_row"], max_column=sheet["max_column"]))
                result.external_sources.append(dict(sheet=name, xml_path=xml_path,
                                                    external_target=external_target, price_date=price_date,
                                                    status="archived_external_cache"))
                if sheet["rows"]:
                    _issue(result, "ARCHIVED_EXTERNAL_CACHE", "Встроенный кэш внешнего прайса сохранён как архивный источник; его цены и условия не подтверждены для текущих закупок.", sheet=name)
                    if sheet["header"] is None:
                        _issue(result, "EXTERNAL_CACHE_LAYOUT_UNKNOWN", "Заголовки каталога внешнего кэша не распознаны; исходные ячейки сохранены без нормализованных товаров.", sheet=name, severity="error")
                    if external_target is None:
                        _issue(result, "EXTERNAL_TARGET_MISSING", "Адрес исходного внешнего прайса отсутствует или неоднозначен.", sheet=name)
                    if price_date is None:
                        _issue(result, "EXTERNAL_PRICE_DATE_UNKNOWN", "Полная однозначная дата в имени внешнего прайса отсутствует; дата не назначена.", sheet=name)
                sheet = None
                element.clear()


def append_external_cache(path: Path, result: Any) -> None:
    """Append every embedded external cache without following its relationships.

    ``result`` is the IEK parser result with catalog_items, external_sources,
    raw_rows, sheets and issues lists. Catalog rows retain duplicate articles.
    Missing/error numeric values remain None with an explicit states_json state.
    A workbook without external cache members is a no-op.
    """
    with ZipFile(path) as archive:
        members = sorted(name for name in archive.namelist()
                         if re.fullmatch(r"xl/externalLinks/[^/]+\.xml", name))
        for xml_path in members:
            try:
                _append_link(archive, xml_path, result)
            except ET.ParseError as error:
                raise ValueError(f"Некорректный XML внешнего кэша: {xml_path}") from error
