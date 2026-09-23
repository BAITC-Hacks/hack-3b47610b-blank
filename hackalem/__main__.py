"""Command-line import, provenance and readiness checks for versioned data."""

import argparse
import json
from pathlib import Path
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
from hackalem.services.quality import (
    list_configurations,
    quality_report,
    run_quality,
    save_configuration,
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

    quality = commands.add_parser("quality", help="Проверить согласованность источников и готовность данных")
    quality.add_argument("--snapshot", type=int, required=True, help="Номер снимка")
    quality.add_argument("--config", type=int, help="Номер сохранённой настройки; без него параметры остаются неизвестными")

    inspection = commands.add_parser("quality-report", help="Показать сохранённую проверку данных")
    inspection.add_argument("--run", type=int, required=True, help="Номер проверки")
    inspection.add_argument("--sku", help="Точный код товара для фильтра")
    inspection.add_argument("--limit", type=int, default=100, help="Предельное число строк каждого списка (по умолчанию 100)")

    configure = commands.add_parser("configure", help="Сохранить версию параметров из JSON-файла")
    configure.add_argument("--snapshot", type=int, required=True, help="Номер снимка")
    configure.add_argument("--file", type=Path, required=True, help="Путь к JSON в UTF-8 (допускается BOM)")

    configs = commands.add_parser("configs", help="Показать настройки выбранного снимка")
    configs.add_argument("--snapshot", type=int, required=True, help="Номер снимка")
    synthetic = commands.add_parser("synthetic-generate", help="Создать отдельный синтетический проверочный dataset")
    synthetic.add_argument("--seed", type=int, default=20260923)
    synthetic.add_argument("--output-root", type=Path, help="Родительская папка наборов; по умолчанию .local/synthetic")
    synthetic_report = commands.add_parser("synthetic-report", help="Проверить целостность синтетического dataset и показать готовность")
    synthetic_report.add_argument("--dataset", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        if args.command in ("synthetic-generate", "synthetic-report"):
            from hackalem.services.synthetic import DEFAULT_OUTPUT_ROOT, create_synthetic_dataset, synthetic_report
            result = (create_synthetic_dataset(args.output_root or DEFAULT_OUTPUT_ROOT, args.seed)
                      if args.command == "synthetic-generate" else synthetic_report(args.dataset))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
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
        elif args.command == "quality":
            result = run_quality(database_path, args.snapshot, configuration_id=args.config)
        elif args.command == "quality-report":
            if args.limit <= 0:
                raise ValueError("Число строк --limit должно быть положительным.")
            result = quality_report(database_path, args.run, sku=args.sku, limit=args.limit)
        elif args.command == "configure":
            payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
            if not isinstance(payload, dict):
                raise ValueError("Настройка должна быть объектом JSON.")
            result = save_configuration(database_path, args.snapshot, payload)
        elif args.command == "configs":
            result = list_configurations(database_path, args.snapshot)
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
