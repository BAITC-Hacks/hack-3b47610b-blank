"""Stage 10: the purchaser UI uses selected immutable calculation versions."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import sqlite3

import pytest
from streamlit.testing.v1 import AppTest

from hackalem.config import PROJECT_ROOT
from hackalem.services.cleaning import run_cleaning
from hackalem.services.datasets import dataset_context
from hackalem.services.forecasting import run_forecast
from hackalem.services.orders import list_order_versions, order_report
from hackalem.services.quality import get_configuration, quality_report, run_quality, save_configuration
from hackalem.services.replenishment import list_replenishment_runs, replenishment_report, run_replenishment
from hackalem.services.synthetic import create_synthetic_dataset
from hackalem.ui import home, procurement_panel


def _forecast_configuration():
    return {
        "horizon_months": 12,
        "warehouse_scope": "source_report",
        "growth_application": {
            "mode": "replace_trend", "start": "2026-01-01", "end": "2026-12-01",
            "scope": "source_report", "status": "scenario",
            "reason": "Проверка интерфейса", "author": "pytest",
        },
        "short_history_fallback": None,
        "seasonal_aggregate_policy": {
            "use": False, "unit_status": "unknown",
            "reason": "Единица агрегата не подтверждена", "author": "pytest",
        },
    }


@pytest.fixture(scope="module")
def switchable_datasets(tmp_path_factory):
    datasets = []
    for seed in (20260923, 20260924):
        generated = create_synthetic_dataset(tmp_path_factory.mktemp("ui-switch"), seed=seed)
        database = Path(generated["database_path"])
        snapshot = next(row for row in generated["snapshots"] if row["supplier"] == "Systeme Electric")
        cleaning = run_cleaning(database, snapshot["snapshot_id"], "2026-01-01")
        forecast = run_forecast(database, snapshot["run_id"], cleaning["run_id"], "SYN-A-001",
                                _forecast_configuration(), allow_scenario=True)
        calculation = run_replenishment(database, {
            "quality_run_id": snapshot["run_id"], "cleaning_run_id": cleaning["run_id"],
            "as_of": "2026-01-01", "supplier": "Systeme Electric",
            "warehouse": "all_selected_warehouses", "items": [{
                "sku": "SYN-A-001", "category_code": "SYN-CAT-01",
                "forecast_run_id": forecast["run_id"], "project_commitments": [],
            }],
        })
        configuration = get_configuration(database, quality_report(database, snapshot["run_id"])["configuration_id"])
        payload = deepcopy(configuration["payload"])
        payload["skus"]["SYN-A-001"]["lead_time_days"]["value"] += 1
        changed = save_configuration(database, snapshot["snapshot_id"], payload)
        updated_quality = run_quality(database, snapshot["snapshot_id"], changed["id"])
        # A new transformation version may reference unchanged immutable files.
        # Keep both snapshots in the fixture, exactly as the import contract does.
        with sqlite3.connect(database) as connection:
            new_snapshot = connection.execute(
                """INSERT INTO snapshots
                (fingerprint,created_at_utc,code_version,code_manifest_json,
                 parameter_version,parameters_json,supplier)
                SELECT fingerprint || '-ui-new-version',created_at_utc,
                       code_version || '-ui-new-version',code_manifest_json,
                       parameter_version,parameters_json,supplier
                FROM snapshots WHERE id=?""", (snapshot["snapshot_id"],),
            ).lastrowid
            connection.execute(
                "INSERT INTO snapshot_files SELECT ?,source_kind,file_id FROM snapshot_files WHERE snapshot_id=?",
                (new_snapshot, snapshot["snapshot_id"]),
            )
        datasets.append({"dataset": generated, "snapshot": snapshot, "calculation": calculation,
                         "cleaning": cleaning, "updated_quality": updated_quality,
                         "new_snapshot_id": new_snapshot})
    return datasets


def _switching_app(datasets, monkeypatch):
    database = Path(datasets[0]["dataset"]["database_path"])
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(database.parent))
    monkeypatch.setenv("HACKALEM_SOURCE_DIR", str(database.parent / "unavailable"))
    paths = {item["dataset"]["database_path"]: dataset_context(item["dataset"]["database_path"])
             for item in datasets}
    monkeypatch.setattr(home, "_dataset_options", lambda _: paths)
    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=30)
    app.selectbox(key="selected_snapshot_id").set_value(datasets[0]["snapshot"]["snapshot_id"]).run(timeout=30)
    return app


def _select_all_run_contexts(app, record):
    snapshot_id = record["snapshot"]["snapshot_id"]
    run_id = record["calculation"]["run_id"]
    keys = [f"procurement_{snapshot_id}_run_id", f"card_{snapshot_id}_run_id",
            f"scenario_{snapshot_id}_base", f"order_{snapshot_id}_source_run"]
    for key in keys:
        app.selectbox(key=key).set_value(run_id).run(timeout=30)
    assert not app.exception
    return keys


def test_calculation_date_resets_all_active_run_contexts(switchable_datasets, monkeypatch):
    app = _switching_app(switchable_datasets, monkeypatch)
    keys = _select_all_run_contexts(app, switchable_datasets[0])
    dataset_id = switchable_datasets[0]["dataset"]["dataset_id"]
    app.date_input(key=f"workspace_date_{dataset_id}").set_value(date(2026, 1, 2)).run(timeout=30)
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in keys} == dict.fromkeys(keys)


def test_quality_version_resets_all_active_run_contexts(switchable_datasets, monkeypatch):
    app = _switching_app(switchable_datasets, monkeypatch)
    record = switchable_datasets[0]
    snapshot_id = record["snapshot"]["snapshot_id"]
    app.selectbox(key=f"procurement_{snapshot_id}_quality").set_value(record["snapshot"]["run_id"]).run(timeout=30)
    keys = _select_all_run_contexts(app, record)
    app.selectbox(key=f"procurement_{snapshot_id}_quality").set_value(record["updated_quality"]["run_id"]).run(timeout=30)
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in keys} == dict.fromkeys(keys)


def test_dataset_switch_and_cache_do_not_reuse_another_database_run(switchable_datasets, monkeypatch):
    first, second = switchable_datasets
    assert first["calculation"]["run_id"] == second["calculation"]["run_id"]
    run_id = first["calculation"]["run_id"]
    before = procurement_panel._replenishment(first["dataset"]["database_path"], run_id)
    after = procurement_panel._replenishment(second["dataset"]["database_path"], run_id)
    assert before["input"]["dataset"]["dataset_id"] == first["dataset"]["dataset_id"]
    assert after["input"]["dataset"]["dataset_id"] == second["dataset"]["dataset_id"]
    assert before["items"][0]["explanation"]["regular_forecast"] != after["items"][0]["explanation"]["regular_forecast"]
    app = _switching_app(switchable_datasets, monkeypatch)
    keys = _select_all_run_contexts(app, first)
    app.selectbox(key="workspace_dataset").set_value(second["dataset"]["database_path"]).run(timeout=30)
    assert not app.exception
    assert app.selectbox(key="selected_snapshot_id").value is None
    app.selectbox(key="selected_snapshot_id").set_value(second["snapshot"]["snapshot_id"]).run(timeout=30)
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in keys} == dict.fromkeys(keys)
    app.selectbox(key=keys[0]).set_value(run_id).run(timeout=30)
    tables = [table.value for table in app.dataframe if "Прогноз L+R" in table.value.columns]
    assert len(tables) == 1
    assert tables[0].iloc[0]["Прогноз L+R"] == after["items"][0]["explanation"]["regular_forecast"]


def test_snapshot_and_supplier_switch_remove_previous_active_run(switchable_datasets, monkeypatch):
    app = _switching_app(switchable_datasets, monkeypatch)
    record = switchable_datasets[0]
    snapshot_id = record["snapshot"]["snapshot_id"]
    keys = _select_all_run_contexts(app, record)
    another = next(row["snapshot_id"] for row in record["dataset"]["snapshots"] if row["snapshot_id"] != snapshot_id)
    app.selectbox(key="selected_snapshot_id").set_value(another).run(timeout=30)
    assert not app.exception
    app.selectbox(key="selected_snapshot_id").set_value(snapshot_id).run(timeout=30)
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in keys} == dict.fromkeys(keys)
    _select_all_run_contexts(app, record)
    app.selectbox(key="workspace_supplier").set_value("IEK").run(timeout=30)
    assert not app.exception
    assert app.selectbox(key="selected_snapshot_id").value is None
    app.selectbox(key="workspace_supplier").set_value("Systeme Electric").run(timeout=30)
    app.selectbox(key="selected_snapshot_id").set_value(snapshot_id).run(timeout=30)
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in keys} == dict.fromkeys(keys)


def test_new_snapshot_version_never_inherits_previous_recommendation(switchable_datasets, monkeypatch):
    app = _switching_app(switchable_datasets, monkeypatch)
    record = switchable_datasets[0]
    original = deepcopy(record["calculation"])
    keys = _select_all_run_contexts(app, record)
    app.selectbox(key="selected_snapshot_id").set_value(record["new_snapshot_id"]).run(timeout=30)
    assert not app.exception
    assert app.selectbox(key="selected_snapshot_id").value == record["new_snapshot_id"]
    assert not any("Прогноз L+R" in table.value.columns for table in app.dataframe)
    assert not any(widget.key in keys for widget in app.selectbox)
    assert list_replenishment_runs(record["dataset"]["database_path"], record["new_snapshot_id"]) == []
    assert replenishment_report(record["dataset"]["database_path"], original["run_id"]) == original
    app.selectbox(key="selected_snapshot_id").set_value(record["snapshot"]["snapshot_id"]).run(timeout=30)
    assert not app.exception
    assert {key: app.selectbox(key=key).value for key in keys} == dict.fromkeys(keys)


def test_versioned_recommendation_card_and_scenario_flow(tmp_path, monkeypatch):
    generated = create_synthetic_dataset(tmp_path / "datasets", seed=20260923)
    database = Path(generated["database_path"])
    snapshot = next(row for row in generated["snapshots"]
                    if row["supplier"] == "Systeme Electric")
    cleaning = run_cleaning(database, snapshot["snapshot_id"], "2026-01-01")
    forecast = run_forecast(
        database, snapshot["run_id"], cleaning["run_id"], "SYN-A-001",
        _forecast_configuration(), allow_scenario=True,
    )
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(database.parent))
    monkeypatch.setenv("HACKALEM_SOURCE_DIR", str(tmp_path / "unavailable-sources"))

    app = AppTest.from_file(str(PROJECT_ROOT / "app.py")).run(timeout=30)
    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Данные и проверки", "Рекомендации", "Карточка товара", "Сценарии", "Заказы",
    ]
    snapshot_id = snapshot["snapshot_id"]
    app.selectbox(key="selected_snapshot_id").set_value(snapshot_id).run(timeout=30)
    app.selectbox(key=f"procurement_{snapshot_id}_quality").set_value(
        snapshot["run_id"]
    ).run(timeout=30)
    app.selectbox(key=f"procurement_{snapshot_id}_cleaning").set_value(
        cleaning["run_id"]
    ).run(timeout=30)
    app.multiselect(key=f"procurement_{snapshot_id}_forecasts").set_value(
        [forecast["run_id"]]
    ).run(timeout=30)
    app.button(key=f"procurement_{snapshot_id}_calculate").click().run(timeout=30)
    assert not app.exception

    runs = list_replenishment_runs(database, snapshot_id)
    assert len(runs) == 1
    base_id = runs[0]["id"]
    base_before = deepcopy(replenishment_report(database, base_id))
    recommendation_tables = [
        table.value for table in app.dataframe
        if {"Товар", "Единица", "Доступный остаток", "Ближайшее поступление",
            "Прогноз L+R", "Страховой запас", "К заказу", "Срочность",
            "Статус"}.issubset(table.value.columns)
    ]
    assert len(recommendation_tables) == 1
    assert recommendation_tables[0]["Товар"].tolist() == ["SYN-A-001"]
    assert any("СИНТЕТИЧЕСКИЕ РЕКОМЕНДАЦИИ" in row.value for row in app.warning)

    # Client-side filtering reads the cached run and must not create another version.
    app.text_input(key=f"recommendation_{base_id}_search").set_value("SYN-A").run(timeout=30)
    assert not app.exception
    assert len(list_replenishment_runs(database, snapshot_id)) == 1

    app.selectbox(key=f"card_{snapshot_id}_run_id").set_value(base_id).run(timeout=30)
    app.selectbox(key=f"card_{base_id}_sku").set_value("SYN-A-001").run(timeout=30)
    assert not app.exception
    assert any("max(0" in block.value for block in app.code)
    explanation_tables = [
        table.value for table in app.dataframe
        if {"Компонент", "Значение", "Единица"}.issubset(table.value.columns)
    ]
    assert len(explanation_tables) == 1

    app.selectbox(key=f"scenario_{snapshot_id}_base").set_value(base_id).run(timeout=30)
    app.number_input(key=f"scenario_{snapshot_id}_demand").set_value(20.0).run(timeout=30)
    app.number_input(key=f"scenario_{snapshot_id}_delay").set_value(7).run(timeout=30)
    app.text_input(key=f"scenario_{snapshot_id}_author").set_value("pytest").run(timeout=30)
    app.text_input(key=f"scenario_{snapshot_id}_reason").set_value(
        "Проверка задержки"
    ).run(timeout=30)
    scenario_button = app.button(key=f"scenario_{snapshot_id}_run")
    assert scenario_button.disabled is False
    scenario_button.click().run(timeout=30)
    assert not app.exception

    runs = list_replenishment_runs(database, snapshot_id)
    assert len(runs) == 2
    scenario_id = runs[0]["id"]
    assert scenario_id != base_id
    assert replenishment_report(database, base_id) == base_before
    scenario = replenishment_report(database, scenario_id)
    assert scenario["input"]["payload"]["scenario"] == {
        "base_run_id": base_id, "demand_factor": 1.2,
        "arrival_delay_days": 7, "author": "pytest", "reason": "Проверка задержки",
    }
    assert any(f"Создан новый расчёт № {scenario_id}" in row.value for row in app.success)
    assert app.button(key=f"order_{snapshot_id}_create").disabled is True

    # The order workflow uses the saved run and keeps corrections by SKU.
    app.selectbox(key=f"order_{snapshot_id}_source_run").set_value(base_id).run(timeout=30)
    app.text_input(key=f"order_{snapshot_id}_creator").set_value("pytest").run(timeout=30)
    app.button(key=f"order_{snapshot_id}_create").click().run(timeout=30)
    assert not app.exception
    version = list_order_versions(database, snapshot_id)[0]
    version_id = version["version_id"]
    draft = order_report(database, version_id)
    selected = draft["items"][0]["selected_quantity"]
    sku = draft["items"][0]["sku"]
    app.number_input(key=f"order_{version_id}_{sku}_quantity").set_value(selected + 12).run(timeout=30)
    app.text_input(key=f"order_{version_id}_{sku}_actor").set_value("pytest").run(timeout=30)
    app.text_input(key=f"order_{version_id}_{sku}_reason").set_value("Проверено вручную").run(timeout=30)
    app.button(key=f"order_{version_id}_{sku}_save").click().run(timeout=30)
    assert not app.exception
    assert order_report(database, version_id)["items"][0]["selected_quantity"] == selected + 12
    app.text_input(key=f"order_{version_id}_search").set_value("нет такого товара").run(timeout=30)
    assert not app.exception
    assert order_report(database, version_id)["items"][0]["selected_quantity"] == selected + 12
    app.text_input(key=f"order_{version_id}_search").set_value("").run(timeout=30)

    app.text_input(key=f"order_{version_id}_submit_actor").set_value("pytest").run(timeout=30)
    app.text_input(key=f"order_{version_id}_submit_reason").set_value("Проверить").run(timeout=30)
    app.button(key=f"order_{version_id}_submit").click().run(timeout=30)
    assert not app.exception and order_report(database, version_id)["status"] == "review"
    app.text_input(key=f"order_{version_id}_responsible").set_value("pytest lead").run(timeout=30)
    app.text_input(key=f"order_{version_id}_approval_note").set_value("Проверено").run(timeout=30)
    app.checkbox(key=f"order_{version_id}_local_identity").check().run(timeout=30)
    app.button(key=f"order_{version_id}_approve").click().run(timeout=30)
    assert not app.exception and order_report(database, version_id)["status"] == "approved"
    app.checkbox(key=f"order_{version_id}_export_ack").check().run(timeout=30)
    assert not app.exception
    assert any("SYNTHETIC_SCENARIO" in item.value for item in app.caption)
    assert len(app.get("download_button")) == 2
