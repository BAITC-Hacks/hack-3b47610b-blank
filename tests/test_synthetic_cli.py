"""CLI synthetic workflow must bypass the configured real database entirely."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from hackalem.config import PROJECT_ROOT


class SyntheticCliTests(unittest.TestCase):
    def test_generate_and_report_do_not_open_or_change_real_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real_dir = root / "real-runtime"
            real_dir.mkdir()
            real_database = real_dir / "hackalem.sqlite3"
            # This is intentionally not SQLite: trying normal bootstrap on it
            # would fail rather than silently allowing the wrong data path.
            sentinel = b"REAL DATABASE SENTINEL -- MUST NOT BE OPENED AS SQLITE\x00"
            real_database.write_bytes(sentinel)
            output_root = root / "synthetic-output"
            environment = {**os.environ, "HACKALEM_DATA_DIR": str(real_dir),
                           "HACKALEM_SOURCE_DIR": str(root / "unavailable-sources"),
                           "PYTHONPATH": str(PROJECT_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}

            def command(*arguments):
                result = subprocess.run([sys.executable, "-m", "hackalem", *map(str, arguments)],
                                        cwd=PROJECT_ROOT, env=environment, capture_output=True,
                                        text=True, encoding="utf-8", timeout=90)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(real_database.read_bytes(), sentinel)
                self.assertEqual(sorted(path.name for path in real_dir.iterdir()), ["hackalem.sqlite3"])
                return json.loads(result.stdout)

            created = command("synthetic-generate", "--output-root", output_root, "--seed", 20260923)
            dataset_dir = Path(created["dataset_dir"])
            self.assertEqual(dataset_dir.parent, output_root)
            self.assertEqual(created["dataset_id"], "synthetic-1-seed-20260923")
            self.assertEqual(Path(created["database_path"]), dataset_dir / "model/hackalem.sqlite3")
            self.assertNotEqual(Path(created["database_path"]), real_database)
            self.assertEqual(created["manifest"]["dataset_kind"], "synthetic")
            self.assertEqual(created["manifest"]["label"], "СИНТЕТИЧЕСКИЙ ПРОВЕРОЧНЫЙ НАБОР")
            self.assertEqual(len(created["snapshots"]), 2)
            self.assertEqual({row["supplier"] for row in created["snapshots"]}, {"IEK", "Systeme Electric"})
            self.assertEqual(sum(row["sku_count"] for row in created["snapshots"]), 17)
            reported = command("synthetic-report", "--dataset", dataset_dir)
            self.assertEqual(reported["dataset_id"], created["dataset_id"])
            self.assertEqual(reported["counts"], created["counts"])
            self.assertEqual(reported["snapshots"], created["snapshots"])
            self.assertEqual(reported["validation"]["integrity"], "ok")
            self.assertEqual(reported["validation"]["artifact_hashes"], "ok")
            self.assertEqual(reported["validation"]["scenario_count"], 17)


if __name__ == "__main__":
    unittest.main()
