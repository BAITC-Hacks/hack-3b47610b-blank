"""Command-line import and inspection of immutable Systeme Electric snapshots."""

import argparse
import json
import sqlite3
import sys

from hackalem.config import load_settings
from hackalem.services.bootstrap import bootstrap
from hackalem.services.imports import (
    import_iek,
    import_systeme,
    list_snapshots,
    report_snapshot,
    trace_cell,
)
from hackalem.services.units import get_unit_assessment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hackalem",
        description="Импорт и проверка данных Systeme Electric и IEK.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("import-systeme", help="Импортировать отчёты в новый снимок")
    commands.add_parser("import-iek", help="Импортировать IEK и встроенный архивный прайс")
    commands.add_parser("list-snapshots", help="Показать сохранённые снимки")

    report = commands.add_parser("report", help="Показать отчёт выбранного снимка")
    report.add_argument("--snapshot", type=int, required=True, help="Номер снимка")

    trace = commands.add_parser("trace", help="Показать происхождение ячейки")
    trace.add_argument("--snapshot", type=int, required=True, help="Номер снимка")
    trace.add_argument("--source", required=True, help="Вид источника, например current")
    trace.add_argument("--sheet", required=True, help="Точное имя листа")
    trace.add_argument("--cell", required=True, help="Адрес ячейки, например AP3")
    units = commands.add_parser("units", help="Показать единицы и неподтверждённый коэффициент SKU")
    units.add_argument("--snapshot", type=int, required=True)
    units.add_argument("--sku", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        state = bootstrap(load_settings())
        database_path = state.settings.database_path
        if args.command == "import-systeme":
            result = import_systeme(state.settings)
        elif args.command == "import-iek":
            result = import_iek(state.settings)
        elif args.command == "units":
            result = get_unit_assessment(database_path, args.snapshot, args.sku)
        elif args.command == "list-snapshots":
            result = list_snapshots(database_path)
        elif args.command == "report":
            result = report_snapshot(database_path, args.snapshot)
        else:
            result = trace_cell(
                database_path,
                args.snapshot,
                args.source,
                args.sheet,
                args.cell.strip().upper(),
            )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
