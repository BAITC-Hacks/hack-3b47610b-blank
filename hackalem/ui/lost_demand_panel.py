"""Small explicit stage-7 inspection panel; no implicit calculations."""

import json
import sqlite3
from pathlib import Path

import streamlit as st

from hackalem.services.cleaning import list_cleaning_runs
from hackalem.services.lost_demand import lost_demand_report, list_lost_demand_runs, run_lost_demand


def render_lost_demand_panel(database_path: Path, snapshot_id: int) -> None:
    st.subheader("Упущенный спрос при отсутствии товара")
    st.caption("Оценка использует только дни с подтверждением наличия или отсутствия на протяжении 24 часов. Реальные месячные остатки не доказывают такие дни.")
    scope = f"lost_demand_{snapshot_id}"
    try:
        cleaning = list_cleaning_runs(database_path, snapshot_id)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        st.error(f"Не удалось прочитать подготовленную историю: {error}")
        return
    by_id = {item["id"]: item for item in cleaning}
    clean_key = f"{scope}_cleaning_run"
    if st.session_state.get(clean_key) not in by_id:
        st.session_state.pop(clean_key, None)
    clean_id = st.selectbox("Версия регулярного спроса для оценки потерь", list(by_id), index=None,
                            placeholder="Сначала подготовьте регулярный спрос", key=clean_key,
                            format_func=lambda value: f"№ {value} · {by_id[value]['as_of']}")
    if clean_id is None:
        return
    sku = st.text_input("Код товара для оценки упущенного спроса", key=f"{scope}_sku").strip()
    evidence_text = st.text_area("Сценарные интервалы JSON (пусто = только имеющееся наблюдение)",
                                 value="", key=f"{scope}_scenario",
                                 help="Для реальных данных нужны интервалы полного наличия и отсутствия по всем выбранным складам, автор и причина.")
    if st.button("Оценить упущенный спрос", key=f"{scope}_run"):
        try:
            if not sku:
                raise ValueError("Укажите код товара.")
            evidence = json.loads(evidence_text) if evidence_text.strip() else None
            with st.spinner("Проверяем полнодневные наблюдения и сохраняем оценку…"):
                result = run_lost_demand(database_path, clean_id, sku, scenario_intervals=evidence)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
            st.error(f"Оценка не сохранена: {error}")
        else:
            st.session_state[f"{scope}_run_id"] = result["run_id"]
            st.success(f"Оценка № {result['run_id']} сохранена.")
    try:
        runs = list_lost_demand_runs(database_path, clean_id)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        st.error(f"Оценки недоступны: {error}")
        return
    indexed = {item["id"]: item for item in runs}
    run_key = f"{scope}_run_id"
    if st.session_state.get(run_key) not in indexed:
        st.session_state.pop(run_key, None)
    run_id = st.selectbox("Сохранённая оценка", list(indexed), index=None,
                          placeholder="Выберите оценку", key=run_key,
                          format_func=lambda value: f"№ {value} · {indexed[value]['sku']} · {indexed[value]['as_of']}")
    if run_id is None:
        return
    try:
        report = lost_demand_report(database_path, run_id, limit=100)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        st.error(f"Не удалось прочитать оценку: {error}")
        return
    source = report["summary"]["source"]
    if source == "real_without_daily_availability":
        st.warning("Точная коррекция недоступна: в реальных источниках нет полного дневного журнала наличия. Можно задать интервалы вручную как сценарий.")
    elif source == "manual_scenario":
        st.warning("СЦЕНАРИЙ: периоды наличия введены вручную; результат не подтверждает реальный stockout.")
    st.json(report["summary"])
    st.dataframe([{key: item[key] for key in ("period", "state", "observed_regular_quantity",
                                                "estimated_lost_quantity", "adjusted_training_quantity",
                                                "confirmed_stockout_days", "unknown_or_unresolved_days", "reason")}
                  for item in report["months"]], hide_index=True, width="stretch")
    with st.expander("Дневные свидетельства и метод"):
        st.json(report["days"])
        if report["days_total"] > len(report["days"]):
            st.caption(f"Показаны первые {len(report['days'])} из {report['days_total']} дней; полный список доступен через CLI.")
