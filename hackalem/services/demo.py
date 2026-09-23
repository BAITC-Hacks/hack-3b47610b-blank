"""Build a reproducible, isolated synthetic demonstration using public services."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from hackalem.config import PROJECT_ROOT, load_settings
from hackalem.services.cleaning import cleaning_report, run_cleaning
from hackalem.services.datasets import dataset_context
from hackalem.services.forecasting import forecast_config_template, forecast_report, run_forecast
from hackalem.services.lost_demand import run_lost_demand
from hackalem.services.orders import (
    approve_order, create_order_project, create_order_revision, export_order_file,
    order_report, submit_order_for_review, update_order_item,
)
from hackalem.services.replenishment import replenishment_report, run_replenishment
from hackalem.services.synthetic import create_synthetic_dataset, synthetic_report


DEMO_VERSION = "demo-v1"
DEMO_ACTOR = "DEMO — синтетический сценарий"
DEFAULT_DEMO_ROOT = PROJECT_ROOT / ".local" / "demo"
# These public fictional identities select the presentation examples. Neither
# hidden demand nor event labels are supplied to cleaning/forecasting services.
DEMO_SKUS = {
    "Systeme Electric": ("SYN-A-001", "SYN-A-002", "SYN-A-003", "SYN-A-006", "SYN-A-009"),
    "IEK": ("SYN-B-003", "SYN-B-004", "SYN-B-005", "SYN-B-006", "SYN-B-007"),
}


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Every destination belongs to the new demo directory; never replace files.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _reuse(manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != DEMO_VERSION or manifest.get("dataset_kind") != "synthetic":
        raise ValueError("Папка не содержит совместимый SYNTHETIC demo; выберите другую выходную папку.")
    synthetic_report(manifest["dataset_dir"])
    database = manifest["database_path"]
    if dataset_context(database)["kind"] != "synthetic":
        raise ValueError("Demo разрешён только для отдельной синтетической базы.")
    root = manifest_path.parent.resolve()
    for relative, digest in manifest["artifacts"].items():
        artifact = (root / relative).resolve()
        if not artifact.is_relative_to(root) or sha256(artifact.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Артефакт demo изменён: {relative}. Существующие файлы сохранены.")
    for run_id in manifest["forecast_run_ids"].values():
        forecast_report(database, run_id)
    for run_id in manifest["replenishment_run_ids"].values():
        replenishment_report(database, run_id)
    for order in manifest["orders"].values():
        order_report(database, order["approved_version_id"])
        order_report(database, order["draft_version_id"])
    return manifest


def prepare_demo(output_root=DEFAULT_DEMO_ROOT, seed=20260923):
    """Prepare a fixed-date demo once; repeat calls verify and reuse its results.

    A changed recipe/seed belongs to a new directory. An incomplete or foreign
    directory is retained and rejected, so failed attempts and user edits cannot
    be silently erased. This command never opens a real business database.
    """
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("Seed demo должен быть целым числом.")
    root = Path(output_root).resolve() / f"{DEMO_VERSION}-{seed}"
    for protected_root in (PROJECT_ROOT, load_settings().source_dir):
        for supplier_folder in ("IEK", "Systeme electric"):
            if root.is_relative_to((protected_root / supplier_folder).resolve()):
                raise ValueError("Нельзя создавать demo в папке исходных файлов.")
    manifest_path = root / "demo-manifest.json"
    if root.exists():
        if not manifest_path.is_file():
            raise ValueError("Папка demo уже существует без завершённого манифеста; выберите другую выходную папку. Данные сохранены.")
        return _reuse(manifest_path)
    root.mkdir(parents=True)
    dataset = create_synthetic_dataset(root / "datasets", seed=seed)
    database = dataset["database_path"]
    if dataset_context(database)["kind"] != "synthetic":
        raise ValueError("Demo разрешён только для отдельной синтетической базы.")
    as_of = dataset["manifest"]["as_of"]
    config = forecast_config_template(as_of)
    config["growth_application"].update(
        author=DEMO_ACTOR,
        reason="Синтетические параметры генератора; рост применяется один раз в режиме replace_trend.",
    )
    config["seasonal_aggregate_policy"].update(
        author=DEMO_ACTOR, reason="Demo использует историю SKU; агрегат с неизвестной единицей отключён.",
    )
    manifest = {
        "schema": DEMO_VERSION, "dataset_kind": "synthetic", "label": "SYNTHETIC DEMO",
        "seed": seed, "as_of": as_of, "dataset_id": dataset["dataset_id"],
        "dataset_dir": dataset["dataset_dir"], "database_path": database,
        "manifest_path": str(manifest_path), "demo_dir": str(root),
        "snapshots": dataset["snapshots"], "cleaning_run_ids": {},
        "forecast_run_ids": {}, "replenishment_run_ids": {}, "orders": {},
        "model_oracle_access": False,
        "limitations": [
            "Все количества и утверждения относятся только к вымышленному набору.",
            "Дата среза demo 01.01.2026; реальные исходники имеют отдельный срез 22.09.2026.",
            "Месячный прогноз распределяется равномерно по дням; дата риска приблизительна.",
            "DEMO — локальная подпись сценария, не аутентификация и не утверждение реального заказа.",
        ],
    }
    reports = root / "reports"
    for snapshot in dataset["snapshots"]:
        supplier = snapshot["supplier"]
        cleaned = run_cleaning(database, snapshot["snapshot_id"], as_of,
                               policy="exclude_high_confidence")
        manifest["cleaning_run_ids"][supplier] = cleaned["run_id"]
        items = []
        for sku in DEMO_SKUS[supplier]:
            lost_run_id = None
            if sku == "SYN-A-009":
                lost = run_lost_demand(database, cleaned["run_id"], sku)
                lost_run_id = lost["run_id"]
                manifest["lost_demand_run_id"] = lost_run_id
                _write_json(reports / "stockout-lost-demand.json", lost)
                raw = run_forecast(database, snapshot["run_id"], cleaned["run_id"],
                                   sku, config, allow_scenario=True)
                manifest["forecast_run_ids"]["stockout_raw"] = raw["run_id"]
                _write_json(reports / "stockout-raw-forecast.json", raw)
            forecast = run_forecast(
                database, snapshot["run_id"], cleaned["run_id"], sku, config,
                allow_scenario=True, lost_demand_run_id=lost_run_id,
            )
            manifest["forecast_run_ids"][sku] = forecast["run_id"]
            _write_json(reports / f"forecast-{sku}.json", forecast)
            items.append({
                "sku": sku, "category_code": forecast["summary"]["category"]["code"],
                "forecast_run_id": forecast["run_id"], "project_commitments": [],
            })
        payload = {
            "quality_run_id": snapshot["run_id"], "cleaning_run_id": cleaned["run_id"],
            "as_of": as_of, "supplier": supplier, "warehouse": "all_selected_warehouses",
            "items": items,
        }
        calculation = run_replenishment(database, payload)
        if any(item["order_quantity"] is None for item in calculation["items"]):
            raise ValueError(f"Demo: не все выбранные строки {supplier} рассчитаны; данные сохранены.")
        manifest["replenishment_run_ids"][supplier] = calculation["run_id"]
        _write_json(reports / f"replenishment-{snapshot['snapshot_id']}.json", calculation)
        if supplier == "IEK":
            delayed_payload = deepcopy(payload)
            delayed_payload["scenario"] = {
                "base_run_id": calculation["run_id"], "demand_factor": 1.0,
                "arrival_delay_days": 14, "author": DEMO_ACTOR,
                "reason": "Демонстрация задержки существующего поступления на 14 дней.",
            }
            delayed = run_replenishment(database, delayed_payload)
            manifest["replenishment_run_ids"]["delay"] = delayed["run_id"]
            _write_json(reports / "incoming-delay.json", delayed)

        order = create_order_project(database, calculation["run_id"], DEMO_ACTOR)
        reviewed = submit_order_for_review(database, order["version_id"], DEMO_ACTOR,
                                          "DEMO: проверка вымышленных количеств и единиц.")
        approved = approve_order(database, reviewed["version_id"], DEMO_ACTOR,
                                 "DEMO: только SYNTHETIC, отправка поставщику отсутствует.")
        revision = create_order_revision(database, approved["version_id"], DEMO_ACTOR,
                                         "DEMO: новая редактируемая версия после утверждения.")
        positive = next(item for item in revision["items"] if item["selected_quantity"] > 0)
        multiple = positive["source"]["explanation"]["order_multiple"]
        draft = update_order_item(
            database, revision["version_id"], positive["sku"],
            positive["selected_quantity"] + multiple, DEMO_ACTOR,
            "DEMO: иллюстративная корректировка на одну кратность; требуется новая проверка.",
        )
        exports = []
        for status, version in (("approved", approved), ("draft", draft)):
            _write_json(reports / f"order-{snapshot['snapshot_id']}-{status}.json", version)
            for extension in ("csv", "xlsx"):
                exported = export_order_file(
                    database, version["version_id"],
                    root / "exports" / f"SYNTHETIC-{snapshot['snapshot_id']}-{status}.{extension}",
                )
                exports.append({
                    "path": exported["path"], "verified": exported["verified"],
                    "classification": exported["metadata"]["classification"],
                    "version_id": version["version_id"], "status": status,
                })
        manifest["orders"][supplier] = {
            "project_id": approved["project_id"], "approved_version_id": approved["version_id"],
            "draft_version_id": draft["version_id"], "exports": exports,
        }
    _write_json(reports / "one-off-cleaning.json", cleaning_report(
        database, manifest["cleaning_run_ids"]["Systeme Electric"], sku="SYN-A-006", limit=1000,
    ))
    manifest["artifacts"] = {
        path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
        for directory in (reports, root / "exports") for path in sorted(directory.iterdir())
    }
    _write_json(manifest_path, manifest)
    return manifest
