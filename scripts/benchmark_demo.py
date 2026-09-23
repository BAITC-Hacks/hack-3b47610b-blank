"""Measure a fresh synthetic demo, recomputation and versioned report reads.

Run: uv run --locked python -m scripts.benchmark_demo --output-dir NEW_DIRECTORY
Only the final evaluator reads hidden truth. No real database is opened.
"""

import argparse
import cProfile
from datetime import UTC, datetime
import io
import json
import os
from pathlib import Path
import platform
import pstats
import sqlite3
import sys
from time import perf_counter

from hackalem.services.cleaning import cleaning_report
from hackalem.services.demo import prepare_demo
from hackalem.services.forecasting import forecast_report, run_forecast
from hackalem.services.lost_demand import lost_demand_report
from hackalem.services.orders import order_report
from hackalem.services.replenishment import replenishment_report, run_replenishment
from hackalem.services.synthetic import evaluate_forecasts
from hackalem.services.systeme import _code_manifest


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _counts(database):
    with sqlite3.connect(database) as connection:
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("cleaning_runs", "lost_demand_runs", "forecast_runs",
                              "replenishment_runs", "order_projects", "order_versions")}


def _evaluate(manifest):
    """Evaluation-only access: model and order generation have finished."""
    dataset_dir = Path(manifest["dataset_dir"])
    ids = manifest["forecast_run_ids"]
    main = evaluate_forecasts(dataset_dir, [ids[sku] for sku in (
        "SYN-A-001", "SYN-A-002", "SYN-A-003",
    )])
    stockout = evaluate_forecasts(dataset_dir, [ids["stockout_raw"], ids["SYN-A-009"]])
    truth = json.loads((dataset_dir / "validation/truth.json").read_text(encoding="utf-8"))
    spec = json.loads((dataset_dir / "validation/spec.json").read_text(encoding="utf-8"))
    acceptance = next(row["acceptance"] for row in spec["requirements"] if row["id"] == "lost_demand")
    target = sum(row["regular_demand"] for row in truth["daily"]
                 if row["sku"] == "SYN-A-009" and row["date"].startswith("2025-08"))
    values = {}
    for label, key in (("raw", "stockout_raw"), ("adjusted", "SYN-A-009")):
        forecast = forecast_report(manifest["database_path"], ids[key])
        values[label] = next(row["prediction"] for row in forecast["summary"]["backtest"]
                             if row["period"] == "2025-08-01")
    error_reduction = 1 - abs(values["adjusted"] - target) / abs(values["raw"] - target)
    raw_metrics, adjusted_metrics = [row["metrics"] for row in stockout["reports"]]
    checks = {
        "primary_forecast_thresholds": main["passed"],
        "stockout_prediction_increased": values["adjusted"] > values["raw"],
        "stockout_error_reduction": error_reduction >= acceptance["absolute_error_reduction_vs_observed_min"],
        "stockout_corrected_error": abs(values["adjusted"] - target) / target <= acceptance["corrected_month_relative_error_max"],
        "stockout_wape_improved": adjusted_metrics["wape"] < raw_metrics["wape"],
    }
    return {
        "schema": "hackalem-demo-acceptance-history-1", "spec_version": spec["version"],
        "dataset_id": manifest["dataset_id"], "dataset_kind": "synthetic",
        "as_of": manifest["as_of"], "truth_access": "evaluator_only_after_model_runs",
        "primary_forecasts": main,
        # evaluate_forecasts.passed expects all three primary SKUs. For this
        # two-run stockout comparison only its per-run metrics are relevant.
        "stockout": {
            "period": "2025-08-01", "hidden_regular_demand": target,
            "raw_prediction": values["raw"], "adjusted_prediction": values["adjusted"],
            "absolute_error_reduction": error_reduction, "reports": stockout["reports"],
        },
        "checks": checks, "passed": all(checks.values()),
        "limitation": "Synthetic held-out history; no claim about real-world demand accuracy or savings.",
    }


def benchmark(output_dir, seed=20260923):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "hackalem-demo-benchmark-1", "status": "running",
        "started_at_utc": datetime.now(UTC).isoformat(), "seed": seed,
        "python": sys.version, "platform": platform.platform(), "logical_cpu_count": os.cpu_count(),
        "code_hash_before": _code_manifest()[0], "phases": [],
        "scope": "Fresh full synthetic pipeline, verified pipeline reuse, repeated service calls, report reads.",
        "timing_protocol": "One unprofiled measurement per phase; profiling is separate and has overhead.",
    }
    destination = output_dir / "benchmark.json"

    def save():
        _write_json(destination, report)

    def measure(name, operation):
        started = perf_counter()
        result = operation()
        elapsed = perf_counter() - started
        report["phases"].append({"name": name, "seconds": elapsed, "profiled": False})
        save()
        print(f"{name}: {elapsed:.3f}s", flush=True)
        return result

    save()
    try:
        demo_root = output_dir / "demo"
        manifest = measure("full_pipeline_first", lambda: prepare_demo(demo_root, seed=seed))
        database = manifest["database_path"]
        report.update(database_path=database, demo_manifest_path=manifest["manifest_path"],
                      as_of=manifest["as_of"], dataset_kind=manifest["dataset_kind"])
        counts_before = _counts(database)
        forecasts = [forecast_report(database, run_id)
                     for run_id in manifest["forecast_run_ids"].values()]
        replenishments = [replenishment_report(database, run_id)
                         for run_id in manifest["replenishment_run_ids"].values()]
        reused = measure("pipeline_verified_reuse", lambda: prepare_demo(demo_root, seed=seed))
        if reused != manifest:
            raise AssertionError("Pipeline reuse changed the manifest")

        def repeat_forecasts():
            ids = []
            for stored in forecasts:
                repeated = run_forecast(
                    database, stored["quality_run_id"], stored["cleaning_run_id"],
                    stored["sku"], stored["config"], allow_scenario=True,
                    lost_demand_run_id=stored["lost_demand_run_id"],
                )
                if repeated["run_id"] != stored["run_id"]:
                    raise AssertionError("Repeat forecast did not reproduce run_id")
                ids.append(repeated["run_id"])
            return ids

        def repeat_replenishments():
            ids = []
            for stored in replenishments:
                repeated = run_replenishment(database, stored["input"]["payload"])
                if repeated["run_id"] != stored["run_id"]:
                    raise AssertionError("Repeat replenishment did not reproduce run_id")
                ids.append(repeated["run_id"])
            return ids

        def read_reports():
            for stored in forecasts:
                forecast_report(database, stored["run_id"])
            for stored in replenishments:
                replenishment_report(database, stored["run_id"])
            for run_id in manifest["cleaning_run_ids"].values():
                cleaning_report(database, run_id)
            lost_demand_report(database, manifest["lost_demand_run_id"])
            for order in manifest["orders"].values():
                order_report(database, order["approved_version_id"])
                order_report(database, order["draft_version_id"])

        repeated_forecasts = measure("forecast_service_repeat", repeat_forecasts)
        repeated_replenishments = measure("replenishment_service_repeat", repeat_replenishments)
        measure("versioned_report_reads", read_reports)
        report["repeat"] = {
            "forecast_count": len(forecasts), "forecast_run_ids": repeated_forecasts,
            "replenishment_count": len(replenishments), "replenishment_run_ids": repeated_replenishments,
            "same_run_ids": True, "counts_before": counts_before,
        }
        profiler = cProfile.Profile()
        started = perf_counter()
        profiler.enable()
        repeat_forecasts()
        repeat_replenishments()
        read_reports()
        profiler.disable()
        report["phases"].append({"name": "repeat_and_reads_profiled", "seconds": perf_counter() - started,
                                 "profiled": True})
        profiler.dump_stats(str(output_dir / "repeat-and-reads.prof"))
        text = io.StringIO()
        pstats.Stats(profiler, stream=text).strip_dirs().sort_stats("cumulative").print_stats(40)
        (output_dir / "repeat-and-reads.txt").write_text(text.getvalue(), encoding="utf-8")
        acceptance = measure("history_acceptance_evaluator", lambda: _evaluate(manifest))
        _write_json(output_dir / "acceptance-history.json", acceptance)
        report["history_acceptance_passed"] = acceptance["passed"]
        report["repeat"]["counts_after"] = _counts(database)
        report["repeat"]["counts_unchanged"] = counts_before == report["repeat"]["counts_after"]
        with sqlite3.connect(database) as connection:
            report["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            report["foreign_key_errors"] = connection.execute("PRAGMA foreign_key_check").fetchall()
        if not (acceptance["passed"] and report["repeat"]["counts_unchanged"]
                and report["integrity_check"] == "ok" and not report["foreign_key_errors"]):
            raise AssertionError("Demo acceptance, repeated version counts or database integrity failed")
        report["status"] = "complete"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["code_hash_after"] = _code_manifest()[0]
        report["code_unchanged"] = report["code_hash_before"] == report["code_hash_after"]
        if not report["code_unchanged"]:
            report["status"] = "failed"
            report["error"] = "Implementation changed during measurement; rerun in a new directory."
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        save()
    if not report["code_unchanged"]:
        raise AssertionError(report["error"])
    print(f"Evidence: {destination}", flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="A new directory; existing paths are rejected")
    parser.add_argument("--seed", type=int, default=20260923)
    arguments = parser.parse_args()
    benchmark(arguments.output_dir, arguments.seed)
