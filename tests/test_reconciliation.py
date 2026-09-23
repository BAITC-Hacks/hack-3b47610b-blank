"""Independent small-source checks; no business parameters or selected authority."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hackalem.import_schema import SCHEMA_SQL, MIGRATION_3
from hackalem.services.reconciliation import analyze_snapshot


JAN, FEB = "2025-01-01", "2025-02-01"


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.database = Path(self.tmp.name) / "fixture.sqlite3"
        self.db = sqlite3.connect(self.database)
        self.addCleanup(self.db.close)
        for statement in SCHEMA_SQL + MIGRATION_3:
            self.db.execute(statement)
        self.files = {}
        for kind in ("transactions", "monthly_sales", "monthly_stock", "current", "multiples"):
            file_id = self.db.execute(
                "INSERT INTO import_files(source_kind,supplier,path,source_name,sha256,imported_at_utc,snapshot_date,rules_version,parser_version) VALUES (?,?,?,?,?,?,?,?,?)",
                (kind, "Systeme Electric", kind, kind, kind, "2025-01-31", "2025-01-31" if kind == "current" else None, "test", "test"),
            ).lastrowid
            self.files[kind] = file_id
            self.db.execute("INSERT INTO import_sheets VALUES (?,'Data','visible','A1:Z99',99,26)", (file_id,))
        self.db.execute("INSERT INTO snapshots VALUES (1,'fixture','2025-01-31','test','{}','test','{}','Systeme Electric')")
        self.db.executemany("INSERT INTO snapshot_files VALUES (1,?,?)", self.files.items())

    def product(self, kind, row, sku, unit="шт", article=None):
        self.db.execute("INSERT OR IGNORE INTO source_rows VALUES (?,'Data',?,'{}')", (self.files[kind], row))
        self.db.execute("INSERT INTO products VALUES (?,'Data',?,?,?,?,?,?)",
                        (self.files[kind], row, sku, "Товар " + sku, article, unit, f"A{row}"))

    def tx(self, row, sku, quantity, state="value", period=JAN, unit="шт"):
        self.db.execute("INSERT OR IGNORE INTO source_rows VALUES (?,'Data',?,'{}')", (self.files["transactions"], row))
        self.db.execute("INSERT INTO transactions VALUES (?,'Data',?,?,?,?,?,?,?,?,?,?)",
                        (self.files["transactions"], row, sku, period, "SAME-DOCUMENT", "Расходная", unit, "Алматы", quantity, state, f"H{row}"))

    def monthly(self, kind, row, sku, quantity, state="value", period=JAN, cell=None, series="sales"):
        self.db.execute("INSERT OR IGNORE INTO source_rows VALUES (?,'Data',?,'{}')", (self.files[kind], row))
        self.db.execute("INSERT INTO monthly_values VALUES (?,'Data',?,?,?,?,?,?,?)",
                        (self.files[kind], row, sku, period, series, quantity, state, cell or f"C{row}"))

    def measure(self, row, sku, metric, quantity, state="value", cell=None):
        self.db.execute("INSERT OR IGNORE INTO source_rows VALUES (?,'Data',?,'{}')", (self.files["current"], row))
        self.db.execute("INSERT INTO measures VALUES (?,'Data',?,?,?,?,NULL,?,?)",
                        (self.files["current"], row, sku, metric, quantity, state, cell or f"AX{row}"))

    def analyze(self):
        self.db.commit()
        before = hashlib.sha256(self.database.read_bytes()).digest()
        report = analyze_snapshot(self.database, 1)
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).digest())
        return report

    def test_partial_is_not_equal_to_known_subtotal_and_reports_stay_separate(self):
        self.tx(2, "0007", 10)
        self.tx(3, "0007", None, "blank")
        self.tx(4, "0007", None, "error")
        self.monthly("monthly_sales", 2, "0007", 10)
        self.monthly("current", 2, "0007", 0)
        report = self.analyze()
        self.assertEqual([x["sku"] for x in report["skus"]], ["0007"])
        sales = {x["source_kind"]: x for x in report["sales"]}
        observed = sales["transactions"]
        self.assertEqual((observed["quantity"], observed["state"]), (10, "partial"))
        self.assertEqual((observed["value_count"], observed["blank_count"], observed["error_count"]), (1, 1, 1))
        self.assertEqual(observed["warehouses"], ["Алматы"])
        self.assertEqual(observed["document_types"], ["Расходная"])
        self.assertEqual(observed["provenance"]["locations"], [{"sheet": "Data", "rows": [2, 3, 4]}])
        comparison = next(x for x in report["comparisons"] if x["left_source"] == "transactions" and x["right_source"] == "monthly_sales")
        self.assertEqual((comparison["kind"], comparison["difference"]), ("missing_value", None))
        self.assertEqual(sales["monthly_sales"]["quantity"], 10)
        self.assertEqual(sales["current"]["quantity"], 0)

    def test_document_rows_preserved_but_duplicate_monthly_is_ambiguous(self):
        self.tx(2, "A", 10)
        self.tx(3, "A", 10)
        self.tx(4, "A", -4)
        self.monthly("monthly_sales", 2, "A", 8)
        self.monthly("monthly_sales", 3, "A", 8)
        self.monthly("monthly_stock", 5, "ONLY_STOCK", 9, series="stock")
        report = self.analyze()
        sales = {x["source_kind"]: x for x in report["sales"]}
        self.assertEqual((sales["transactions"]["quantity"], sales["transactions"]["negative_count"]), (16, 1))
        self.assertEqual((sales["monthly_sales"]["quantity"], sales["monthly_sales"]["state"]), (None, "ambiguous"))
        self.assertEqual({x["sku"] for x in report["skus"]}, {"A", "ONLY_STOCK"})
        self.assertTrue(any(x["code"] == "DUPLICATE_MONTHLY_VALUE" and x["sku"] == "A" for x in report["issues"]))
        self.assertTrue(any(x["code"] == "SOURCE_QUANTITY_NEGATIVE" for x in report["issues"]))

    def test_zero_blank_absent_month_and_assortment_are_distinct(self):
        self.tx(2, "A", 0)
        self.tx(3, "TX_ONLY", 4, period=FEB)
        self.monthly("monthly_sales", 2, "A", 0)
        self.monthly("monthly_sales", 2, "A", None, "blank", FEB, "D2")
        self.monthly("monthly_sales", 4, "MONTH_ONLY", 4)
        report = self.analyze()
        pairs = {(x["sku"], x["period"]): x for x in report["comparisons"] if x["left_source"] == "transactions" and x["right_source"] == "monthly_sales"}
        self.assertEqual(pairs["A", JAN]["kind"], "equal")
        self.assertEqual(pairs["A", FEB]["kind"], "period_right_only")
        self.assertIsNone(pairs["A", FEB]["left_value"])
        self.assertIsNone(pairs["A", FEB]["right_value"])
        self.assertEqual(pairs["TX_ONLY", FEB]["kind"], "assortment_left_only")
        self.assertEqual(pairs["MONTH_ONLY", JAN]["kind"], "assortment_right_only")
        self.assertEqual(len(pairs), 4)
        blanks = next(x for x in report["sales"] if x["source_kind"] == "monthly_sales" and x["sku"] == "A" and x["period"] == FEB)
        self.assertEqual((blanks["state"], blanks["blank_count"]), ("missing", 1))

    def test_units_cannot_be_numerically_compared_and_mixed_units_not_summed(self):
        self.product("monthly_sales", 2, "A", unit="м")
        self.monthly("monthly_sales", 2, "A", 10)
        self.tx(2, "A", 10, unit="шт")
        self.tx(3, "B", 4, unit="шт")
        self.tx(4, "B", 4, unit="м")
        report = self.analyze()
        comparison = next(x for x in report["comparisons"] if x["sku"] == "A" and x["right_source"] == "monthly_sales")
        self.assertEqual((comparison["kind"], comparison["difference"]), ("unit_conflict", None))
        mixed = next(x for x in report["sales"] if x["sku"] == "B")
        self.assertEqual((mixed["state"], mixed["quantity"]), ("ambiguous", None))

    def test_import_lineage_stock_identity_and_missing_id(self):
        self.measure(2, "A", "stock", 100)
        self.measure(2, "A", "reserved_stock", 25, cell="AY2")
        self.measure(2, "A", "free_stock", 74, cell="AZ2")
        self.monthly("monthly_stock", 2, "A", 90, series="stock")
        self.db.execute("INSERT INTO import_issues(file_id,severity,code,sheet,row,cell,message,sku) VALUES (?,'warning','REPORTED_12_MONTHS_USES_13','Data',2,'AP2','Exact original message','A')", (self.files["current"],))
        self.db.execute("INSERT INTO import_issues(file_id,severity,code,message) VALUES (?,'error','SKU_MISSING','No code original')", (self.files["monthly_sales"],))
        report = self.analyze()
        imported = next(x for x in report["issues"] if x["code"] == "REPORTED_12_MONTHS_USES_13")
        self.assertEqual(imported["message"], "Exact original message")
        self.assertEqual(imported["evidence"]["cell"], "AP2")
        self.assertEqual(imported["evidence"]["origin"], "import")
        self.assertIsNone(next(x for x in report["issues"] if x["code"] == "SKU_MISSING")["sku"])
        self.assertTrue(any(x["code"] == "CURRENT_STOCK_IDENTITY_MISMATCH" for x in report["issues"]))
        self.assertTrue(any(x["code"] == "STOCK_SNAPSHOT_DIFFERENCE" for x in report["issues"]))
        with self.assertRaises(ValueError):
            analyze_snapshot(self.database, 999)


if __name__ == "__main__":
    unittest.main()
