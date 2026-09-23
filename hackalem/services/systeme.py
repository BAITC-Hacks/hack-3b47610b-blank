"""Local, atomic imports; every reader requires an explicit versioned input set."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from hackalem.config import PROJECT_ROOT, Settings
from hackalem.import_schema import EXTRA_FACT_COLUMNS, FACT_COLUMNS
from hackalem.importers import systeme as parser
from hackalem.storage import initialize_database

SOURCE_KINDS = frozenset({
    "multiples", "transactions", "monthly_stock", "monthly_sales", "seasonality", "current",
})
DEFAULT_PARAMETERS = {
    "dataset": "real", "blank_policy": "preserve", "eta_year": None,
    "sales_authority": None, "calculation_version": "not-implemented-stage-2",
}
ALL_FACT_COLUMNS = {**FACT_COLUMNS, **EXTRA_FACT_COLUMNS}
SUPPLIERS = ("Systeme Electric", "IEK")


def _profile(supplier):
    if supplier == "Systeme Electric":
        return parser, "Systeme electric", SOURCE_KINDS
    if supplier == "IEK":
        from hackalem.importers import iek
        return iek, "IEK", frozenset({"minimums", "transactions", "monthly_stock", "monthly_sales", "seasonality", "incoming"})
    raise ValueError(f"Неизвестный поставщик: {supplier}")


def _parser_version(module, supplier):
    if supplier == "Systeme Electric":
        return _hash(Path(module.__file__).read_bytes())
    from hackalem.importers import iek_cache
    return _hash(b"\0".join(Path(item.__file__).read_bytes() for item in (module, iek_cache, parser)))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _now():
    return datetime.now(UTC).isoformat()


def _connect(path):
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _code_manifest():
    paths = sorted((PROJECT_ROOT / "hackalem").rglob("*.py"))
    paths += [PROJECT_ROOT / "pyproject.toml", PROJECT_ROOT / "uv.lock"]
    manifest = {str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"): _hash(path.read_bytes()) for path in paths}
    return _hash(_json(manifest).encode()), manifest


def _insert_rows(connection, table, file_id, columns, rows):
    names = ["file_id", *columns]
    connection.executemany(
        f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
        ((file_id, *(row.get(column) for column in columns)) for row in rows),
    )


def _store_workbook(connection, path, sha256, parsed, parser_version, supplier, rules_version):
    cursor = connection.execute(
        """INSERT INTO import_files
        (source_kind, supplier, path, source_name, sha256, imported_at_utc, snapshot_date, rules_version, parser_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (parsed.source_kind, supplier, str(path), path.name, sha256, _now(), parsed.snapshot_date, rules_version, parser_version),
    )
    file_id = cursor.lastrowid
    _insert_rows(connection, "import_sheets", file_id,
                 ["sheet", "state", "declared_dimension", "max_row", "max_column"], parsed.sheets)
    connection.executemany(
        "INSERT INTO source_rows (file_id, sheet, row, cells_json) VALUES (?, ?, ?, ?)",
        ((file_id, row["sheet"], row["row"], _json(row["cells"])) for row in parsed.raw_rows),
    )
    for table, columns in ALL_FACT_COLUMNS.items():
        _insert_rows(connection, table, file_id, columns, getattr(parsed, table, []))
    _insert_rows(connection, "row_metadata", file_id, ["sheet", "row", "hidden"], getattr(parsed, "row_metadata", []))
    _insert_rows(connection, "external_sources", file_id,
                 ["sheet", "xml_path", "external_target", "price_date", "status"], getattr(parsed, "external_sources", []))
    _insert_rows(connection, "import_issues", file_id,
                 ["severity", "code", "sheet", "row", "cell", "message", "sku"], parsed.issues)
    return file_id


def _create_snapshot(connection, versions, parameters, supplier="Systeme Electric"):
    _, _, kinds = _profile(supplier)
    if set(versions) != kinds:
        raise ValueError(f"Набор должен содержать ровно шесть типов источников {supplier}.")
    members = []
    for kind, file_id in sorted(versions.items()):
        row = connection.execute("SELECT * FROM import_files WHERE id=?", (file_id,)).fetchone()
        if row is None or row["source_kind"] != kind or row["supplier"] != supplier:
            raise ValueError(f"Версия {file_id} не соответствует источнику {kind}.")
        members.append({"source_kind": kind, "file_id": file_id})
    chosen_parameters = dict(DEFAULT_PARAMETERS)
    if supplier == "IEK":
        chosen_parameters.update(calculation_version="not-implemented-stage-3", archive_terms_active=False)
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise ValueError("Параметры набора должны быть объектом JSON.")
        # Stage 2 has no transformation parameter overrides yet. Retain extra named
        # scenario metadata, but never imply an unsupported blank/ETA conversion.
        for name, value in chosen_parameters.items():
            if name in parameters and parameters[name] != value:
                raise ValueError(f"Изменение параметра {name} пока не реализовано.")
        chosen_parameters.update(parameters)
    parameter_json = _json(chosen_parameters)
    parameter_version = _hash(parameter_json.encode())
    code_version, manifest = _code_manifest()
    fingerprint = _hash(_json({"supplier": supplier, "members": members, "code": code_version, "parameters": parameter_version}).encode())
    existing = connection.execute("SELECT id FROM snapshots WHERE fingerprint=?", (fingerprint,)).fetchone()
    if existing:
        return existing["id"]
    cursor = connection.execute(
        """INSERT INTO snapshots (fingerprint, created_at_utc, code_version,
        code_manifest_json, parameter_version, parameters_json, supplier) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (fingerprint, _now(), code_version, _json(manifest), parameter_version, parameter_json, supplier),
    )
    snapshot_id = cursor.lastrowid
    connection.executemany(
        "INSERT INTO snapshot_files (snapshot_id, source_kind, file_id) VALUES (?, ?, ?)",
        ((snapshot_id, member["source_kind"], member["file_id"]) for member in members),
    )
    if supplier == "IEK":
        from hackalem.services.units import build_unit_assessments
        build_unit_assessments(connection, snapshot_id)
    return snapshot_id


def create_snapshot(database_path: Path, versions: dict[str, int], parameters: dict | None = None, *, supplier="Systeme Electric"):
    """Pin one existing import per source; old sets are never edited or combined."""
    initialize_database(database_path)
    with closing(_connect(database_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        return _create_snapshot(connection, versions, parameters, supplier)


def import_systeme(settings: Settings, parameters: dict | None = None):
    return import_supplier(settings, "Systeme Electric", parameters)


def import_iek(settings: Settings, parameters: dict | None = None):
    return import_supplier(settings, "IEK", parameters)


def import_supplier(settings: Settings, supplier: str, parameters: dict | None = None):
    """Import all six source types atomically, from immutable in-memory file bytes.

    Cached imports are reused. Copies read by openpyxl live only in a temporary
    runtime directory, so a file changed during parsing cannot change the hash
    provenance of data already being parsed.
    """
    module, directory, _ = _profile(supplier)
    folder = settings.source_dir / directory
    if not folder.is_dir():
        raise ValueError(f"Папка {directory} недоступна: {folder}")
    paths = sorted(path for path in folder.iterdir()
                   if path.is_file() and path.suffix.lower() == ".xlsx" and not path.name.startswith("~$"))
    if not paths:
        raise ValueError(f"В папке {directory} не найдены файлы XLSX.")
    initialize_database(settings.database_path)
    parser_version = _parser_version(module, supplier)
    versions = {}
    reused = 0
    with closing(_connect(settings.database_path)) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        with TemporaryDirectory(prefix="supplier-import-", dir=settings.data_dir) as temporary:
            for path in paths:
                content = path.read_bytes()
                digest = _hash(content)
                existing = connection.execute(
                    "SELECT id, source_kind FROM import_files WHERE sha256=? AND source_name=? AND rules_version=? AND parser_version=? AND supplier=?",
                    (digest, path.name, module.RULES_VERSION, parser_version, supplier),
                ).fetchone()
                if existing:
                    file_id, kind = existing["id"], existing["source_kind"]
                    reused += 1
                else:
                    copy = Path(temporary) / path.name
                    copy.write_bytes(content)
                    try:
                        parsed = module.parse_workbook(copy)
                    except Exception as error:
                        raise ValueError(f"Не удалось импортировать {path.name}: {error}") from error
                    kind = parsed.source_kind
                    file_id = _store_workbook(connection, path.resolve(), digest, parsed, parser_version, supplier, module.RULES_VERSION)
                    del parsed
                if kind in versions:
                    raise ValueError(f"Найдено несколько файлов типа {kind}. Оставьте один файл каждого типа в папке импорта.")
                versions[kind] = file_id
                connection.execute(
                    "INSERT OR IGNORE INTO import_locations (file_id, path, first_seen_utc) VALUES (?, ?, ?)",
                    (file_id, str(path.resolve()), _now()),
                )
        snapshot_id = _create_snapshot(connection, versions, parameters, supplier)
    report = report_snapshot(settings.database_path, snapshot_id)
    report["reused_files"] = reused
    return report


def list_snapshots(database_path: Path, supplier: str | None = None):
    with closing(_connect(database_path)) as connection:
        query = "SELECT id, created_at_utc, code_version, supplier FROM snapshots"
        args = ()
        if supplier is not None:
            query += " WHERE supplier=?"
            args = (supplier,)
        return [dict(row) for row in connection.execute(query + " ORDER BY id DESC", args)]


def _snapshot(connection, snapshot_id):
    row = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError(f"Набор версий №{snapshot_id} не найден.")
    return row


def report_snapshot(database_path: Path, snapshot_id: int):
    with closing(_connect(database_path)) as connection:
        snapshot = _snapshot(connection, snapshot_id)
        files = [dict(row) for row in connection.execute(
            """SELECT f.* FROM import_files f JOIN snapshot_files s ON s.file_id=f.id
            WHERE s.snapshot_id=? ORDER BY f.source_kind""", (snapshot_id,),
        )]
        count_tables = [*ALL_FACT_COLUMNS, "source_rows"]
        totals = {"files": len(files), "sheets": 0, "external_sheets": 0, "hidden_rows": 0,
                  **{name: 0 for name in [*ALL_FACT_COLUMNS, "raw_rows"]}}
        for file in files:
            file["sheets"] = [dict(row) for row in connection.execute(
                "SELECT sheet, state, declared_dimension, max_row, max_column FROM import_sheets WHERE file_id=? ORDER BY sheet",
                (file["id"],),
            )]
            totals["sheets"] += sum(sheet["state"] != "external_cache" for sheet in file["sheets"])
            totals["external_sheets"] += sum(sheet["state"] == "external_cache" for sheet in file["sheets"])
            file["external_sources"] = [dict(row) for row in connection.execute(
                "SELECT * FROM external_sources WHERE file_id=?", (file["id"],))]
            file["hidden_rows"] = connection.execute(
                "SELECT COUNT(*) FROM row_metadata WHERE file_id=? AND hidden=1", (file["id"],)).fetchone()[0]
            totals["hidden_rows"] += file["hidden_rows"]
            file["counts"] = {}
            for table in count_tables:
                name = "raw_rows" if table == "source_rows" else table
                count = connection.execute(f"SELECT COUNT(*) FROM {table} WHERE file_id=?", (file["id"],)).fetchone()[0]
                file["counts"][name] = count
                totals[name] += count
            file["distinct_skus"] = connection.execute(
                "SELECT COUNT(DISTINCT sku) FROM products WHERE file_id=?", (file["id"],)
            ).fetchone()[0]
        issue_codes = [dict(row) for row in connection.execute(
            """SELECT i.code, i.severity, COUNT(*) AS count FROM import_issues i
            JOIN snapshot_files s ON s.file_id=i.file_id WHERE s.snapshot_id=?
            GROUP BY i.code, i.severity ORDER BY i.severity, i.code""", (snapshot_id,),
        )]
        issues = {severity: sum(row["count"] for row in issue_codes if row["severity"] == severity)
                  for severity in ("error", "warning")}
        examples = [dict(row) for row in connection.execute(
            """SELECT s.source_kind, i.severity, i.code, i.sheet, i.row, i.cell, i.message, i.sku
            FROM import_issues i JOIN snapshot_files s ON s.file_id=i.file_id
            WHERE s.snapshot_id=? ORDER BY i.severity, i.id LIMIT 100""", (snapshot_id,),
        )]
        return {
            "snapshot_id": snapshot_id, "created_at_utc": snapshot["created_at_utc"], "supplier": snapshot["supplier"],
            "files": files, "totals": totals, "issues": issues, "issue_codes": issue_codes,
            "issue_examples": examples, "issue_examples_limit": 100,
            "unit_assessments": [dict(row) for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM unit_assessments WHERE snapshot_id=? GROUP BY status", (snapshot_id,))],
            "versions": {
                "rules_version": sorted({file["rules_version"] for file in files}),
                "code_version": snapshot["code_version"],
                "code_manifest": json.loads(snapshot["code_manifest_json"]),
                "parameters": json.loads(snapshot["parameters_json"]),
                "parameter_version": snapshot["parameter_version"],
            },
        }


def _member(connection, snapshot_id, source_kind):
    _snapshot(connection, snapshot_id)
    row = connection.execute(
        """SELECT f.* FROM import_files f JOIN snapshot_files s ON s.file_id=f.id
        WHERE s.snapshot_id=? AND s.source_kind=?""", (snapshot_id, source_kind),
    ).fetchone()
    if row is None:
        raise ValueError(f"Источник {source_kind} отсутствует в наборе №{snapshot_id}.")
    return row


def read_records(database_path: Path, snapshot_id: int, source_kind: str,
                 table: str, sku: str | None = None, *, article: str | None = None):
    """A source must be named explicitly; alternative sales reports never add up."""
    if table not in ALL_FACT_COLUMNS:
        raise ValueError("Неизвестный тип нормализованных записей.")
    with closing(_connect(database_path)) as connection:
        file = _member(connection, snapshot_id, source_kind)
        query = f"SELECT * FROM {table} WHERE file_id=?"
        args = [file["id"]]
        if sku is not None:
            if "sku" not in ALL_FACT_COLUMNS[table]:
                raise ValueError("Эта таблица не содержит SKU; для прайса используйте артикул.")
            query += " AND sku=?"
            args.append(sku)
        if article is not None:
            if "article" not in ALL_FACT_COLUMNS[table]:
                raise ValueError("Эта таблица не содержит артикула.")
            query += " AND article=?"
            args.append(article)
        query += " ORDER BY sheet, row, cell"
        return [dict(row) for row in connection.execute(query, args)]


def trace_cell(database_path: Path, snapshot_id: int, source_kind: str, sheet: str, cell: str):
    cell = cell.strip().upper()
    match = re.fullmatch(r"([A-Z]{1,3})([1-9][0-9]*)", cell)
    if not match:
        raise ValueError("Укажите адрес одной ячейки, например AP3.")
    column, row_number = match.group(1), int(match.group(2))
    column_number = 0
    for letter in column:
        column_number = column_number * 26 + ord(letter) - ord("A") + 1
    with closing(_connect(database_path)) as connection:
        file = _member(connection, snapshot_id, source_kind)
        metadata = connection.execute(
            "SELECT * FROM import_sheets WHERE file_id=? AND sheet=?", (file["id"], sheet),
        ).fetchone()
        if metadata is None or row_number > metadata["max_row"] or column_number > metadata["max_column"]:
            raise ValueError("Лист или ячейка вне сохранённой области источника.")
        raw = connection.execute(
            "SELECT cells_json FROM source_rows WHERE file_id=? AND sheet=? AND row=?",
            (file["id"], sheet, row_number),
        ).fetchone()
        cells = json.loads(raw["cells_json"]) if raw else {}
        source_cell = cells.get(column, {"value": None, "formula": None, "data_type": None, "number_format": None})
        facts = []
        for table in ALL_FACT_COLUMNS:
            predicate, args = "cell=?", [cell]
            if table == "incoming_orders":
                predicate, args = "(cell=? OR header_cell=?)", [cell, cell]
            elif table == "catalog_items":
                predicate, args = "row=?", [row_number]
            facts.extend({"table": table, **dict(row)} for row in connection.execute(
                f"SELECT * FROM {table} WHERE file_id=? AND sheet=? AND {predicate}",
                (file["id"], sheet, *args),
            ))
        return {
            "snapshot_id": snapshot_id, "file": dict(file), "sheet": sheet,
            "sheet_state": metadata["state"], "row": row_number, "cell": cell,
            "source": source_cell, "normalized": facts,
            "row_metadata": dict(row) if (row := connection.execute(
                "SELECT hidden FROM row_metadata WHERE file_id=? AND sheet=? AND row=?",
                (file["id"], sheet, row_number)).fetchone()) else None,
            "external_source": dict(row) if (row := connection.execute(
                "SELECT * FROM external_sources WHERE file_id=? AND sheet=?", (file["id"], sheet)).fetchone()) else None,
            "issues": [dict(row) for row in connection.execute(
                """SELECT severity, code, message FROM import_issues WHERE file_id=? AND sheet=?
                AND (cell=? OR (cell IS NULL AND (row IS NULL OR row=?)))""",
                (file["id"], sheet, cell, row_number),
            )],
        }
