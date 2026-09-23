"""Explicit stage-8 forecast workflow; opening the panel never writes a run."""

import json
import sqlite3
from pathlib import Path

import streamlit as st

from hackalem.services.cleaning import list_cleaning_runs
from hackalem.services.forecasting import (
    forecast_config_template, forecast_report, list_forecast_runs, run_forecast,
)
from hackalem.services.quality import list_quality_runs
from hackalem.services.lost_demand import list_lost_demand_runs


_ERRORS = (ValueError, OSError, sqlite3.Error, RuntimeError)


def render_forecast_panel(database_path: Path, snapshot_id: int) -> None:
    st.subheader("Прогноз регулярного спроса")
    st.caption(
        "Сравниваются сезонный аналог, сглаженный уровень с ограниченным трендом "
        "и SBA для прерывистого спроса. Прогноз не является заказом."
    )
    scope = f"forecast_{snapshot_id}"
    try:
        quality = list_quality_runs(database_path, snapshot_id)
        cleaning = list_cleaning_runs(database_path, snapshot_id)
    except _ERRORS as error:
        st.error(f"Версии входов недоступны: {error}")
        return
    if not quality or not cleaning:
        st.info("Сначала сохраните проверку качества и подготовку регулярного спроса.")
        return
    quality_id = st.selectbox("Проверка качества для прогноза", [row["id"] for row in quality],
                              index=None, key=f"{scope}_quality")
    cleaning_id = st.selectbox("Подготовка спроса для прогноза", [row["id"] for row in cleaning],
                               index=None, key=f"{scope}_cleaning")
    lost_runs = list_lost_demand_runs(database_path, cleaning_id) if cleaning_id is not None else []
    lost_by_id = {row["id"]: row for row in lost_runs}
    lost_demand_run_id = st.selectbox(
        "Оценка упущенного спроса этапа 7 (необязательно)",
        [None, *lost_by_id], key=f"{scope}_lost_demand",
        format_func=lambda value: "Без поправки этапа 7" if value is None else
        f"№ {value} · {lost_by_id[value]['sku']} · {lost_by_id[value]['as_of']}",
    )
    sku = st.text_input("Код товара для прогноза", key=f"{scope}_sku").strip()
    template = forecast_config_template(cleaning[0]["as_of"])
    config_text = st.text_area("Настройка прогноза (JSON)",
                               value=json.dumps(template, ensure_ascii=False, indent=2),
                               height=280, key=f"{scope}_config")
    allow_scenario = st.checkbox("Разрешить явно сценарный прогноз",
                                 key=f"{scope}_allow_scenario")
    if st.button("Рассчитать и сохранить прогноз", key=f"{scope}_run"):
        try:
            if quality_id is None or cleaning_id is None or not sku:
                raise ValueError("Выберите обе версии входов и укажите SKU.")
            config = json.loads(config_text)
            result = run_forecast(database_path, quality_id, cleaning_id, sku, config,
                                  allow_scenario=allow_scenario,
                                  lost_demand_run_id=lost_demand_run_id)
        except (json.JSONDecodeError, ValueError, OSError, sqlite3.Error, RuntimeError) as error:
            st.error(f"Прогноз не сохранён: {error}")
        else:
            st.session_state[f"{scope}_run_id"] = result["run_id"]
            st.success(f"Прогноз № {result['run_id']} сохранён.")

    try:
        runs = list_forecast_runs(database_path, snapshot_id)
    except _ERRORS as error:
        st.error(f"Прогнозы недоступны: {error}")
        return
    if not runs:
        return
    indexed = {row["id"]: row for row in runs}
    run_id = st.selectbox("Сохранённый прогноз", list(indexed), index=None,
                          key=f"{scope}_run_id",
                          format_func=lambda value: f"№ {value} · {indexed[value]['sku']} · {indexed[value]['selected_model']}")
    if run_id is None:
        return
    try:
        report = forecast_report(database_path, run_id)
    except _ERRORS as error:
        st.error(f"Отчёт прогноза недоступен: {error}")
        return
    summary = report["summary"]
    st.write(f"{report['sku']} · {report['status']} · модель: {report['selected_model'] or 'не выбрана'}")
    if summary["metrics"]:
        st.json({key: summary["metrics"].get(key) for key in ("wape", "mae_units", "bias_units", "count")})
    st.dataframe(summary["forecasts"], hide_index=True, width="stretch")
    with st.expander("Сравнение моделей, ограничения и происхождение"):
        st.json({"model_selection": summary["model_selection"], "baselines": summary["baselines"],
                 "training_protocol": summary["training_protocol"], "limitations": summary["limitations"],
                 "growth_decision": summary["growth_decision"],
                 "lost_demand_input": summary["lost_demand_input"],
                 "seasonal_aggregate_decision": summary["seasonal_aggregate_decision"]})
