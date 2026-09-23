"""Reproducible demonstration exercises the complete synthetic service chain."""

from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from hackalem.services.demo import prepare_demo
from hackalem.__main__ import main
from hackalem.services.forecasting import forecast_report
from hackalem.services.orders import order_report
from hackalem.services.replenishment import replenishment_report


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo")
    return root, prepare_demo(root)


def test_demo_connects_five_requirements_with_saved_versions(demo):
    _, manifest = demo
    database = manifest["database_path"]
    assert manifest["dataset_kind"] == "synthetic"
    assert manifest["as_of"] == "2026-01-01"
    assert manifest["model_oracle_access"] is False
    base = replenishment_report(database, manifest["replenishment_run_ids"]["IEK"])
    delay = replenishment_report(database, manifest["replenishment_run_ids"]["delay"])
    normal = next(row for row in base["items"] if row["sku"] == "SYN-B-003")
    delayed = next(row for row in delay["items"] if row["sku"] == normal["sku"])
    assert normal["urgent_problem"] is False
    assert delayed["urgent_problem"] is True
    assert delayed["explanation"]["urgent_risk_date"] < delayed["explanation"]["new_order_eta"]
    assert delay["input"]["payload"]["scenario"]["base_run_id"] == base["run_id"]

    forecasts = manifest["forecast_run_ids"]
    seasonal = forecast_report(database, forecasts["SYN-A-002"])["summary"]
    trend = forecast_report(database, forecasts["SYN-A-003"])["summary"]
    assert seasonal["selected_model"] == "seasonal_analog"
    assert trend["forecasts"][-1]["prediction"] > trend["forecasts"][0]["prediction"]
    raw = forecast_report(database, forecasts["stockout_raw"])
    adjusted = forecast_report(database, forecasts["SYN-A-009"])
    assert adjusted["lost_demand_run_id"] == manifest["lost_demand_run_id"]
    assert sum(row["prediction"] for row in adjusted["summary"]["forecasts"]) > sum(
        row["prediction"] for row in raw["summary"]["forecasts"])
    assert adjusted["summary"]["lost_demand_input"]["historical_lost_demand_as_current_backlog"] == 0
    cleaning = json.loads((Path(manifest["demo_dir"]) / "reports/one-off-cleaning.json").read_text(encoding="utf-8"))
    assert any(row["removed_component"] > 0 for row in cleaning["months"])
    for supplier, order in manifest["orders"].items():
        approved = order_report(database, order["approved_version_id"])
        draft = order_report(database, order["draft_version_id"])
        assert approved["supplier"] == draft["supplier"] == supplier
        assert approved["status"] == "approved" and draft["status"] == "draft"
        assert approved["approval_snapshot"]["dataset"]["kind"] == "synthetic"
        assert draft["parent_version_id"] == approved["version_id"]
        assert any(row["selected_quantity"] != row["suggested_quantity"] for row in draft["items"])
        assert all(export["verified"] and export["classification"] == "SYNTHETIC_SCENARIO"
                   for export in order["exports"])
        assert {Path(export["path"]).suffix for export in order["exports"]} == {".csv", ".xlsx"}
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_demo_repeat_preserves_database_runs_and_artifacts(demo):
    root, manifest = demo
    paths = [Path(manifest["database_path"]), Path(manifest["manifest_path"])] + [
        Path(manifest["demo_dir"]) / relative for relative in manifest["artifacts"]]
    before = {path: sha256(path.read_bytes()).hexdigest() for path in paths}
    assert prepare_demo(root) == manifest
    assert {path: sha256(path.read_bytes()).hexdigest() for path in paths} == before
    assert all(not relative.startswith(("datasets/", "validation/", "model/"))
               for relative in manifest["artifacts"])


def test_demo_preserves_an_unfinished_or_foreign_directory(tmp_path):
    existing = tmp_path / "demo-v1-20260923"
    existing.mkdir()
    marker = existing / "user-note.txt"
    marker.write_text("Keep me", encoding="utf-8")
    with pytest.raises(ValueError, match="уже существует"):
        prepare_demo(tmp_path)
    assert marker.read_text(encoding="utf-8") == "Keep me"


def test_demo_cli_does_not_initialize_selected_real_database(demo, tmp_path, monkeypatch, capsys):
    root, manifest = demo
    real_data = tmp_path / "live-business-database"
    monkeypatch.setenv("HACKALEM_DATA_DIR", str(real_data))
    assert main(["demo", "--output-root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["database_path"] == manifest["database_path"]
    assert not real_data.exists()
