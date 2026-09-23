"""End-to-end CLI checks with a tiny synthetic database, never production inputs."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from hackalem.config import PROJECT_ROOT
from hackalem.storage import initialize_database


class QualityCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name) / "runtime"
        self.database = self.runtime / "hackalem.sqlite3"
        initialize_database(self.database)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for file_id, kind in ((1, "monthly_sales"), (2, "transactions")):
                connection.execute(
                    "INSERT INTO import_files VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (file_id, kind, "Systeme Electric", f"synthetic/{kind}.xlsx", kind, kind, "2026-09-22", None, "test", "test"),
                )
                connection.execute("INSERT INTO import_sheets VALUES (?,'Data','visible','A1:H3',3,8)", (file_id,))
                for row in (2, 3):
                    connection.execute("INSERT INTO source_rows VALUES (?,'Data',?,'{}')", (file_id, row))
                connection.execute("INSERT INTO products VALUES (?,'Data',2,'0007','Синтетический товар','ART7','шт','A2')", (file_id,))
            connection.execute("INSERT INTO monthly_values VALUES (1,'Data',2,'0007','2025-01-01','sales',10,'value','C2')")
            connection.execute("INSERT INTO monthly_values VALUES (1,'Data',2,'0007','2025-02-01','sales',20,'value','D2')")
            connection.execute("INSERT INTO transactions VALUES (2,'Data',2,'0007','2025-01-15','DOC1','Расходная','шт','Тестовый склад',12,'value','H2')")
            connection.execute("INSERT INTO transactions VALUES (2,'Data',3,'0007','2025-02-15','DOC2','Расходная','шт','Тестовый склад',25,'value','H3')")
            connection.execute("INSERT INTO snapshots VALUES (1,'synthetic','2026-09-22','test','{}','test','{\"dataset\":\"synthetic\"}','Systeme Electric')")
            connection.executemany("INSERT INTO snapshot_files VALUES (1,?,?)", [("monthly_sales", 1), ("transactions", 2)])
        self.environ = {**os.environ, "HACKALEM_DATA_DIR": str(self.runtime),
                        "HACKALEM_SOURCE_DIR": str(Path(self.tmp.name) / "synthetic-sources"),
                        "PYTHONPATH": str(PROJECT_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}

    def command(self, *args):
        return subprocess.run([sys.executable, "-m", "hackalem", *map(str, args)],
                              cwd=PROJECT_ROOT, env=self.environ, capture_output=True,
                              text=True, encoding="utf-8", timeout=30)

    def counts(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                         for table in ("quality_configurations", "quality_runs"))

    def source_fingerprint(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
                    for table in ("import_files", "source_rows", "products", "transactions", "monthly_values", "snapshots", "snapshot_files")}

    def test_configure_quality_report_round_trip_keeps_leading_zero_and_sources(self):
        before = self.source_fingerprint()
        raw = {"current_stock": 100, "reserved_stock": 10, "stock_date": "2026-09-22",
               "lead_time_days": 14, "review_period_days": 7, "category_code": "regular",
               "category_label": "Регулярный", "stock_policy": {"mode": "stock", "safety_days": 5},
               "minimum_order": 1, "order_multiple": 1, "accounting_unit": "шт", "purchase_unit": "шт",
               "unit_factor": 1, "business_growth": 0, "no_open_orders": True}
        metadata = {"status": "confirmed", "reason": "Синтетический эталон CLI", "author": "test"}
        payload = {"as_of": "2026-09-22", "defaults": {key: {"value": value, **metadata} for key, value in raw.items()},
                   "skus": {}, "sales_choices": [{"sku": "0007", "start": "2025-01-01", "end": "2025-02-01",
                                                      "source": "monthly_sales", "scope": "source_report", **metadata}]}
        configuration_path = Path(self.tmp.name) / "параметры с BOM.json"
        configuration_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8-sig")
        saved = self.command("configure", "--snapshot", 1, "--file", configuration_path)
        self.assertEqual(saved.returncode, 0, saved.stderr)
        configuration_id = json.loads(saved.stdout)["id"]
        self.assertEqual(self.counts(), (1, 0))
        quality = self.command("quality", "--snapshot", 1, "--config", configuration_id)
        self.assertEqual(quality.returncode, 0, quality.stderr)
        result = json.loads(quality.stdout)
        self.assertEqual(result["snapshot_id"], 1)
        run_id = result["run_id"]
        self.assertEqual(self.counts(), (1, 1))
        inspected = self.command("quality-report", "--run", run_id, "--sku", "0007", "--limit", 1)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        report = json.loads(inspected.stdout)
        self.assertEqual(report["skus"][0]["sku"], "0007")
        self.assertEqual(report["skus"][0]["status"], "Достаточно данных")
        self.assertEqual([x["quantity"] for x in report["selected_sales"]], [10, 20])
        self.assertEqual([x["source"] for x in report["selected_sales"]], ["monthly_sales", "monthly_sales"])
        self.assertTrue(all(x["provenance"]["file_id"] == 1 for x in report["selected_sales"]))
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(before, self.source_fingerprint())

    def test_invalid_json_and_unknown_config_fail_without_quality_records(self):
        configuration_path = Path(self.tmp.name) / "bad.json"
        configuration_path.write_text("{bad", encoding="utf-8")
        malformed = self.command("configure", "--snapshot", 1, "--file", configuration_path)
        self.assertEqual(malformed.returncode, 1)
        self.assertIn("Ошибка:", malformed.stderr)
        self.assertEqual(self.counts(), (0, 0))
        missing = self.command("quality", "--snapshot", 1, "--config", 999)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("не найдена", missing.stderr)
        self.assertEqual(self.counts(), (0, 0))
        limit = self.command("quality-report", "--run", 1, "--limit", 0)
        self.assertEqual(limit.returncode, 1)
        self.assertIn("положительным", limit.stderr)
        self.assertEqual(self.counts(), (0, 0))


if __name__ == "__main__":
    unittest.main()
