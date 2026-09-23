"""Purchaser-facing views over immutable stage 7-9 runs."""

import copy
import sqlite3
from datetime import date
from pathlib import Path

import streamlit as st

from hackalem.services.cleaning import cleaning_report, list_cleaning_runs
from hackalem.services.forecasting import forecast_report, list_forecast_runs
from hackalem.services.lost_demand import lost_demand_report
from hackalem.services.orders import (
    approve_order, build_order_export, create_order_project, create_order_revision,
    list_order_versions, order_report, submit_order_for_review, update_order_item,
)
from hackalem.services.quality import list_quality_runs
from hackalem.services.replenishment import (
    list_replenishment_runs, replenishment_report, run_replenishment,
)

_ERRORS = (ValueError, OSError, sqlite3.Error, RuntimeError)


@st.cache_data(show_spinner=False)
def _replenishment(database_path: str, run_id: int):
    return replenishment_report(Path(database_path), run_id)


@st.cache_data(show_spinner=False)
def _cleaning(database_path: str, run_id: int, sku: str):
    return cleaning_report(Path(database_path), run_id, sku=sku, limit=10000)


@st.cache_data(show_spinner=False)
def _forecast(database_path: str, run_id: int):
    return forecast_report(Path(database_path), run_id)


@st.cache_data(show_spinner=False)
def _lost_demand(database_path: str, run_id: int):
    return lost_demand_report(Path(database_path), run_id, limit=10000)


def _run_key(snapshot_id):
    return f"procurement_{snapshot_id}_run_id"


def _clear_run(snapshot_id):
    st.session_state.pop(_run_key(snapshot_id), None)


def _run_selector(database_path, snapshot_id, label="Версия расчёта", *, widget_key=None):
    runs = list_replenishment_runs(database_path, snapshot_id)
    if not runs:
        return None, []
    indexed = {row["id"]: row for row in runs}
    key = widget_key or _run_key(snapshot_id)
    if st.session_state.get(key) not in indexed:
        st.session_state.pop(key, None)
    run_id = st.selectbox(
        label, list(indexed), index=None, placeholder="Выберите сохранённый расчёт",
        key=key, format_func=lambda value: (
            f"№ {value} · {indexed[value]['as_of']} · "
            f"{indexed[value]['summary']['items']} позиций"
        ),
    )
    return run_id, runs


def _recommendation_row(item):
    if item["status"] == "blocked":
        return {
            "Товар": item["sku"], "Наименование": item.get("name") or "",
            "Категория": item.get("category_code") or "", "Единица": "",
            "Доступный остаток": None, "Путь": None, "Ближайшее поступление": None,
            "Прогноз L+R": None, "Страховой запас": None, "К заказу": None,
            "Единица закупки": "", "Срочность": "Заблокировано",
            "Статус": "blocked", "Причина": "; ".join(item.get("reasons", [])),
        }
    explanation = item["explanation"]
    arrivals = explanation["arrivals"]
    return {
        "Товар": item["sku"], "Наименование": item.get("name") or "",
        "Категория": item.get("category_code") or "",
        "Единица": explanation["accounting_unit"],
        "Доступный остаток": explanation["available_stock"],
        "Путь": explanation["timely_incoming"],
        "Ближайшее поступление": min((row["eta"] for row in arrivals), default=None),
        "Прогноз L+R": explanation["regular_forecast"],
        "Страховой запас": explanation["safety_stock"],
        "К заказу": item["order_quantity"],
        "Единица закупки": explanation["purchase_unit"],
        "Срочность": "Срочно" if item["urgent_problem"] else "Обычный срок",
        "Статус": item["status"], "Причина": explanation["text"],
    }


def _create_calculation(database_path, snapshot_id, supplier, warehouse, calculation_date):
    st.markdown("**Новый расчёт из сохранённых версий**")
    quality = list_quality_runs(database_path, snapshot_id)
    cleaning = list_cleaning_runs(database_path, snapshot_id)
    if not quality or not cleaning:
        st.info("Сначала сохраните проверку качества и подготовку регулярного спроса в разделе «Данные и проверки».")
        return
    columns = st.columns(2)
    quality_id = columns[0].selectbox(
        "Проверка качества", [row["id"] for row in quality], index=None,
        key=f"procurement_{snapshot_id}_quality", on_change=_clear_run, args=(snapshot_id,),
    )
    cleaning_id = columns[1].selectbox(
        "Подготовка спроса", [row["id"] for row in cleaning], index=None,
        key=f"procurement_{snapshot_id}_cleaning", on_change=_clear_run, args=(snapshot_id,),
    )
    forecasts = list_forecast_runs(database_path, snapshot_id)
    compatible = []
    if quality_id is not None and cleaning_id is not None:
        for row in forecasts:
            report = _forecast(str(database_path), row["id"])
            if report["quality_run_id"] == quality_id and report["cleaning_run_id"] == cleaning_id:
                compatible.append(report)
    by_id = {row["run_id"]: row for row in compatible}
    selected = st.multiselect(
        "Прогнозы товаров", list(by_id), key=f"procurement_{snapshot_id}_forecasts",
        format_func=lambda value: f"№ {value} · {by_id[value]['sku']} · {by_id[value]['status']}",
        on_change=_clear_run, args=(snapshot_id,),
    )
    if quality_id is not None and cleaning_id is not None and not compatible:
        st.info("Для выбранных версий нет совместимых прогнозов. Создайте их в разделе «Данные и проверки».")
    if st.button("Рассчитать рекомендации", type="primary",
                 disabled=not selected, key=f"procurement_{snapshot_id}_calculate"):
        payload = {
            "quality_run_id": quality_id, "cleaning_run_id": cleaning_id,
            "as_of": calculation_date.isoformat(), "supplier": supplier,
            "warehouse": warehouse,
            "items": [{"sku": by_id[run_id]["sku"],
                       "category_code": by_id[run_id]["summary"]["category"]["code"],
                       "forecast_run_id": run_id, "project_commitments": []}
                      for run_id in selected],
        }
        try:
            result = run_replenishment(database_path, payload)
        except _ERRORS as error:
            st.error(f"Рекомендации не сохранены: {error}")
        else:
            _replenishment.clear()
            st.session_state[_run_key(snapshot_id)] = result["run_id"]
            st.success(f"Расчёт № {result['run_id']} сохранён. Предыдущие версии не изменены.")


def render_recommendations(database_path: Path, snapshot_id: int, supplier: str,
                           warehouse: str, calculation_date: date) -> None:
    st.subheader("Рекомендации")
    st.caption("Каждая строка относится к выбранному dataset и неизменяемому run_id. Количества разных единиц не суммируются.")
    with st.expander("Создать расчёт", expanded=not list_replenishment_runs(database_path, snapshot_id)):
        _create_calculation(database_path, snapshot_id, supplier, warehouse, calculation_date)
    try:
        run_id, _ = _run_selector(database_path, snapshot_id)
    except _ERRORS as error:
        st.error(f"Версии расчёта недоступны: {error}")
        return
    if run_id is None:
        st.info("Сохранённых рекомендаций пока нет. Выберите совместимые версии и выполните расчёт.")
        return
    report = _replenishment(str(database_path), run_id)
    rows = [_recommendation_row(item) for item in report["items"]]
    if report["input"]["dataset"]["kind"] == "synthetic":
        st.warning("СИНТЕТИЧЕСКИЕ РЕКОМЕНДАЦИИ — не использовать для реального заказа.")
    if any(item.get("scenario") for item in report["items"]):
        st.warning("СЦЕНАРНЫЙ РАСЧЁТ — допущения требуют проверки ответственным сотрудником.")
    controls = st.columns(5)
    search = controls[0].text_input("Поиск товара", key=f"recommendation_{run_id}_search").casefold()
    categories = sorted({row["Категория"] for row in rows if row["Категория"]})
    category = controls[1].selectbox("Категория", ["Все", *categories], key=f"recommendation_{run_id}_category")
    statuses = sorted({row["Статус"] for row in rows})
    status = controls[2].selectbox("Полнота", ["Все", *statuses], key=f"recommendation_{run_id}_status")
    urgency = controls[3].selectbox("Срочность", ["Все", "Срочно", "Обычный срок", "Заблокировано"],
                                    key=f"recommendation_{run_id}_urgency")
    sort_by = controls[4].selectbox("Сортировка", ["Товар", "К заказу", "Прогноз L+R", "Доступный остаток"],
                                    key=f"recommendation_{run_id}_sort")
    filtered = [row for row in rows
                if (not search or search in (row["Товар"] + " " + row["Наименование"]).casefold())
                and (category == "Все" or row["Категория"] == category)
                and (status == "Все" or row["Статус"] == status)
                and (urgency == "Все" or row["Срочность"] == urgency)]
    filtered.sort(key=lambda row: (row[sort_by] is None, row[sort_by]))
    metrics = st.columns(3)
    metrics[0].metric("Позиций", len(filtered))
    metrics[1].metric("Срочных", sum(row["Срочность"] == "Срочно" for row in filtered))
    metrics[2].metric("Заблокировано", sum(row["Статус"] == "blocked" for row in filtered))
    if filtered:
        st.dataframe(filtered, hide_index=True, width="stretch")
        unit_totals = {}
        for row in filtered:
            if row["К заказу"] is not None:
                unit_totals[row["Единица закупки"]] = unit_totals.get(row["Единица закупки"], 0) + row["К заказу"]
        if unit_totals:
            st.dataframe([{"Единица закупки": unit, "Количество": quantity}
                          for unit, quantity in sorted(unit_totals.items())], hide_index=True)
        st.caption("Стоимость не показана: подтверждённая актуальная цена и валюта не входят в расчёт.")
    else:
        st.info("По выбранным фильтрам рекомендаций нет.")


def render_product_card(database_path: Path, snapshot_id: int) -> None:
    st.subheader("Карточка товара")
    try:
        run_id, _ = _run_selector(
            database_path, snapshot_id, "Версия расчёта для карточки",
            widget_key=f"card_{snapshot_id}_run_id",
        )
    except _ERRORS as error:
        st.error(str(error))
        return
    if run_id is None:
        st.info("Выберите сохранённую версию расчёта, затем товар.")
        return
    report = _replenishment(str(database_path), run_id)
    items = {row["sku"]: row for row in report["items"]}
    sku = st.selectbox("Товар", list(items), index=None, key=f"card_{run_id}_sku")
    if sku is None:
        st.info("Выберите товар для просмотра истории и объяснения.")
        return
    item = items[sku]
    if item["status"] == "blocked":
        st.error("Расчёт товара заблокирован: " + "; ".join(item["reasons"]))
        return
    explanation = item["explanation"]
    if item.get("scenario"):
        st.warning("СЦЕНАРНЫЙ РЕЗУЛЬТАТ")
    metrics = st.columns(4)
    metrics[0].metric("Доступно", f"{explanation['available_stock']:g} {explanation['accounting_unit']}")
    metrics[1].metric("Прогноз L+R", f"{explanation['regular_forecast']:g} {explanation['accounting_unit']}")
    metrics[2].metric("Страховой запас", f"{explanation['safety_stock']:g} {explanation['accounting_unit']}")
    quantity = item["order_quantity"]
    metrics[3].metric("К заказу", "Не рассчитано" if quantity is None else
                      f"{quantity:g} {explanation['purchase_unit']}")
    sources = explanation["sources"]
    clean = _cleaning(str(database_path), sources["cleaning_run_id"], sku)
    months = [{"Месяц": row["period"], "Фактические продажи": row["raw_signed_quantity"],
               "Очищенный регулярный спрос": row["regular_quantity"]} for row in clean["months"]]
    st.markdown("**Фактические и очищенные продажи**")
    st.line_chart(months, x="Месяц", y=["Фактические продажи", "Очищенный регулярный спрос"])
    anomalies = [row for row in clean["documents"] if row["status"] not in ("regular", "regular_manual")]
    with st.expander(f"Аномалии и решения ({len(anomalies)})"):
        if anomalies:
            st.dataframe(anomalies, hide_index=True, width="stretch")
        else:
            st.write("Отдельных аномалий не зарегистрировано.")
    lost_id = sources.get("lost_demand_run_id")
    with st.expander("Подтверждённое наличие и упущенный спрос"):
        if lost_id:
            lost = _lost_demand(str(database_path), lost_id)
            st.dataframe(lost["days"], hide_index=True, width="stretch")
        else:
            st.info("Совместимая версия оценки наличия не использовалась.")
    forecast_id = sources.get("forecast_run_id")
    st.markdown("**Прогноз и будущий остаток**")
    if forecast_id:
        forecast = _forecast(str(database_path), forecast_id)
        st.line_chart(forecast["summary"]["forecasts"], x="period", y="prediction")
    else:
        st.caption("Использован ручной сценарный прогноз.")
    st.line_chart(item["calendar"], x="date",
                  y=["balance_without_new_order", "balance_with_new_order"])
    st.markdown("**Пошаговое объяснение количества**")
    st.code(explanation["formula"], language=None)
    st.dataframe([
        {"Компонент": "Регулярный прогноз", "Значение": explanation["regular_forecast"], "Единица": explanation["accounting_unit"]},
        {"Компонент": "Проектные обязательства", "Значение": explanation["unreserved_project_due"], "Единица": explanation["accounting_unit"]},
        {"Компонент": "Страховой запас", "Значение": explanation["safety_stock"], "Единица": explanation["accounting_unit"]},
        {"Компонент": "Доступный остаток", "Значение": -explanation["available_stock"], "Единица": explanation["accounting_unit"]},
        {"Компонент": "Своевременные поступления", "Значение": -explanation["timely_incoming"], "Единица": explanation["accounting_unit"]},
        {"Компонент": "Потребность до округления", "Значение": explanation["raw_need_accounting"], "Единица": explanation["accounting_unit"]},
        {"Компонент": "Итог после перевода и округления", "Значение": explanation["rounded_order_purchase"], "Единица": explanation["purchase_unit"]},
    ], hide_index=True, width="stretch")
    st.write(explanation["text"])
    with st.expander("Источники, поступления и допущения"):
        st.json({"sources": sources, "arrivals": explanation["arrivals"],
                 "excluded_arrivals": explanation["excluded_arrivals"],
                 "risk": {"level": explanation["risk_level"],
                          "date": explanation["first_risk_date"],
                          "precision": explanation["risk_date_precision"]},
                 "assumptions": explanation["assumptions"]})


def render_scenarios(database_path: Path, snapshot_id: int) -> None:
    st.subheader("Сценарии")
    runs = list_replenishment_runs(database_path, snapshot_id)
    if not runs:
        st.info("Сначала сохраните базовый расчёт в разделе «Рекомендации».")
        return
    indexed = {row["id"]: row for row in runs}
    base_id = st.selectbox("Базовый расчёт", list(indexed), index=None,
                           key=f"scenario_{snapshot_id}_base")
    demand_change = st.number_input("Изменение спроса, %", min_value=-100.0, max_value=900.0,
                                    value=0.0, step=5.0, key=f"scenario_{snapshot_id}_demand")
    delay = st.number_input("Задержка всех ожидаемых поступлений, дней", min_value=0,
                            max_value=365, value=0, step=1, key=f"scenario_{snapshot_id}_delay")
    author = st.text_input("Автор сценария", key=f"scenario_{snapshot_id}_author")
    reason = st.text_input("Основание сценария", key=f"scenario_{snapshot_id}_reason")
    if st.button("Создать новый сценарный расчёт",
                 disabled=base_id is None or not author.strip() or not reason.strip(),
                 key=f"scenario_{snapshot_id}_run"):
        try:
            base = _replenishment(str(database_path), base_id)
            payload = copy.deepcopy(base["input"]["payload"])
            payload["scenario"] = {
                "base_run_id": base_id, "demand_factor": 1 + demand_change / 100,
                "arrival_delay_days": int(delay), "author": author, "reason": reason,
            }
            result = run_replenishment(database_path, payload)
        except _ERRORS as error:
            st.error(f"Сценарий не сохранён: {error}")
        else:
            _replenishment.clear()
            st.success(f"Создан новый расчёт № {result['run_id']}; базовый № {base_id} сохранён без изменений.")


def render_orders(database_path: Path, snapshot_id: int) -> None:
    st.subheader("Заказы")
    st.caption("Проект относится к одному поставщику и сохранённому расчёту. Имя ответственного — локальная запись, а не промышленная аутентификация.")
    flash = st.session_state.pop(f"order_{snapshot_id}_flash", None)
    if flash:
        st.success(flash)
    pending = st.session_state.pop(f"order_{snapshot_id}_pending_version", None)
    selector_key = f"order_{snapshot_id}_version"
    runs = list_replenishment_runs(database_path, snapshot_id)
    with st.expander("Создать проект из рекомендации", expanded=not list_order_versions(database_path, snapshot_id)):
        run_index = {row["id"]: row for row in runs}
        source_run = st.selectbox(
            "Расчёт пополнения", list(run_index), index=None,
            key=f"order_{snapshot_id}_source_run",
            format_func=lambda value: f"№ {value} · {run_index[value]['as_of']} · {run_index[value]['summary']['items']} позиций",
        ) if run_index else None
        creator = st.text_input("Автор проекта", key=f"order_{snapshot_id}_creator")
        if st.button("Создать проект заказа", disabled=source_run is None or not creator.strip(),
                     key=f"order_{snapshot_id}_create"):
            try:
                created = create_order_project(database_path, source_run, creator)
            except _ERRORS as error:
                st.error(f"Проект не создан: {error}")
            else:
                pending = created["version_id"]
                st.success(f"Проект № {created['project_id']}, версия {created['version_number']} сохранён.")
    versions = list_order_versions(database_path, snapshot_id)
    if not versions:
        st.info("Сначала сохраните расчёт в разделе «Рекомендации», затем создайте проект заказа.")
        return
    indexed = {row["version_id"]: row for row in versions}
    if pending in indexed:
        st.session_state[selector_key] = pending
    if st.session_state.get(selector_key) not in indexed:
        st.session_state.pop(selector_key, None)
    version_id = st.selectbox(
        "Версия проекта", list(indexed), index=None, key=selector_key,
        placeholder="Выберите проект и версию",
        format_func=lambda value: (
            f"Проект {indexed[value]['project_id']} · v{indexed[value]['version_number']} · "
            f"{indexed[value]['supplier']} · {indexed[value]['status_label']}"
        ),
    )
    if version_id is None:
        st.info("Выберите версию проекта заказа.")
        return
    try:
        report = order_report(database_path, version_id)
    except _ERRORS as error:
        st.error(f"Версия заказа недоступна: {error}")
        return
    metrics = st.columns(4)
    metrics[0].metric("Проект", report["project_id"])
    metrics[1].metric("Версия", report["version_number"])
    metrics[2].metric("Статус", report["status_label"])
    metrics[3].metric("Строк к заказу", sum(item["selected_quantity"] > 0 for item in report["items"]))
    if report["dataset"]["kind"] == "synthetic":
        st.warning("СИНТЕТИЧЕСКИЙ ПРОЕКТ — не является реальным заказом поставщику.")
    elif any(item["source"].get("scenario") for item in report["items"]):
        st.warning("СЦЕНАРНЫЙ ПРОЕКТ — его нельзя утверждать как реальный заказ.")
    search = st.text_input("Фильтр строк", key=f"order_{version_id}_search").casefold()
    rows = [{
        "Строка": item["line_id"], "Артикул": item["article"] or "",
        "Код1С": item["code_1c"], "Наименование": item["name"] or "",
        "Предложено": item["suggested_quantity"], "Выбрано менеджером": item["selected_quantity"],
        "Единица": item["purchase_unit"] or "", "Причина корректировки": item["correction_reason"] or "",
    } for item in report["items"]]
    filtered = [row for row in rows if not search or search in
                (row["Строка"] + " " + row["Артикул"] + " " + row["Наименование"]).casefold()]
    st.dataframe(filtered, hide_index=True, width="stretch")
    if not filtered:
        st.info("По фильтру строк нет. Сохранённые корректировки не изменены.")
    else:
        by_sku = {item["sku"]: item for item in report["items"] if item["sku"] in {row["Строка"] for row in filtered}}
        sku = st.selectbox("Строка для корректировки", list(by_sku),
                           key=f"order_{version_id}_edit_sku")
        current = by_sku[sku]
        quantity = st.number_input(
            "Выбранное количество", min_value=0.0, value=float(current["selected_quantity"]),
            key=f"order_{version_id}_{sku}_quantity",
        )
        columns = st.columns(2)
        actor = columns[0].text_input("Кто изменяет", key=f"order_{version_id}_{sku}_actor")
        reason = columns[1].text_input("Причина изменения", key=f"order_{version_id}_{sku}_reason")
        changed = float(quantity) != float(current["selected_quantity"])
        label = ("Создать новую версию и сохранить" if report["status"] == "approved"
                 else "Сохранить количество менеджера")
        if st.button(label, disabled=not changed or not actor.strip() or not reason.strip(),
                     key=f"order_{version_id}_{sku}_save"):
            try:
                updated = update_order_item(database_path, version_id, sku, quantity, actor, reason)
            except _ERRORS as error:
                st.error(f"Корректировка не сохранена: {error}")
            else:
                st.session_state[f"order_{snapshot_id}_pending_version"] = updated["version_id"]
                st.session_state[f"order_{snapshot_id}_flash"] = (
                    f"Количество {sku} сохранено в версии {updated['version_number']}."
                )
                st.rerun()

    st.markdown("**Проверка и утверждение**")
    if report["status"] == "draft":
        columns = st.columns(2)
        actor = columns[0].text_input("Кто передаёт", key=f"order_{version_id}_submit_actor")
        reason = columns[1].text_input("Основание передачи", key=f"order_{version_id}_submit_reason")
        if st.button("Передать на проверку", disabled=not actor.strip() or not reason.strip(),
                     key=f"order_{version_id}_submit"):
            try:
                submit_order_for_review(database_path, version_id, actor, reason)
            except _ERRORS as error:
                st.error(f"Статус не изменён: {error}")
            else:
                st.session_state[f"order_{snapshot_id}_pending_version"] = version_id
                st.session_state[f"order_{snapshot_id}_flash"] = "Проект передан на проверку."
                st.rerun()
    elif report["status"] == "review":
        columns = st.columns(2)
        responsible = columns[0].text_input("Ответственный", key=f"order_{version_id}_responsible")
        note = columns[1].text_input("Основание утверждения", key=f"order_{version_id}_approval_note")
        acknowledged = st.checkbox(
            "Понимаю: это локальная фиксация имени, а не проверка личности.",
            key=f"order_{version_id}_local_identity",
        )
        if st.button("Утвердить неизменяемую версию",
                     disabled=not responsible.strip() or not note.strip() or not acknowledged,
                     key=f"order_{version_id}_approve"):
            try:
                approve_order(database_path, version_id, responsible, note)
            except _ERRORS as error:
                st.error(f"Утверждение не выполнено: {error}")
            else:
                st.session_state[f"order_{snapshot_id}_pending_version"] = version_id
                st.session_state[f"order_{snapshot_id}_flash"] = "Неизменяемый снимок версии утверждён."
                st.rerun()
    else:
        st.success(f"Версия утверждена {report['approved_at_utc']} ответственным «{report['responsible']}» и неизменяема.")
        with st.expander("Создать новый черновик без изменения утверждённой версии"):
            actor = st.text_input("Автор новой версии", key=f"order_{version_id}_revision_actor")
            reason = st.text_input("Причина новой версии", key=f"order_{version_id}_revision_reason")
            if st.button("Создать новую версию", disabled=not actor.strip() or not reason.strip(),
                         key=f"order_{version_id}_revision"):
                try:
                    revision = create_order_revision(database_path, version_id, actor, reason)
                except _ERRORS as error:
                    st.error(f"Новая версия не создана: {error}")
                else:
                    st.session_state[f"order_{snapshot_id}_pending_version"] = revision["version_id"]
                    st.session_state[f"order_{snapshot_id}_flash"] = f"Создан черновик версии {revision['version_number']}."
                    st.rerun()

    st.markdown("**Экспорт выбранной версии**")
    restricted = (report["status"] != "approved" or report["dataset"]["kind"] != "real" or
                  any(item["source"].get("scenario") for item in report["items"]))
    allowed = True
    if restricted:
        st.warning("Файл будет явно помечен как черновой, сценарный или синтетический и не является утверждённым реальным заказом.")
        allowed = st.checkbox("Выгрузить с этой маркировкой", key=f"order_{version_id}_export_ack")
    if allowed:
        try:
            csv_export = build_order_export(database_path, version_id, "csv")
            xlsx_export = build_order_export(database_path, version_id, "xlsx")
        except _ERRORS as error:
            st.error(f"Экспорт недоступен: {error}")
        else:
            st.caption(f"Маркировка: {csv_export['metadata']['classification']}. Обе версии повторно прочитаны и сверены.")
            columns = st.columns(2)
            columns[0].download_button("Скачать CSV", csv_export["content"], csv_export["filename"],
                                       csv_export["media_type"], key=f"order_{version_id}_csv")
            columns[1].download_button("Скачать Excel", xlsx_export["content"], xlsx_export["filename"],
                                       xlsx_export["media_type"], key=f"order_{version_id}_xlsx")
    with st.expander("Журнал изменений"):
        st.dataframe(report["events"], hide_index=True, width="stretch")
    st.caption("Формат можно настроить под согласованный шаблон 1С после его получения и проверки. Готовая интеграция с 1С и отправка поставщику не заявляются.")
