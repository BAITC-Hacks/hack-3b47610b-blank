"""Systeme Electric import status and source lineage, without procurement calculations."""

from pathlib import Path
import sqlite3
from datetime import date

import streamlit as st

from hackalem.config import PROJECT_ROOT, Settings, load_settings
from hackalem.services.bootstrap import bootstrap
from hackalem.services.imports import (
    SUPPLIERS,
    import_supplier,
    list_snapshots,
    report_snapshot,
    trace_cell,
)
from hackalem.services.units import (
    confirm_unit_conversion, convert_quantity, get_unit_assessment, list_unit_issues,
)
from hackalem.storage import UnsupportedSchemaError
from hackalem.services.datasets import dataset_context


_SERVICE_ERRORS = (OSError, ValueError, sqlite3.Error, RuntimeError)
_COUNT_LABELS = {
    "products": "Строк товаров",
    "transactions": "Операций",
    "monthly_values": "Месячных значений",
    "measures": "Показателей",
    "seasonal_values": "Сезонных значений",
    "raw_rows": "Исходных строк",
    "incoming_orders": "Строк поступлений",
    "catalog_items": "Артикулов архива",
}


def _render_report(report: dict) -> None:
    st.subheader(f"Снимок № {report['snapshot_id']}")
    st.caption(f"Сохранён: {report['created_at_utc']} (UTC)")
    st.caption(f"Поставщик: {report['supplier']}")
    if report["supplier"] == "IEK":
        dates = sorted({entry["price_date"] for file in report["files"]
                        for entry in file["external_sources"] if entry["price_date"]})
        date_label = ", ".join(dates) if dates else "дата не установлена"
        st.warning(f"Прайс ({date_label}) — архивный кэш. Цены и условия не подтверждены как действующие. Месячные остатки IEK — начальные, не текущие.")
    totals = report["totals"]
    columns = st.columns(4)
    columns[0].metric("Файлов", totals.get("files", 0))
    columns[1].metric("Листов", totals.get("sheets", 0))
    columns[2].metric("Ошибок данных", report["issues"].get("error", 0))
    columns[3].metric("Предупреждений", report["issues"].get("warning", 0))

    st.markdown("**Сохранённые источники**")
    files = []
    sheets = []
    for source in report["files"]:
        record = {
            "Версия файла": source["id"],
            "Источник": source["source_kind"],
            "Файл": Path(source["path"]).name,
            "Дата среза": source.get("snapshot_date") or "Не указана",
        }
        record.update(
            {label: source["counts"].get(name, 0) for name, label in _COUNT_LABELS.items()}
        )
        files.append(record)
        for sheet in source["sheets"]:
            sheets.append(
                {
                    "Источник": source["source_kind"],
                    "Лист": sheet["sheet"],
                    "Состояние": sheet["state"],
                    "Строк": sheet["max_row"],
                    "Столбцов": sheet["max_column"],
                }
            )
    st.dataframe(files, hide_index=True, width="stretch")
    with st.expander("Листы, контрольные суммы и версии"):
        st.dataframe(sheets, hide_index=True, width="stretch")
        st.dataframe(
            [
                {
                    "Источник": source["source_kind"],
                    "Путь": source["path"],
                    "SHA-256": source["sha256"],
                }
                for source in report["files"]
            ],
            hide_index=True,
            width="stretch",
        )
        st.json(report["versions"])

    st.markdown("**Замечания к данным**")
    if report["issue_codes"]:
        st.dataframe(
            [
                {
                    "Код": issue["code"],
                    "Уровень": issue["severity"],
                    "Количество": issue["count"],
                }
                for issue in report["issue_codes"]
            ],
            hide_index=True,
            width="stretch",
        )
        with st.expander("Примеры замечаний с координатами"):
            st.caption("Показаны первые 100 замечаний. Все замечания сохранены в локальной базе.")
            st.dataframe(report["issue_examples"], hide_index=True, width="stretch")
    else:
        st.write("Импорт не зарегистрировал замечаний.")


def _render_units(database_path, report):
    if report["supplier"] != "IEK":
        return
    snapshot_id = report["snapshot_id"]
    st.subheader("Единицы учёта и закупки")
    st.dataframe(report["unit_assessments"], hide_index=True, width="stretch")
    with st.expander("Товары с отсутствующими или различающимися единицами"):
        st.caption("Первые 100 записей. Для конкретного товара введите код ниже.")
        st.dataframe(list_unit_issues(database_path, snapshot_id), hide_index=True, width="stretch")
    sku = st.text_input("Код товара для проверки единиц", placeholder="Например, 280200087_").strip()
    if not sku:
        return
    try:
        assessment = get_unit_assessment(database_path, snapshot_id, sku)
    except _SERVICE_ERRORS as error:
        st.warning(str(error))
        return
    st.write({"Учётная единица": assessment["accounting_unit"],
              "Закупочная единица из архива": assessment["purchase_unit"],
              "Предлагаемый коэффициент (не подтверждён)": assessment["proposed_factor"],
              "Статус": assessment["status"]})
    with st.expander("Источники единиц, MOQ, кратность и подтверждения"):
        st.json(assessment)
    if not assessment["accounting_unit"] or not assessment["purchase_unit"]:
        st.info("Сначала нужно уточнить отсутствующие или неоднозначные единицы.")
        return
    with st.expander("Подтвердить перевод единиц"):
        st.caption("Подтверждение относится только к этому товару и снимку. Оно не подтверждает актуальность архивных цен или MOQ.")
        with st.form(f"confirm_unit_{snapshot_id}_{sku}"):
            factor = st.number_input("Подтверждаемый коэффициент", min_value=0.000001, value=None,
                                     placeholder="Укажите проверенный коэффициент")
            quantity = st.number_input("Количество в закупочной единице для проверки", min_value=0.0, value=None)
            actor = st.text_input("Кто подтвердил")
            reason = st.text_input("Основание подтверждения")
            approved = st.checkbox("Подтверждаю применимость указанных единиц и коэффициента")
            submitted = st.form_submit_button("Сохранить подтверждение единиц")
        if submitted:
            try:
                decision = confirm_unit_conversion(database_path, snapshot_id, sku, factor,
                                                   archive_units_confirmed=approved, reason=reason, confirmed_by=actor)
                st.success(f"Подтверждение №{decision['id']} сохранено.")
                if quantity is not None:
                    st.json(convert_quantity(database_path, snapshot_id, sku, quantity, confirmation_id=decision["id"]))
            except _SERVICE_ERRORS as error:
                st.error(str(error))


def _render_lineage(database_path: Path, report: dict) -> None:
    st.subheader("Происхождение значения")
    st.caption("Выберите источник и ячейку, чтобы посмотреть сохранённые данные и формулу.")
    files = {source["source_kind"]: source for source in report["files"]}
    if not files:
        return
    source_kind = st.selectbox("Источник", list(files), key="lineage_source")
    sheet_names = [sheet["sheet"] for sheet in files[source_kind]["sheets"]]
    if not sheet_names:
        st.info("В выбранном источнике нет сохранённых листов.")
        return
    with st.form("lineage_lookup"):
        sheet = st.selectbox("Лист", sheet_names)
        cell = st.text_input("Ячейка", placeholder="Например, AP3").strip().upper()
        submitted = st.form_submit_button("Показать происхождение")
    if submitted:
        if not cell:
            st.warning("Укажите адрес ячейки.")
            return
        try:
            result = trace_cell(
                database_path, report["snapshot_id"], source_kind, sheet, cell
            )
        except _SERVICE_ERRORS as error:
            st.error(f"Не удалось получить происхождение значения: {error}")
        else:
            st.json(result)


def _reset_workspace_selection() -> None:
    for key in list(st.session_state):
        if key != "workspace_dataset":
            del st.session_state[key]


def _reset_calculation_selection() -> None:
    for key in list(st.session_state):
        if ((key.startswith("procurement_") or key.startswith("card_") or
             key.startswith("scenario_")) and key.endswith(("_run_id", "_base")) or
                (key.startswith("order_") and key.endswith("_source_run"))):
            st.session_state.pop(key, None)


def _reset_snapshot_selection() -> None:
    st.session_state.pop("selected_snapshot_id", None)
    _reset_calculation_selection()


def _dataset_options(current_path: Path) -> dict[str, dict]:
    candidates = [current_path, PROJECT_ROOT / ".local" / "hackalem.sqlite3"]
    candidates.extend(sorted((PROJECT_ROOT / ".local" / "synthetic").glob("*/model/hackalem.sqlite3")))
    result = {}
    for path in candidates:
        path = path.resolve()
        if path.exists() and str(path) not in result:
            try:
                result[str(path)] = dataset_context(path)
            except (ValueError, OSError, sqlite3.Error):
                continue
    return result


def render_home() -> None:
    st.set_page_config(page_title="Помощник закупщика", page_icon="📦", layout="wide")
    st.title("Помощник закупщика")
    st.caption("Электрокомплект · Локальный кабинет закупок")

    try:
        settings = load_settings()
        initial = bootstrap(settings)
        datasets = _dataset_options(initial.settings.database_path)
    except (OSError, ValueError, sqlite3.Error, UnsupportedSchemaError) as error:
        st.error(f"Не удалось подготовить локальное хранилище: {error}")
        st.info("Проверьте пути и доступ к папкам. Исходные отчёты не изменяются.")
        st.stop()

    with st.sidebar:
        st.markdown("**Рабочее пространство**")
        selected_path = st.selectbox(
            "Dataset", list(datasets), index=list(datasets).index(str(initial.settings.database_path.resolve())),
            key="workspace_dataset", on_change=_reset_workspace_selection,
            format_func=lambda value: (
                f"{datasets[value]['label']} · {datasets[value]['dataset_id']} · {value}"
            ),
        )
    try:
        state = bootstrap(Settings(source_dir=settings.source_dir, data_dir=Path(selected_path).parent))
        dataset = dataset_context(state.settings.database_path)
    except (OSError, ValueError, sqlite3.Error, UnsupportedSchemaError) as error:
        st.error(f"Не удалось открыть выбранный dataset: {error}")
        st.stop()
    with st.sidebar:
        st.write("Локальное хранилище подключено")
        with st.expander("Расположение файлов"):
            st.write("Исходные отчёты")
            st.code(str(state.settings.source_dir), language=None)
            st.write("Локальное хранилище")
            st.code(str(state.storage.path), language=None)

    if dataset['kind'] == 'synthetic':
        st.warning('СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР — вымышленные товары, клиенты и условия. Для реальных закупок не применяется.')
        st.caption(f"Dataset: {dataset['dataset_id']}. Скрытый спрос и эталонные метки в интерфейс модели не загружаются.")
    try:
        snapshots = list_snapshots(state.settings.database_path)
    except _SERVICE_ERRORS as error:
        st.error(f"Не удалось прочитать список снимков: {error}")
        st.stop()

    suppliers = sorted({snapshot["supplier"] for snapshot in snapshots})
    with st.sidebar:
        supplier_filter = st.selectbox("Поставщик", ["Все", *suppliers], key="workspace_supplier",
                                       on_change=_reset_snapshot_selection)
        st.selectbox("Склад / охват", ["source_report"], key="workspace_scope",
                     format_func=lambda _: "Весь охват выбранного отчёта")
        default_date = date(2026, 1, 1) if dataset["kind"] == "synthetic" else date(2026, 9, 22)
        calculation_date = st.date_input("Дата расчёта", value=default_date,
                                         key=f"workspace_date_{dataset['dataset_id']}",
                                         on_change=_reset_calculation_selection)
    visible_snapshots = [row for row in snapshots if supplier_filter == "Все" or row["supplier"] == supplier_filter]
    snapshots_by_id = {snapshot["id"]: snapshot for snapshot in visible_snapshots}
    pending_snapshot_id = st.session_state.pop("_pending_snapshot_id", None)
    if pending_snapshot_id in snapshots_by_id:
        st.session_state["selected_snapshot_id"] = pending_snapshot_id
    if st.session_state.get("selected_snapshot_id") not in snapshots_by_id:
        st.session_state.pop("selected_snapshot_id", None)
    selected_id = st.selectbox(
        "Версия данных", list(snapshots_by_id), index=None,
        placeholder="Выберите снимок поставщика", key="selected_snapshot_id",
        format_func=lambda value: (
            f"№ {value} · {snapshots_by_id[value]['supplier']} · "
            f"{snapshots_by_id[value]['created_at_utc']} (UTC)"
        ),
    ) if snapshots_by_id else None

    data_tab, recommendations_tab, card_tab, scenarios_tab, orders_tab = st.tabs([
        "Данные и проверки", "Рекомендации", "Карточка товара", "Сценарии", "Заказы",
    ])
    with data_tab:
        if not state.source_directory_exists:
            st.warning("Папка исходных отчётов недоступна. Укажите HACKALEM_SOURCE_DIR; ошибка не скрыта.")
        st.subheader("Данные поставщиков")
        st.caption("Импорт сохраняет версии файлов, значения и их происхождение.")
        import_success = st.session_state.pop("_import_success", None)
        if import_success:
            st.success(import_success)
        import_supplier_name = st.selectbox("Поставщик для импорта", SUPPLIERS, key="import_supplier")
        if st.button(
            f"Импортировать {import_supplier_name}", type="primary",
            disabled=not state.source_directory_exists or dataset['kind'] == 'synthetic',
        ):
            try:
                with st.spinner("Читаем отчёты и сохраняем снимок…"):
                    imported = import_supplier(state.settings, import_supplier_name)
            except _SERVICE_ERRORS as error:
                st.error(f"Не удалось импортировать отчёты: {error}")
            else:
                st.session_state["_pending_snapshot_id"] = imported["snapshot_id"]
                st.session_state["_import_success"] = (
                    f"Снимок № {imported['snapshot_id']} сохранён. "
                    f"Файлов без изменений: {imported.get('reused_files', 0)}."
                )
                st.rerun()
        if not snapshots:
            st.info("Данные пока не загружены.")
        elif selected_id is None:
            st.info("Выберите версию данных, чтобы проверить импорт, параметры и происхождение.")
        else:
            try:
                report = report_snapshot(state.settings.database_path, selected_id)
            except _SERVICE_ERRORS as error:
                st.error(f"Не удалось прочитать снимок: {error}")
            else:
                _render_report(report)
                _render_lineage(state.settings.database_path, report)
                _render_units(state.settings.database_path, report)
                from hackalem.ui.quality_panel import render_quality_panel
                render_quality_panel(state.settings.database_path, selected_id)
                from hackalem.ui.cleaning_panel import render_cleaning_panel
                render_cleaning_panel(state.settings.database_path, selected_id)
                from hackalem.ui.lost_demand_panel import render_lost_demand_panel
                render_lost_demand_panel(state.settings.database_path, selected_id)
                from hackalem.ui.forecast_panel import render_forecast_panel
                render_forecast_panel(state.settings.database_path, selected_id)
    from hackalem.ui.procurement_panel import (
        render_orders, render_product_card, render_recommendations, render_scenarios,
    )
    if selected_id is None:
        for tab, text in ((recommendations_tab, "Выберите версию данных для рекомендаций."),
                          (card_tab, "Выберите версию данных и расчёт для карточки товара."),
                          (scenarios_tab, "Выберите версию данных и базовый расчёт для сценария."),
                          (orders_tab, "Выберите версию данных.")):
            with tab:
                st.info(text)
    else:
        supplier = snapshots_by_id[selected_id]["supplier"]
        with recommendations_tab:
            render_recommendations(state.settings.database_path, selected_id, supplier,
                                   "all_selected_warehouses", calculation_date)
        with card_tab:
            render_product_card(state.settings.database_path, selected_id)
        with scenarios_tab:
            render_scenarios(state.settings.database_path, selected_id)
        with orders_tab:
            render_orders(state.settings.database_path, selected_id)

    st.divider()
    st.caption("Проекты, локальное утверждение и проверяемый экспорт доступны; отправки поставщику нет.")
