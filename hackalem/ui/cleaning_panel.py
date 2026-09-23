"""Explicit document review; opening the page never creates a preparation run."""

import json
import sqlite3
from datetime import date
from pathlib import Path

import streamlit as st

from hackalem.services.cleaning import cleaning_report, list_cleaning_runs, run_cleaning


def render_cleaning_panel(database_path: Path, snapshot_id: int) -> None:
    st.subheader("Регулярный спрос: разовые сделки и возвраты")
    st.caption("Исходные операции сохранены. Подготовка создаёт отдельную версию с причиной для каждого документа; неопределённые случаи требуют решения.")
    scope = f"cleaning_{snapshot_id}"
    as_of = st.date_input("Дата среза подготовки", value=date.today(), key=f"{scope}_as_of")
    policy = st.selectbox("Правило для уверенных одиночных покупок", ["review_only", "exclude_high_confidence"],
                          format_func=lambda value: "Отправить на проверку" if value == "review_only" else "Исключить уверенные случаи",
                          key=f"{scope}_policy")
    decisions_text = st.text_area("Ручные решения (JSON-массив, по умолчанию [])", value="[]", key=f"{scope}_decisions",
                                  help="Ключ документа, действие, автор, основание, отдельное проектное обязательство. Предыдущее решение не перезаписывается.")
    if st.button("Подготовить регулярный спрос", key=f"{scope}_run"):
        try:
            decisions = json.loads(decisions_text)
            with st.spinner("Проверяем документы и сохраняем отдельную версию…"):
                result = run_cleaning(database_path, snapshot_id, as_of.isoformat(), policy=policy, decisions=decisions)
        except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
            st.error(f"Подготовка не сохранена: {error}")
        else:
            st.session_state[f"{scope}_run_id"] = result["run_id"]
            st.success(f"Версия № {result['run_id']} сохранена.")
    try:
        runs = list_cleaning_runs(database_path, snapshot_id)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        st.error(f"Версии недоступны: {error}")
        return
    if not runs:
        st.info("Версий подготовки пока нет.")
        return
    indexed = {run["id"]: run for run in runs}
    key = f"{scope}_run_id"
    if st.session_state.get(key) not in indexed:
        st.session_state.pop(key, None)
    run_id = st.selectbox("Сохранённая версия", list(indexed), index=None,
                          placeholder="Выберите версию", key=key,
                          format_func=lambda value: f"№ {value} · {indexed[value]['as_of']} · {indexed[value]['policy']}")
    if run_id is None:
        return
    sku = st.text_input("Код товара для просмотра", key=f"{scope}_sku").strip() or None
    try:
        result = cleaning_report(database_path, run_id, sku=sku, limit=200)
    except (ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        st.error(f"Отчёт недоступен: {error}")
        return
    st.write(f"Документов: {result['documents_total']}; месяцев: {result['months_total']}; анализ клиента: {result['summary']['client_analysis']}.")
    st.json(result["summary"]["status_counts"])
    if result["documents_total"] > 200:
        st.caption("Показаны первые 200 документов; для полного списка используйте фильтр SKU или CLI.")
    for item in result["documents"]:
        if item["status"] not in ("regular", "regular_manual"):
            with st.expander(f"{item['sku']} · {item['document_key']} · {item['status']}"):
                st.json(item)
    st.markdown("**Месячный сырой и регулярный спрос**")
    st.dataframe([{key: row[key] for key in ("sku", "period", "state", "raw_signed_quantity",
                                               "regular_quantity", "removed_component", "return_quantity",
                                               "project_commitment_quantity")}
                  for row in result["months"]], hide_index=True, width="stretch")
