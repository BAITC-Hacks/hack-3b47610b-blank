"""Benchmark real ingestion and preparation in a new isolated local directory.

Run from the checkout: uv run --locked python scripts/benchmark_real.py
    --output-dir .local/stage12-benchmark-UNIQUE
This does not measure a complete order: unconfirmed real inputs remain blocked.
"""

import argparse
import cProfile
import hashlib
import io
import json
import platform
import pstats
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hackalem.config import load_settings
from hackalem.domain.quality_config import configuration_template
from hackalem.services.cleaning import cleaning_report, prepared_input, run_cleaning
from hackalem.services.imports import SUPPLIERS, import_supplier, report_snapshot
from hackalem.services.quality import calculation_input, quality_report, run_quality, save_configuration
from hackalem.services.systeme import _code_manifest


AS_OF = "2026-09-22"


def _hashes(paths, root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def benchmark(output_dir, source_dir=None):
    output_dir = Path(output_dir).resolve()
    settings = load_settings({
        "HACKALEM_SOURCE_DIR": str(Path(source_dir).resolve() if source_dir else ROOT),
        "HACKALEM_DATA_DIR": str(output_dir),
    })
    # Never reuse an existing database or overwrite a previous evidence artifact.
    output_dir.mkdir(parents=True, exist_ok=False)
    paths = [path for folder in ("Systeme electric", "IEK")
             for path in (settings.source_dir / folder).glob("*.xlsx")
             if not path.name.startswith("~$")]
    if len(paths) != 12:
        raise ValueError(f"Expected 12 source workbooks, found {len(paths)}.")
    report = {
        "schema": "hackalem-real-benchmark-1", "started_at_utc": datetime.now(UTC).isoformat(),
        "as_of": AS_OF, "database_path": str(settings.database_path),
        "source_dir": str(settings.source_dir), "python": sys.version,
        "platform": platform.platform(), "source_hashes_before": _hashes(paths, settings.source_dir),
        "code_hash_before": _code_manifest()[0], "phases": [], "suppliers": {},
        "scope": "Import, data quality and regular-demand preparation only; not a complete order.",
        "business_parameters": "None supplied; only the explicit source cutoff is set.",
        "status": "running",
    }
    destination = output_dir / "benchmark.json"

    def save():
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

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
        for supplier in SUPPLIERS:
            imported = measure(f"{supplier}:import_first", lambda: import_supplier(settings, supplier))
            repeated = measure(f"{supplier}:import_repeat", lambda: import_supplier(settings, supplier))
            assert imported["snapshot_id"] == repeated["snapshot_id"]
            assert imported["totals"] == repeated["totals"]
            assert repeated["reused_files"] == 6
            snapshot_id = imported["snapshot_id"]
            config = configuration_template()
            config["as_of"] = AS_OF
            config_id = save_configuration(settings.database_path, snapshot_id, config)["id"]
            quality = measure(f"{supplier}:quality_first", lambda: run_quality(
                settings.database_path, snapshot_id, config_id))
            quality_again = measure(f"{supplier}:quality_repeat", lambda: run_quality(
                settings.database_path, snapshot_id, config_id))
            assert quality["run_id"] == quality_again["run_id"]
            cleaned = measure(f"{supplier}:cleaning_first", lambda: run_cleaning(
                settings.database_path, snapshot_id, AS_OF, policy="review_only"))
            cleaned_again = measure(f"{supplier}:cleaning_repeat", lambda: run_cleaning(
                settings.database_path, snapshot_id, AS_OF, policy="review_only"))
            assert cleaned["run_id"] == cleaned_again["run_id"]
            sku = quality["skus"][0]["sku"]
            gates = {}
            for gate, operation in (
                ("quality_calculation_input", lambda: calculation_input(settings.database_path, quality["run_id"], sku)),
                ("replenishment_prepared_input", lambda: prepared_input(
                    settings.database_path, quality["run_id"], cleaned["run_id"], sku)),
            ):
                try:
                    operation()
                except ValueError as error:
                    gates[gate] = {"blocked": True, "sku": sku, "reason": str(error)}
                else:
                    raise AssertionError(f"{gate} admitted real inputs without business parameters")
            report["suppliers"][supplier] = {
                "snapshot_id": snapshot_id, "quality_run_id": quality["run_id"],
                "cleaning_run_id": cleaned["run_id"], "same_ids_on_repeat": True,
                "reused_files_on_repeat": repeated["reused_files"], "volumes": imported["totals"],
                "quality_summary": quality["summary"], "cleaning_summary": cleaned["summary"],
                "real_order_gates": gates,
            }
            # Profile versioned report reads separately: these timings include profiling overhead.
            profiler = cProfile.Profile()
            started = perf_counter()
            profiler.enable()
            report_snapshot(settings.database_path, snapshot_id)
            quality_report(settings.database_path, quality["run_id"])
            cleaning_report(settings.database_path, cleaned["run_id"])
            profiler.disable()
            report["phases"].append({"name": f"{supplier}:report_reads_profiled",
                                     "seconds": perf_counter() - started, "profiled": True})
            slug = "systeme" if supplier == "Systeme Electric" else "iek"
            profiler.dump_stats(str(output_dir / f"reports-{slug}.prof"))
            text = io.StringIO()
            pstats.Stats(profiler, stream=text).strip_dirs().sort_stats("cumulative").print_stats(30)
            (output_dir / f"reports-{slug}.txt").write_text(text.getvalue(), encoding="utf-8")
            save()
        with sqlite3.connect(settings.database_path) as connection:
            report["integrity_check"] = connection.execute("PRAGMA integrity_check").fetchone()[0]
            report["foreign_key_errors"] = connection.execute("PRAGMA foreign_key_check").fetchall()
            report["orders_created"] = connection.execute("SELECT COUNT(*) FROM order_projects").fetchone()[0]
            report["replenishment_runs_created"] = connection.execute("SELECT COUNT(*) FROM replenishment_runs").fetchone()[0]
        report["status"] = "complete"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["source_hashes_after"] = _hashes(paths, settings.source_dir)
        report["source_files_unchanged"] = report["source_hashes_before"] == report["source_hashes_after"]
        report["code_hash_after"] = _code_manifest()[0]
        report["code_unchanged"] = report["code_hash_before"] == report["code_hash_after"]
        if not report["source_files_unchanged"] or not report["code_unchanged"]:
            report["status"] = "failed"
            report["error"] = "Sources or implementation changed during the benchmark; rerun in a new directory."
        report["finished_at_utc"] = datetime.now(UTC).isoformat()
        save()
    assert report["source_files_unchanged"] and report["code_unchanged"]
    assert report["integrity_check"] == "ok" and not report["foreign_key_errors"]
    print(f"Evidence: {destination}", flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, help="A new directory; existing paths are rejected")
    parser.add_argument("--source-dir", help="Root containing IEK and Systeme electric (default: checkout)")
    arguments = parser.parse_args()
    benchmark(arguments.output_dir, arguments.source_dir)
