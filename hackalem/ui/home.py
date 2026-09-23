"""Systeme Electric import status and source lineage, without procurement calculations."""

from pathlib import Path
import sqlite3

import streamlit as st

from hackalem.config import load_settings
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


def render_home() -> None:
    st.set_page_config(page_title="Помощник закупщика", page_icon="📦", layout="wide")
    st.title("Помощник закупщика")
    st.caption("Электрокомплект · Локальный кабинет закупок")

    try:
        state = bootstrap(load_settings())
        dataset = dataset_context(state.settings.database_path)
    except (OSError, ValueError, sqlite3.Error, UnsupportedSchemaError) as error:
        st.error(f"Не удалось подготовить локальное хранилище: {error}")
        st.info("Проверьте пути и доступ к папкам. Исходные отчёты не изменяются.")
        st.stop()

    with st.sidebar:
        st.markdown("**Рабочее пространство**")
        st.write("Локальное хранилище подключено")
        st.caption("Проверочный набор: этап 5 из 12")
        with st.expander("Расположение файлов"):
            st.write("Исходные отчёты")
            st.code(str(state.settings.source_dir), language=None)
            st.write("Локальное хранилище")
            st.code(str(state.storage.path), language=None)

    if not state.source_directory_exists:
        st.warning(
            "Папка исходных отчётов пока недоступна. "
            "Укажите существующую папку в HACKALEM_SOURCE_DIR перед этапом импорта."
        )

    if dataset['kind'] == 'synthetic':
        st.warning('СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР — вымышленные товары, клиенты и условия. Для реальных закупок не применяется.')
        st.caption(f"Dataset: {dataset['dataset_id']}. Скрытый спрос и эталонные метки в интерфейс модели не загружаются.")
    st.subheader("Данные поставщиков")
    st.caption("Импорт сохраняет версии файлов, значения и их происхождение.")
    supplier = st.selectbox("Поставщик для импорта", SUPPLIERS, key="import_supplier")
    if st.button(
        f"Импортировать {supplier}",
        type="primary",
        disabled=not state.source_directory_exists or dataset['kind'] == 'synthetic',
    ):
        try:
            with st.spinner("Читаем отчёты и сохраняем снимок…"):
                imported = import_supplier(state.settings, supplier)
        except _SERVICE_ERRORS as error:
            st.error(f"Не удалось импортировать отчёты: {error}")
        else:
            st.session_state["selected_snapshot_id"] = imported["snapshot_id"]
            st.success(
                f"Снимок № {imported['snapshot_id']} сохранён. "
                f"Файлов без изменений: {imported.get('reused_files', 0)}."
            )

    try:
        snapshots = list_snapshots(state.settings.database_path)
    except _SERVICE_ERRORS as error:
        st.error(f"Не удалось прочитать список снимков: {error}")
        st.stop()

    if not snapshots:
        st.info("Данные пока не загружены.")
    else:
        snapshots_by_id = {snapshot["id"]: snapshot for snapshot in snapshots}
        if st.session_state.get("selected_snapshot_id") not in snapshots_by_id:
            st.session_state.pop("selected_snapshot_id", None)
        selected_id = st.selectbox(
            "Снимок данных",
            list(snapshots_by_id),
            index=None,
            placeholder="Выберите сохранённый снимок",
            key="selected_snapshot_id",
            format_func=lambda value: (
                f"№ {value} · {snapshots_by_id[value]['supplier']} · {snapshots_by_id[value]['created_at_utc']} (UTC)"
            ),
        )
        if selected_id is None:
            st.info("Выберите снимок, чтобы проверить импорт и происхождение значений.")
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

    st.divider()
    st.caption("Прогноз, расчёт заказов и экспорт будут добавлены на следующих этапах.")
