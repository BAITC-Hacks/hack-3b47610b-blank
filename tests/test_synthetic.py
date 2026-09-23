"""Independent properties of the isolated synthetic validation dataset."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import shutil
import sqlite3

import pytest
from openpyxl import Workbook

from hackalem.config import Settings
from hackalem.services.quality import (
    calculation_input, get_configuration, quality_report, run_quality,
    save_configuration,
)
from hackalem.services.synthetic import create_synthetic_dataset, read_observed_context, synthetic_report
from hackalem.services.systeme import import_systeme, read_records
from hackalem.storage import initialize_database
from hackalem.synthetic.generator import generate_dataset


SEED = 20260923
SCENARIO = "Сценарный расчёт"
MISSING = "Не хватает данных"
EXPECTED_SCENARIOS = {
    "stable", "seasonal", "growth", "intermittent", "new_product",
    "one_off_client", "repeated_large_client", "return", "stockout",
    "unknown_blank", "no_open_orders", "timely_incoming", "late_incoming",
    "minimum_order", "order_multiple", "unit_conversion", "missing_critical",
}
ORACLE_KEYS = {
    "true_demand", "regular_demand", "lost_demand", "outlier_quantity",
    "return_quantity", "is_outlier", "outlier_label", "scenario_id",
    "expected_properties", "expected_forecast", "expected_order",
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def _scenarios(data):
    return {item["id"]: item for item in data["scenarios"]}


@pytest.fixture(scope="module")
def generated(tmp_path_factory):
    return create_synthetic_dataset(tmp_path_factory.mktemp("synthetic"), seed=SEED)


def _copy_dataset(generated, tmp_path):
    target = tmp_path / generated["dataset_id"]
    shutil.copytree(Path(generated["dataset_dir"]), target)
    return target


def test_pure_generator_is_deterministic_seeded_and_preserves_global_rng():
    before = random.getstate()
    first = generate_dataset(seed=SEED)
    second = generate_dataset(seed=SEED)
    changed = generate_dataset(seed=SEED + 1)
    assert random.getstate() == before
    assert _canonical(first) == _canonical(second)
    assert _canonical(first["observed"]) != _canonical(changed["observed"])
    assert first["manifest"]["dataset_id"] != changed["manifest"]["dataset_id"]


def test_generator_covers_case_without_oracle_fields_in_observations():
    data = generate_dataset(seed=SEED)
    scenarios = _scenarios(data)
    assert set(scenarios) == EXPECTED_SCENARIOS
    assert {requirement for item in scenarios.values() for requirement in item["must_haves"]} == {
        "base_need", "seasonality_growth", "lost_demand", "one_off_orders", "supplier_orders",
    }
    assert all(item["expected_properties"] and item["pending_checks"] for item in scenarios.values())
    observed = data["observed"]
    assert set(observed) == {"products", "transactions", "availability", "incoming", "parameters"}
    assert not (set(_keys(observed)) & ORACLE_KEYS)
    assert len(observed["products"]) == 17
    skus = {item["sku"] for item in observed["products"]}
    assert skus == {item["sku"] for item in scenarios.values()}
    assert all(sku.startswith("SYN-") for sku in skus)
    assert {item["supplier"] for item in observed["products"]} == {"Systeme Electric", "IEK"}
    months = {item["date"][:7] for item in observed["transactions"]}
    expected_months = {f"{year}-{month:02}" for year in (2024, 2025) for month in range(1, 13)}
    assert months == expected_months
    assert all(item["date"] < "2026-01-01" for item in observed["transactions"])
    assert all(item["date"] < "2026-01-01" for item in observed["availability"])
    assert all(item["customer_id"] is None or item["customer_id"].startswith("SYN-")
               for item in observed["transactions"])
    new_sku = scenarios["new_product"]["sku"]
    new_product = next(item for item in observed["products"] if item["sku"] == new_sku)
    assert new_product["launch_date"] == "2025-10-01"
    assert all(item["date"] >= new_product["launch_date"]
               for item in observed["transactions"] if item["sku"] == new_sku)


def test_stockout_unknown_blank_and_client_labels_are_distinct_observations():
    data = generate_dataset(seed=SEED)
    scenarios = _scenarios(data)
    transactions = data["observed"]["transactions"]
    availability = data["observed"]["availability"]
    stockout_sku, unknown_sku = (scenarios[name]["sku"] for name in ("stockout", "unknown_blank"))
    stockout_days = {item["date"] for item in availability
                     if item["sku"] == stockout_sku and item["available"] is False}
    assert stockout_days
    assert all(item["observed_hours"] == 24 for item in availability
               if item["sku"] == stockout_sku and item["date"] in stockout_days)
    unknown_days = {item["date"] for item in availability
                    if item["sku"] == unknown_sku and item["available"] is None}
    assert len(unknown_days) == 7
    assert all(item["observed_hours"] is None for item in availability
               if item["sku"] == unknown_sku and item["date"] in unknown_days)
    unknown_rows = [item for item in transactions
                    if item["sku"] == unknown_sku and item["date"] in unknown_days]
    assert unknown_rows and all(item["quantity"] is None and item["state"] == "blank" for item in unknown_rows)
    return_rows = [item for item in transactions if item["sku"] == scenarios["return"]["sku"] and item["quantity"] is not None and item["quantity"] < 0]
    assert return_rows, "A return must remain a signed observation, not an outlier deletion."
    truth_by_day = {(item["sku"], item["date"]): item for item in data["truth"]["daily"]}
    for day in stockout_days:
        truth = truth_by_day[stockout_sku, day]
        assert truth["true_demand"] > 0
        assert truth["observed_sales"] == 0
        assert truth["lost_demand"] == truth["true_demand"]
    for day in unknown_days:
        truth = truth_by_day[unknown_sku, day]
        assert truth["observed_sales"] is None
        assert truth["lost_demand"] == 0
    one_off_sku, repeated_sku = (scenarios[name]["sku"] for name in ("one_off_client", "repeated_large_client"))
    one_off = [item for item in data["truth"]["daily"] if item["sku"] == one_off_sku and item["outlier_quantity"] > 0]
    assert len(one_off) == 1
    assert all(item["outlier_quantity"] == 0 for item in data["truth"]["daily"] if item["sku"] == repeated_sku)
    assert sum(item["return_quantity"] for item in data["truth"]["daily"] if item["sku"] == scenarios["return"]["sku"]) < 0


def test_identical_seed_has_equal_immutable_artifacts_and_reuses_dataset(generated, tmp_path):
    first_dir = Path(generated["dataset_dir"])
    first_manifest = _json(first_dir / "manifest.json")
    before = {name: _sha(first_dir / name) for name in first_manifest["files"]}
    reused = create_synthetic_dataset(first_dir.parent, seed=SEED)
    assert reused["dataset_id"] == generated["dataset_id"]
    assert Path(reused["database_path"]) == Path(generated["database_path"])
    assert before == {name: _sha(first_dir / name) for name in first_manifest["files"]}
    independent = create_synthetic_dataset(tmp_path, seed=SEED)
    other_dir = Path(independent["dataset_dir"])
    other_manifest = _json(other_dir / "manifest.json")
    assert first_manifest["files"] == other_manifest["files"]
    assert first_manifest["model_fingerprint"] == other_manifest["model_fingerprint"]
    assert _sha(first_dir / "manifest.json") == _sha(other_dir / "manifest.json")
    for name, digest in first_manifest["files"].items():
        assert before[name] == digest == _sha(other_dir / name)


def test_creation_keeps_existing_real_database_unchanged(tmp_path):
    real_db = tmp_path / "hackalem.sqlite3"
    initialize_database(real_db)
    with closing(sqlite3.connect(real_db)) as connection, connection:
        connection.execute("INSERT INTO app_metadata VALUES ('real-sentinel','do-not-modify')")
    before = _sha(real_db)
    created = create_synthetic_dataset(tmp_path, seed=SEED)
    assert Path(created["database_path"]) != real_db
    assert _sha(real_db) == before
    with closing(sqlite3.connect(created["database_path"])) as connection:
        metadata = dict(connection.execute("SELECT key,value FROM app_metadata"))
        assert metadata["dataset_kind"] == "synthetic"
        assert metadata["dataset_id"] == created["dataset_id"]
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for (value,) in connection.execute("SELECT parameters_json FROM snapshots"):
            assert json.loads(value)["dataset"] == "synthetic"


def test_normalized_model_has_observations_lineage_and_no_truth(generated):
    database = Path(generated["database_path"])
    observed = _json(Path(generated["dataset_dir"]) / "model" / "observations.json")
    assert not (set(_keys(observed)) & ORACLE_KEYS)
    with closing(sqlite3.connect(database)) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"synthetic_customers", "synthetic_availability", "synthetic_products"} <= tables
        assert not any("truth" in table or "oracle" in table for table in tables)
        for table in tables:
            fields = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            assert not (fields & ORACLE_KEYS), table
        assert connection.execute("SELECT COUNT(DISTINCT sku) FROM products").fetchone()[0] == 17
        customers = connection.execute("SELECT COUNT(customer_id) FROM synthetic_customers").fetchone()[0]
        transaction_count = connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        expected_customers = sum(item["customer_id"] is not None for item in observed["transactions"])
        assert customers == expected_customers > 0
        assert transaction_count == len(observed["transactions"])
        assert connection.execute("""SELECT COUNT(*) FROM synthetic_customers c
            LEFT JOIN transactions t ON t.file_id=c.file_id AND t.sheet=c.sheet AND t.row=c.row
            WHERE t.file_id IS NULL""").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM synthetic_availability WHERE available=0").fetchone()[0] > 0
        assert connection.execute("SELECT COUNT(*) FROM synthetic_availability WHERE available IS NULL").fetchone()[0] > 0
        source_kind_count = connection.execute("SELECT snapshot_id,COUNT(*) FROM snapshot_files GROUP BY snapshot_id").fetchall()
        assert len(source_kind_count) == 2 and all(count == 6 for _, count in source_kind_count)
    for snapshot in generated["snapshots"]:
        history = read_records(database, snapshot["snapshot_id"], "monthly_sales", "monthly_values")
        assert history
        assert not (set(_keys(history)) & ORACLE_KEYS)
        with pytest.raises(ValueError):
            read_records(database, snapshot["snapshot_id"], "monthly_sales", "truth")


def test_synthetic_uses_shared_quality_gate_and_cannot_be_confirmed(generated, tmp_path):
    dataset_dir = _copy_dataset(generated, tmp_path)
    database = dataset_dir / "model" / "hackalem.sqlite3"
    scenarios = _scenarios(generate_dataset(seed=SEED))
    stable = scenarios["stable"]
    snapshot = next(item for item in generated["snapshots"] if item["supplier"] == stable["supplier"])
    report = quality_report(database, snapshot["run_id"], sku=stable["sku"])
    assert report["dataset"]["kind"] == "synthetic"
    assert report["skus"][0]["status"] == SCENARIO
    assert not report["skus"][0]["eligible_for_confirmed_order"]
    with pytest.raises(ValueError):
        calculation_input(database, snapshot["run_id"], stable["sku"])
    model_input = calculation_input(database, snapshot["run_id"], stable["sku"], allow_scenario=True)
    assert len(model_input["history"]) == 24
    assert not (set(_keys(model_input)) & ORACLE_KEYS)
    confirmed = deepcopy(get_configuration(database, snapshot["configuration_id"])["payload"])
    for group in [confirmed["defaults"], *confirmed["skus"].values()]:
        for entry in group.values():
            entry["status"] = "confirmed"
    for choice in confirmed["sales_choices"]:
        choice["status"] = "confirmed"
    config = save_configuration(database, snapshot["snapshot_id"], confirmed)
    rerun = run_quality(database, snapshot["snapshot_id"], config["id"])
    updated = quality_report(database, rerun["run_id"], sku=stable["sku"])["skus"][0]
    assert updated["status"] == SCENARIO
    assert updated["eligible_for_calculation"] and not updated["eligible_for_confirmed_order"]
    with pytest.raises(ValueError):
        calculation_input(database, rerun["run_id"], stable["sku"])
    # Appending decisions and quality runs must not invalidate immutable observations.
    create_synthetic_dataset(tmp_path, seed=SEED)


@pytest.mark.parametrize("scenario_id", ["return", "unknown_blank", "missing_critical"])
def test_unresolved_scenarios_remain_blocked_before_later_stages(generated, scenario_id):
    scenario = _scenarios(generate_dataset(seed=SEED))[scenario_id]
    snapshot = next(item for item in generated["snapshots"] if item["supplier"] == scenario["supplier"])
    report = quality_report(generated["database_path"], snapshot["run_id"], sku=scenario["sku"])
    assert report["skus"][0]["status"] == MISSING
    assert report["skus"][0]["reasons"]
    with pytest.raises(ValueError):
        calculation_input(generated["database_path"], snapshot["run_id"], scenario["sku"], allow_scenario=True)


@pytest.mark.parametrize("relative_path", ["model/observations.json", "validation/truth.json"])
def test_reuse_rejects_corrupt_artifact_without_overwriting(generated, tmp_path, relative_path):
    dataset_dir = _copy_dataset(generated, tmp_path)
    path = dataset_dir / relative_path
    changed = path.read_bytes() + b"\nCORRUPTED"
    path.write_bytes(changed)
    with pytest.raises(ValueError):
        create_synthetic_dataset(tmp_path, seed=SEED)
    assert path.read_bytes() == changed
    with pytest.raises(ValueError):
        synthetic_report(dataset_dir)


@pytest.mark.parametrize("mutation", ["quantity", "import_issue"])
def test_reuse_rejects_changed_import_facts(generated, tmp_path, mutation):
    dataset_dir = _copy_dataset(generated, tmp_path)
    database = dataset_dir / "model" / "hackalem.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        if mutation == "quantity":
            connection.execute("UPDATE monthly_values SET quantity=quantity+1 WHERE rowid=(SELECT rowid FROM monthly_values WHERE quantity IS NOT NULL LIMIT 1)")
        else:
            # Source issues participate in the chosen-input gate just like quantities.
            connection.execute("""INSERT INTO import_issues(file_id,severity,code,sheet,row,cell,message,sku)
                SELECT file_id,'error','FACT_PERIOD_INVALID',sheet,row,cell,'Changed source diagnostic',sku
                FROM transactions ORDER BY rowid LIMIT 1""")
    modified = _sha(database)
    with pytest.raises(ValueError):
        create_synthetic_dataset(tmp_path, seed=SEED)
    assert _sha(database) == modified


def test_unknown_existing_directory_is_not_overwritten(generated, tmp_path):
    existing = tmp_path / generated["dataset_id"]
    existing.mkdir()
    sentinel = existing / "user-file.txt"
    sentinel.write_text("Keep this file", encoding="utf-8")
    with pytest.raises(ValueError):
        create_synthetic_dataset(tmp_path, seed=SEED)
    assert list(existing.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "Keep this file"


def test_real_import_refuses_synthetic_database_before_writing(generated, tmp_path):
    dataset_dir = _copy_dataset(generated, tmp_path)
    source_root = tmp_path / "real-source"
    folder = source_root / "Systeme electric"
    folder.mkdir(parents=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Номенклатура", "Номенклатура.Код", "Артикул", "Кратность"])
    sheet.append(["Тестовый реальный источник", "REAL-001", "REAL-ART-001", 1])
    workbook.save(folder / "valid-minimums.xlsx")
    database = dataset_dir / "model" / "hackalem.sqlite3"
    before = _sha(database)
    settings = Settings(source_dir=source_root, data_dir=database.parent)
    with pytest.raises(ValueError, match="(?i)synthetic|синтет"):
        import_systeme(settings)
    assert _sha(database) == before


def test_observed_context_is_cutoff_bound_supplier_scoped_and_oracle_independent(generated, tmp_path):
    dataset_dir = _copy_dataset(generated, tmp_path)
    database = dataset_dir / "model" / "hackalem.sqlite3"
    snapshot = next(item for item in generated["snapshots"] if item["supplier"] == "Systeme Electric")
    # The model reader must work with validation files wholly unavailable.
    (dataset_dir / "validation").rename(dataset_dir / "validation-hidden")
    early = read_observed_context(database, snapshot["snapshot_id"], "2024-01-15")
    later = read_observed_context(database, snapshot["snapshot_id"], "2025-12-31")
    assert early["dataset"]["kind"] == "synthetic"
    assert early["customers"] and early["availability"]
    assert all(item["occurred_at"][:10] <= "2024-01-15" for item in early["customers"])
    assert all(item["date"] <= "2024-01-15" for item in early["availability"])
    assert all(item["sku"].startswith("SYN-A-") for key in ("customers", "availability") for item in early[key])
    assert len(later["customers"]) > len(early["customers"])
    assert len(later["availability"]) > len(early["availability"])
    assert not (set(_keys(early)) & ORACLE_KEYS)
    with pytest.raises(ValueError):
        read_observed_context(database, snapshot["snapshot_id"], "2026-01-01")
    with pytest.raises(ValueError):
        read_observed_context(database, snapshot["snapshot_id"], "2026-02-01")
    real_database = tmp_path / "real.sqlite3"
    initialize_database(real_database)
    before = _sha(real_database)
    with pytest.raises(ValueError):
        read_observed_context(real_database, snapshot["snapshot_id"], "2024-01-15")
    assert _sha(real_database) == before
