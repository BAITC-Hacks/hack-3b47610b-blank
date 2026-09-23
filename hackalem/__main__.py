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
from hackalem.services.cleaning import cleaning_report, list_cleaning_runs, run_cleaning
from hackalem.services.forecasting import forecast_report, list_forecast_runs, run_forecast
from hackalem.services.lost_demand import lost_demand_report, list_lost_demand_runs, run_lost_demand
from hackalem.services.replenishment import run_replenishment, replenishment_report
from hackalem.services.orders import (
    approve_order, create_order_project, export_order_file, order_report,
    submit_order_for_review, update_order_item,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hackalem",
        description="Импорт и проверка данных Systeme Electric и IEK.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Подготовить отдельную синтетическую демонстрацию")
    demo.add_argument("--output-root", type=Path)
    demo.add_argument("--seed", type=int, default=20260923)
    backup = commands.add_parser("db-backup", help="Согласованная резервная копия SQLite в новый файл")
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("db-restore", help="Восстановить копию в пустой HACKALEM_DATA_DIR")
    restore.add_argument("--input", type=Path, required=True)
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
    cleaning = commands.add_parser("clean", help="Сохранить отдельную версию подготовки регулярного спроса")
    cleaning.add_argument("--snapshot", type=int, required=True)
    cleaning.add_argument("--as-of", required=True, help="Дата среза YYYY-MM-DD; текущий месяц исключается")
    cleaning.add_argument("--policy", choices=("review_only", "exclude_high_confidence"), default="review_only")
    cleaning.add_argument("--decisions", type=Path, help="JSON-массив ручных решений в UTF-8")
    cleaning_report_command = commands.add_parser("cleaning-report", help="Показать подготовленные документы и месяцы")
    cleaning_report_command.add_argument("--run", type=int, required=True)
    cleaning_report_command.add_argument("--sku")
    cleaning_report_command.add_argument("--limit", type=int, default=100)
    cleaning_runs = commands.add_parser("cleaning-runs", help="Показать версии подготовки снимка")
    cleaning_runs.add_argument("--snapshot", type=int, required=True)
    lost = commands.add_parser("lost-demand", help="Оценить упущенный спрос по полнодневному наличию")
    lost.add_argument("--cleaning-run", type=int, required=True)
    lost.add_argument("--sku", required=True)
    lost.add_argument("--scenario-evidence", type=Path, help="JSON-массив ручных интервалов; результат всегда сценарный")
    lost_report = commands.add_parser("lost-demand-report", help="Показать сохранённую оценку и причины")
    lost_report.add_argument("--run", type=int, required=True)
    lost_report.add_argument("--limit", type=int, default=100)
    lost_runs = commands.add_parser("lost-demand-runs", help="Показать оценки выбранной подготовки")
    lost_runs.add_argument("--cleaning-run", type=int, required=True)
    forecast = commands.add_parser("forecast", help="Сохранить месячный прогноз одного SKU")
    forecast.add_argument("--quality-run", type=int, required=True)
    forecast.add_argument("--cleaning-run", type=int, required=True)
    forecast.add_argument("--sku", required=True)
    forecast.add_argument("--config", type=Path, required=True, help="JSON-настройка прогноза")
    forecast.add_argument("--allow-scenario", action="store_true")
    forecast.add_argument("--lost-demand-run", type=int,
                          help="Совместимая версия оценки потерь этапа 7")
    forecast_report_command = commands.add_parser("forecast-report", help="Показать сохранённый прогноз")
    forecast_report_command.add_argument("--run", type=int, required=True)
    forecast_runs = commands.add_parser("forecast-runs", help="Показать прогнозы снимка")
    forecast_runs.add_argument("--snapshot", type=int, required=True)
    forecast_runs.add_argument("--sku")
    evaluation = commands.add_parser("forecast-evaluate", help="Проверить прогнозы только по синтетическому эталону")
    evaluation.add_argument("--dataset", type=Path, required=True)
    evaluation.add_argument("--runs", type=int, nargs="+", required=True)
    replenish = commands.add_parser("replenish", help="Сохранить сценарный расчёт пополнения из JSON")
    replenish.add_argument("--file", type=Path, required=True)
    replenish_report = commands.add_parser("replenishment-report", help="Воспроизвести сохранённый расчёт по run_id")
    replenish_report.add_argument("--run", type=int, required=True)
    replenish_report.add_argument("--sku")
    order_create = commands.add_parser("order-create", help="Создать проект заказа из расчёта пополнения")
    order_create.add_argument("--replenishment-run", type=int, required=True)
    order_create.add_argument("--actor", required=True)
    order_report_command = commands.add_parser("order-report", help="Показать версию проекта заказа")
    order_report_command.add_argument("--version", type=int, required=True)
    order_update = commands.add_parser("order-update", help="Сохранить количество менеджера по стабильному SKU")
    order_update.add_argument("--version", type=int, required=True)
    order_update.add_argument("--sku", required=True)
    order_update.add_argument("--quantity", type=float, required=True)
    order_update.add_argument("--actor", required=True)
    order_update.add_argument("--reason", required=True)
    order_submit = commands.add_parser("order-submit", help="Передать черновик заказа на проверку")
    order_submit.add_argument("--version", type=int, required=True)
    order_submit.add_argument("--actor", required=True)
    order_submit.add_argument("--reason", required=True)
    order_approve = commands.add_parser("order-approve", help="Зафиксировать локальное утверждение версии")
    order_approve.add_argument("--version", type=int, required=True)
    order_approve.add_argument("--responsible", required=True)
    order_approve.add_argument("--note", required=True)
    order_export = commands.add_parser("order-export", help="Выгрузить и проверить CSV/XLSX выбранной версии")
    order_export.add_argument("--version", type=int, required=True)
    order_export.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        # These commands must not bootstrap/migrate the live database first.
        if args.command == "demo":
            from hackalem.services.demo import DEFAULT_DEMO_ROOT, prepare_demo
            result = prepare_demo(args.output_root or DEFAULT_DEMO_ROOT, args.seed)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in ("db-backup", "db-restore"):
            from hackalem.services.backup import copy_database
            database = load_settings().database_path
            result = (copy_database(database, args.output) if args.command == "db-backup"
                      else copy_database(args.input, database))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in ("synthetic-generate", "synthetic-report", "forecast-evaluate"):
            from hackalem.services.synthetic import (
                DEFAULT_OUTPUT_ROOT, create_synthetic_dataset, evaluate_forecasts, synthetic_report,
            )
            if args.command == "synthetic-generate":
                result = create_synthetic_dataset(args.output_root or DEFAULT_OUTPUT_ROOT, args.seed)
            elif args.command == "synthetic-report":
                result = synthetic_report(args.dataset)
            else:
                result = evaluate_forecasts(args.dataset, args.runs)
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
        elif args.command == "clean":
            decisions = json.loads(args.decisions.read_text(encoding="utf-8-sig")) if args.decisions else []
            result = run_cleaning(database_path, args.snapshot, args.as_of,
                                  policy=args.policy, decisions=decisions)
        elif args.command == "cleaning-report":
            result = cleaning_report(database_path, args.run, sku=args.sku, limit=args.limit)
        elif args.command == "cleaning-runs":
            result = list_cleaning_runs(database_path, args.snapshot)
        elif args.command == "lost-demand":
            evidence = json.loads(args.scenario_evidence.read_text(encoding="utf-8-sig")) if args.scenario_evidence else None
            result = run_lost_demand(database_path, args.cleaning_run, args.sku, scenario_intervals=evidence)
        elif args.command == "lost-demand-report":
            result = lost_demand_report(database_path, args.run, limit=args.limit)
        elif args.command == "lost-demand-runs":
            result = list_lost_demand_runs(database_path, args.cleaning_run)
        elif args.command == "forecast":
            payload = json.loads(args.config.read_text(encoding="utf-8-sig"))
            result = run_forecast(database_path, args.quality_run, args.cleaning_run, args.sku,
                                  payload, allow_scenario=args.allow_scenario,
                                  lost_demand_run_id=args.lost_demand_run)
        elif args.command == "forecast-report":
            result = forecast_report(database_path, args.run)
        elif args.command == "forecast-runs":
            result = list_forecast_runs(database_path, args.snapshot, args.sku)
        elif args.command == "replenish":
            result = run_replenishment(database_path, json.loads(args.file.read_text(encoding="utf-8-sig")))
        elif args.command == "replenishment-report":
            result = replenishment_report(database_path, args.run, sku=args.sku)
        elif args.command == "order-create":
            result = create_order_project(database_path, args.replenishment_run, args.actor)
        elif args.command == "order-report":
            result = order_report(database_path, args.version)
        elif args.command == "order-update":
            result = update_order_item(database_path, args.version, args.sku,
                                       args.quantity, args.actor, args.reason)
        elif args.command == "order-submit":
            result = submit_order_for_review(database_path, args.version, args.actor, args.reason)
        elif args.command == "order-approve":
            result = approve_order(database_path, args.version, args.responsible, args.note)
        elif args.command == "order-export":
            result = export_order_file(database_path, args.version, args.output)
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
