"""Inspect source agreement and parameter readiness before procurement calculations."""

import json
from pathlib import Path
import sqlite3

import streamlit as st

from hackalem.services.quality import (
    configuration_template,
    get_configuration,
    list_configurations,
    list_quality_runs,
    quality_report,
    run_quality,
    save_configuration,
)


_SERVICE_ERRORS = (OSError, ValueError, sqlite3.Error, RuntimeError)
_REPORT_LIMIT = 100
_SCENARIO_EXAMPLE = {
    "as_of": "2026-09-22",
    "defaults": {},
    "skus": {
        "ПРИМЕР_SKU": {
            "lead_time_days": {
                "value": 14,
                "status": "scenario",
                "reason": "Учебное допущение: срок ещё не подтверждён поставщиком",
                "author": "Автор сценария",
            }
        }
    },
    "sales_choices": [
        {
            "sku": "ПРИМЕР_SKU",
            "start": "2025-01-01",
            "end": "2026-08-01",
            "source": "monthly_sales",
            "scope": "source_report",
            "status": "scenario",
            "reason": "Учебный выбор источника для проверки сценария",
            "author": "Автор сценария",
        }
    ],
}


def _display_rows(rows: list[dict]) -> list[dict]:
    """Keep nested source references readable in a flat review table."""
    return [
        {
            key: json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
            for key, value in row.items()
        }
        for row in rows
    ]


def _configuration_editor(database_path: Path, snapshot_id: int, scope: str) -> None:
    with st.expander("Задать параметры и источник продаж"):
        st.caption(
            "Вставьте JSON с параметрами для этого снимка. "
            "Для каждого значения укажите статус confirmed или scenario, основание и автора. "
            "Сохранение создаёт отдельную версию настройки."
        )
        st.markdown(
            "Параметры товара: `current_stock`, `reserved_stock`, `stock_date`, "
            "`lead_time_days`, `review_period_days`, `category_code`, `category_label`, "
            "`stock_policy`, `minimum_order`, `order_multiple`, `accounting_unit`, "
            "`purchase_unit`, `unit_factor`, `business_growth`. "
            "Дополнительно: `blank_sales_policy`, `eta_confirmations`, `incoming_unit` и `no_open_orders`."
        )
        st.caption(
            "defaults применяются ко всем товарам, skus — к указанным кодам. "
            "В stock_policy задаются mode (stock, on_demand или exclude) и safety_days. "
            "sales_choices сохраняет выбор источника для конкретного периода; источники не складываются."
        )
        st.caption(
            "Если ожидаемых поставок нет, это задаётся явно через no_open_orders "
            "со значением true, статусом, автором и основанием. "
            "Для существующих поступлений укажите incoming_unit и eta_confirmations по каждой строке."
        )
        with st.form(f"{scope}_configuration_form"):
            source = st.text_area(
                "Настройка в формате JSON",
                value=json.dumps(configuration_template(), ensure_ascii=False, indent=2),
                height=250,
                key=f"{scope}_configuration_text",
            )
            submitted = st.form_submit_button("Сохранить настройку")
        if submitted:
            try:
                payload = json.loads(source)
                if not isinstance(payload, dict):
                    raise ValueError("Настройка должна быть объектом JSON.")
                saved = save_configuration(database_path, snapshot_id, payload)
            except _SERVICE_ERRORS as error:
                st.error(f"Не удалось сохранить настройку: {error}")
            else:
                st.session_state[f"{scope}_configuration_id"] = saved["id"]
                st.success(f"Настройка № {saved['id']} сохранена для снимка № {snapshot_id}.")

        st.markdown("**Пример сценарного допущения**")
        st.caption(
            "Пример ниже не применяется автоматически. Замените код, даты, значения, "
            "автора и основания своими данными перед сохранением."
        )
        st.code(json.dumps(_SCENARIO_EXAMPLE, ensure_ascii=False, indent=2), language="json")


def _render_quality_report(report: dict, scope: str, database_path: Path) -> None:
    st.caption(
        f"Проверка № {report['run_id']} · Снимок № {report['snapshot_id']} · "
        f"{report['supplier']} · Настройка: "
        f"{report['configuration_id'] if report['configuration_id'] is not None else 'без дополнительных параметров'}"
    )
    summary = report["summary"]
    columns = st.columns(2)
    columns[0].metric("Товаров в проверке", summary["sku_count"])
    columns[1].metric("Замечаний", summary["issue_count"])
    st.dataframe(
        [{"Статус данных": status, "Товаров": count} for status, count in summary["status_counts"].items()],
        hide_index=True,
        width="stretch",
    )
    with st.expander("Покрытие источниками и сводка расхождений"):
        st.json(summary["coverage"])
        st.dataframe(
            [{"Вид сравнения": kind, "Количество": count} for kind, count in summary["comparison_counts"].items()],
            hide_index=True,
            width="stretch",
        )

    st.markdown("**Готовность данных по товарам**")
    st.caption(
        f"В каждом списке показано не более {_REPORT_LIMIT} строк. "
        "Для конкретного товара используйте фильтр по коду."
    )
    if report["skus"]:
        st.dataframe(
            [
                {
                    "Код": row["sku"],
                    "Товар": row.get("name"),
                    "Статус": row["status"],
                    "Входы допускают расчёт": row["eligible_for_calculation"],
                    "Входы допускают подтверждённый заказ": row["eligible_for_confirmed_order"],
                    "Причины": "\n".join(row["reasons"]),
                    "Сценарные допущения": "\n".join(row["assumptions"]),
                }
                for row in report["skus"]
            ],
            hide_index=True,
            width="stretch",
        )
        by_sku = {row["sku"]: row for row in report["skus"]}
        selected_key = f"{scope}_detail_sku"
        if st.session_state.get(selected_key) not in by_sku:
            st.session_state.pop(selected_key, None)
        selected_sku = st.selectbox(
            "Товар для подробного просмотра",
            list(by_sku),
            index=None,
            placeholder="Выберите код из показанных строк",
            key=selected_key,
        )
        if selected_sku is not None:
            row = by_sku[selected_sku]
            with st.expander("Причины статуса и параметры товара", expanded=True):
                st.write(f"{row['sku']} · {row['status']}")
                st.markdown("**Причины**")
                if row["reasons"]:
                    for reason in row["reasons"]:
                        st.write(reason)
                else:
                    st.write("Блокирующих причин в этой проверке нет.")
                st.markdown("**Сценарные допущения**")
                if row["assumptions"]:
                    for assumption in row["assumptions"]:
                        st.write(assumption)
                else:
                    st.write("Сценарные допущения не зарегистрированы.")
                st.markdown("**Параметры и основания**")
                st.json(row["parameters"])
                st.markdown("**Выбранные продажи и происхождение**")
                detailed = quality_report(database_path, report["run_id"], sku=selected_sku, limit=1)
                st.dataframe(_display_rows(detailed["selected_sales"]), hide_index=True, width="stretch")
                st.markdown("**Подтверждения ожидаемых поставок**")
                st.json(row["incoming"])
    else:
        st.info("По выбранному фильтру товары не найдены.")

    st.markdown("**Расхождения источников**")
    if report["comparisons"]:
        st.dataframe(_display_rows(report["comparisons"]), hide_index=True, width="stretch")
    else:
        st.write("В показанной выборке нет записей сравнения.")
    st.markdown("**Замечания к данным**")
    if report["issues"]:
        st.dataframe(_display_rows(report["issues"]), hide_index=True, width="stretch")
    else:
        st.write("В показанной выборке нет замечаний.")


def render_quality_panel(database_path: Path, snapshot_id: int) -> None:
    """Render a separate quality workflow for the explicitly selected snapshot."""
    scope = f"quality_{snapshot_id}"
    st.subheader("Согласование источников и готовность данных")
    st.caption(
        "Статусы описывают полноту и согласованность входных данных. "
        "Прогноз и количество к заказу на этом этапе не рассчитываются."
    )
    _configuration_editor(database_path, snapshot_id, scope)
    try:
        configurations = list_configurations(database_path, snapshot_id)
    except _SERVICE_ERRORS as error:
        st.error(f"Не удалось прочитать настройки: {error}")
        return
    by_id = {record["id"]: record for record in configurations}
    config_key = f"{scope}_configuration_id"
    if st.session_state.get(config_key) not in by_id:
        st.session_state.pop(config_key, None)
    configuration_id = st.selectbox(
        "Настройка для проверки",
        list(by_id),
        index=None,
        placeholder="Без дополнительных параметров",
        key=config_key,
        format_func=lambda identifier: f"№ {identifier} · {by_id[identifier]['created_at_utc']} (UTC)",
    )
    if configuration_id is not None:
        try:
            configuration = get_configuration(database_path, configuration_id)
        except _SERVICE_ERRORS as error:
            st.error(f"Не удалось прочитать выбранную настройку: {error}")
            return
        with st.expander("Содержимое выбранной настройки"):
            st.json(configuration["payload"])

    if st.button("Проверить данные", key=f"{scope}_run", type="primary"):
        try:
            with st.spinner("Сверяем источники и проверяем параметры…"):
                result = run_quality(database_path, snapshot_id, configuration_id)
        except _SERVICE_ERRORS as error:
            st.error(f"Не удалось выполнить проверку: {error}")
        else:
            st.session_state[f"{scope}_run_id"] = result["run_id"]
            st.success(f"Проверка № {result['run_id']} сохранена.")

    try:
        runs = list_quality_runs(database_path, snapshot_id)
    except _SERVICE_ERRORS as error:
        st.error(f"Не удалось прочитать список проверок: {error}")
        return
    if not runs:
        st.info("Для этого снимка проверок пока нет.")
        return
    runs_by_id = {record["id"]: record for record in runs}
    run_key = f"{scope}_run_id"
    if st.session_state.get(run_key) not in runs_by_id:
        st.session_state.pop(run_key, None)
    run_id = st.selectbox(
        "Сохранённая проверка",
        list(runs_by_id),
        index=None,
        placeholder="Выберите проверку",
        key=run_key,
        format_func=lambda identifier: f"№ {identifier} · {runs_by_id[identifier]['created_at_utc']} (UTC)",
    )
    if run_id is None:
        st.info("Выберите сохранённую проверку для просмотра результатов.")
        return
    sku = st.text_input(
        "Код товара для фильтра проверки",
        placeholder="Оставьте пустым для общей выборки",
        key=f"{scope}_sku_filter",
    ).strip()
    try:
        report = quality_report(database_path, run_id, sku=sku or None, limit=_REPORT_LIMIT)
    except _SERVICE_ERRORS as error:
        st.error(f"Не удалось прочитать результат проверки: {error}")
        return
    _render_quality_report(report, f"{scope}_{run_id}", database_path)
