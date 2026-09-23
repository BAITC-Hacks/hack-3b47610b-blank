"""Independent readiness checks on tiny, versioned SQLite source fixtures."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import sqlite3

import pytest

from hackalem.services.quality import (
    calculation_input,
    quality_report,
    run_quality,
    save_configuration,
)
from hackalem.import_schema import MIGRATION_3, SCHEMA_SQL
from hackalem.storage import SCHEMA_VERSION, initialize_database


AS_OF = "2026-09-22"
JAN = "2025-01-01"
FEB = "2025-02-01"
READY = "Достаточно данных"
SCENARIO = "Сценарный расчёт"
MISSING = "Не хватает данных"


def _seed_store(tmp_path, *, supplier="Systeme Electric", broken_a=False, legacy=False):
    """Seed normal import tables, not mocks of the quality/reconciliation engine."""
    database = tmp_path / "quality.sqlite3"
    if legacy:
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE app_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT INTO app_metadata VALUES ('created_at_utc','2026-09-22T18:00:00+00:00')")
            for statement in SCHEMA_SQL + MIGRATION_3:
                connection.execute(statement)
            connection.execute("PRAGMA user_version=3")
    else:
        initialize_database(database)
    kinds = (
        ["multiples", "transactions", "monthly_stock", "monthly_sales", "seasonality", "current"]
        if supplier == "Systeme Electric" else
        ["minimums", "transactions", "monthly_stock", "monthly_sales", "seasonality", "incoming"]
    )
    files = {}
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for kind in kinds:
            cursor = connection.execute(
                """INSERT INTO import_files
                (source_kind,supplier,path,source_name,sha256,imported_at_utc,snapshot_date,rules_version,parser_version)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (kind, supplier, str(tmp_path / f"{kind}.xlsx"), f"{kind}.xlsx",
                 hashlib.sha256(f"{supplier}:{kind}".encode()).hexdigest(),
                 "2026-09-22T18:00:00+00:00", AS_OF if kind in {"current", "incoming"} else None,
                 "fixture-rules", "fixture-parser"),
            )
            files[kind] = cursor.lastrowid
            connection.execute(
                """INSERT INTO import_sheets
                (file_id,sheet,state,declared_dimension,max_row,max_column)
                VALUES (?,'Data','visible','A1:AZ99',99,52)""", (cursor.lastrowid,),
            )

        def raw(kind, row, sku, cell=None, value=None, state="value"):
            file_id = files[kind]
            existing = connection.execute(
                "SELECT cells_json FROM source_rows WHERE file_id=? AND sheet='Data' AND row=?",
                (file_id, row),
            ).fetchone()
            cells = json.loads(existing[0]) if existing else {
                "A": {"value": sku, "formula": None, "data_type": "s", "number_format": "General"},
                "B": {"value": f"Товар {sku}", "formula": None, "data_type": "s", "number_format": "General"},
            }
            if cell:
                column = "".join(character for character in cell if character.isalpha())
                cells[column] = {
                    "value": "#N/A" if state == "error" else value,
                    "formula": None, "data_type": "e" if state == "error" else "n",
                    "number_format": "General",
                }
            connection.execute(
                """INSERT INTO source_rows(file_id,sheet,row,cells_json) VALUES (?,'Data',?,?)
                ON CONFLICT(file_id,sheet,row) DO UPDATE SET cells_json=excluded.cells_json""",
                (file_id, row, json.dumps(cells, ensure_ascii=False)),
            )
            if not connection.execute(
                "SELECT 1 FROM products WHERE file_id=? AND sku=?", (file_id, sku),
            ).fetchone():
                connection.execute(
                    """INSERT INTO products(file_id,sheet,row,sku,name,article,unit,cell)
                    VALUES (?,'Data',?,?,?,?,'шт',?)""",
                    (file_id, row, sku, f"Товар {sku}", f"ART-{sku}", f"A{row}"),
                )

        def monthly(kind, row, sku, period, quantity, state="value", column="C", series="sales"):
            cell = f"{column}{row}"
            raw(kind, row, sku, cell, quantity, state)
            connection.execute(
                """INSERT INTO monthly_values(file_id,sheet,row,sku,period,series,quantity,state,cell)
                VALUES (?,'Data',?,?,?,?,?,?,?)""",
                (files[kind], row, sku, period, series, quantity, state, cell),
            )

        monthly("monthly_sales", 2, "A", JAN, None if broken_a else 10, "error" if broken_a else "value")
        monthly("monthly_sales", 2, "A", FEB, 20, column="D")
        monthly("monthly_sales", 3, "B", JAN, None, "blank")
        monthly("monthly_sales", 4, "C", JAN, None, "error")
        monthly("monthly_sales", 5, "D", JAN, 0)
        stock_series = "stock" if supplier == "Systeme Electric" else "opening_stock"
        monthly("monthly_stock", 2, "A", JAN, 40, series=stock_series)
        monthly("monthly_stock", 3, "ONLY_STOCK", JAN, 7, series=stock_series)

        for row, period, quantity in [(2, JAN, 12), (3, FEB, 25)]:
            raw("transactions", row, "A", f"H{row}", quantity)
            connection.execute(
                """INSERT INTO transactions
                (file_id,sheet,row,sku,occurred_at,document_number,document_type,unit,warehouse,quantity,state,cell)
                VALUES (?,'Data',?,'A',?,?,'Расходная накладная','шт','Алматы',?,'value',?)""",
                (files["transactions"], row, period[:7] + "-15T10:00:00",
                 f"DOC-{row}", quantity, f"H{row}"),
            )

        if supplier == "Systeme Electric":
            monthly("current", 2, "A", JAN, 11)
            monthly("current", 2, "A", FEB, 21, column="D")
            raw("current", 2, "A", "E2", 100)
            connection.execute(
                """INSERT INTO measures(file_id,sheet,row,sku,metric,number,text,state,cell)
                VALUES (?,'Data',2,'A','stock',100,NULL,'value','E2')""", (files["current"],),
            )
            connection.execute(
                """INSERT INTO import_issues(file_id,severity,code,sheet,row,cell,message,sku)
                VALUES (?,'warning','REPORTED_12_MONTHS_USES_13','Data',2,'AP2',
                'Исходная формула включает 13 месяцев и делит на 12','A')""", (files["current"],),
            )

        minimum_kind = "multiples" if supplier == "Systeme Electric" else "minimums"
        raw(minimum_kind, 2, "A", "C2", 1)
        raw(minimum_kind, 3, "B", "C3", None, "error")
        metric = "order_multiple" if supplier == "Systeme Electric" else "minimum_order"
        for row, sku, quantity, state in [(2, "A", 1, "value"), (3, "B", None, "error")]:
            connection.execute(
                """INSERT INTO measures(file_id,sheet,row,sku,metric,number,text,state,cell)
                VALUES (?,'Data',?,?,?,?,NULL,?,?)""",
                (files[minimum_kind], row, sku, metric, quantity, state, f"C{row}"),
            )
        connection.execute(
            """INSERT INTO import_issues(file_id,severity,code,sheet,row,cell,message,sku)
            VALUES (?,'error','EXCEL_CELL_ERROR','Data',3,'C3','Исходное ограничение содержит #N/A','B')""",
            (files[minimum_kind],),
        )
        cursor = connection.execute(
            """INSERT INTO snapshots(fingerprint,created_at_utc,code_version,code_manifest_json,
            parameter_version,parameters_json,supplier) VALUES (?,?,?,?,?,?,?)""",
            ("fixture-snapshot", "2026-09-22T18:00:00+00:00", "fixture-code", "{}",
             "fixture-parameters", '{"dataset":"real"}', supplier),
        )
        snapshot_id = cursor.lastrowid
        connection.executemany(
            "INSERT INTO snapshot_files(snapshot_id,source_kind,file_id) VALUES (?,?,?)",
            [(snapshot_id, kind, file_id) for kind, file_id in files.items()],
        )
    return database, snapshot_id


def _entry(value, status="confirmed"):
    return {"value": value, "status": status, "reason": "Независимый тестовый эталон", "author": "pytest"}


def _choice(sku="A", source="monthly_sales", start=JAN, end=JAN, status="confirmed"):
    return {
        "sku": sku, "start": start, "end": end, "source": source,
        "scope": "source_report", "status": status,
        "reason": "Явно выбран источник для указанного охвата", "author": "pytest",
    }


def _configuration():
    values = {
        "current_stock": 100, "reserved_stock": 10, "stock_date": AS_OF,
        "lead_time_days": 14, "review_period_days": 7,
        "category_code": "regular", "category_label": "Регулярный ассортимент",
        "stock_policy": {"mode": "stock", "safety_days": 5},
        "minimum_order": 1, "order_multiple": 1,
        "accounting_unit": "шт", "purchase_unit": "шт", "unit_factor": 1,
        "business_growth": 0, "no_open_orders": True,
    }
    return {
        "as_of": AS_OF, "defaults": {name: _entry(value) for name, value in values.items()},
        "skus": {}, "sales_choices": [_choice()],
    }


def _run(database, snapshot_id, payload):
    configuration = save_configuration(database, snapshot_id, payload)
    report = run_quality(database, snapshot_id, configuration["id"])
    return report, configuration


def _sku(report, sku):
    return next(row for row in report["skus"] if row["sku"] == sku)


def test_no_configuration_preserves_union_and_does_not_allow_calculation(tmp_path):
    database, snapshot = _seed_store(tmp_path)

    result = run_quality(database, snapshot)

    assert result["summary"]["sku_count"] == 5
    assert {row["sku"] for row in result["skus"]} == {"A", "B", "C", "D", "ONLY_STOCK"}
    assert all(row["status"] == MISSING for row in result["skus"])
    assert all(row["reasons"] and not row["eligible_for_confirmed_order"] for row in result["skus"])
    with pytest.raises(ValueError):
        calculation_input(database, result["run_id"], "A")


def test_specific_month_choice_overrides_wildcard_without_summing_sources(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    payload = _configuration()
    payload["sales_choices"] = [_choice("*", end=FEB), _choice("A", source="transactions")]

    result, _ = _run(database, snapshot, payload)

    row = _sku(result, "A")
    assert row["status"] == READY
    assert row["eligible_for_calculation"] is True
    assert row["eligible_for_confirmed_order"] is True
    history = calculation_input(database, result["run_id"], "A")["history"]
    assert [(item["period"], item["source"], item["quantity"]) for item in history] == [
        (JAN, "transactions", 12), (FEB, "monthly_sales", 20),
    ]
    assert all(item["choice"] and item["provenance"] for item in history)
    assert any(issue["code"] == "REPORTED_12_MONTHS_USES_13" for issue in result["issues"])


def test_selected_error_does_not_fall_back_to_other_valid_source(tmp_path):
    database, snapshot = _seed_store(tmp_path, broken_a=True)

    result, _ = _run(database, snapshot, _configuration())

    assert _sku(result, "A")["status"] == MISSING
    with pytest.raises(ValueError):
        calculation_input(database, result["run_id"], "A", allow_scenario=True)


def test_confirmed_blank_zero_policy_does_not_convert_errors_or_absent_skus(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    payload = _configuration()
    payload["sales_choices"] = [_choice("*")]
    payload["defaults"]["blank_sales_policy"] = _entry("zero")

    result, _ = _run(database, snapshot, payload)

    assert _sku(result, "B")["status"] == READY
    assert _sku(result, "D")["status"] == READY
    assert _sku(result, "C")["status"] == MISSING
    assert _sku(result, "ONLY_STOCK")["status"] == MISSING
    for sku in ("B", "D"):
        history = calculation_input(database, result["run_id"], sku)["history"]
        assert len(history) == 1
        assert history[0]["quantity"] == 0
    # The manual, confirmed multiple resolves B's blocker without deleting its source issue.
    assert any(issue["code"] == "EXCEL_CELL_ERROR" for issue in result["issues"])


def test_scenario_requires_explicit_opt_in_and_never_becomes_confirmed_order(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    payload = _configuration()
    payload["defaults"]["lead_time_days"] = _entry(14, "scenario")

    result, _ = _run(database, snapshot, payload)

    row = _sku(result, "A")
    assert row["status"] == SCENARIO
    assert row["eligible_for_calculation"] is True
    assert row["eligible_for_confirmed_order"] is False
    assert row["assumptions"]
    with pytest.raises(ValueError):
        calculation_input(database, result["run_id"], "A")
    permitted = calculation_input(database, result["run_id"], "A", allow_scenario=True)
    assert permitted["status"] == SCENARIO
    assert permitted["history"][0]["quantity"] == 10


def test_iek_opening_stock_never_fills_missing_current_stock_even_in_scenario(tmp_path):
    database, snapshot = _seed_store(tmp_path, supplier="IEK")
    payload = _configuration()
    del payload["defaults"]["current_stock"]
    payload["defaults"]["lead_time_days"] = _entry(14, "scenario")

    result, _ = _run(database, snapshot, payload)

    row = _sku(result, "A")
    assert row["status"] == MISSING
    assert row["eligible_for_calculation"] is False
    assert row["eligible_for_confirmed_order"] is False
    with pytest.raises(ValueError):
        calculation_input(database, result["run_id"], "A", allow_scenario=True)


def test_overlapping_choices_at_same_specificity_are_rejected(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    payload = _configuration()
    payload["sales_choices"] = [_choice("A", end=FEB), _choice("A", source="transactions")]

    with pytest.raises(ValueError):
        save_configuration(database, snapshot, payload)


def test_new_configuration_and_run_do_not_rewrite_previous_decisions(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    payload = _configuration()
    saved = save_configuration(database, snapshot, payload)
    payload["sales_choices"][0]["source"] = "transactions"
    first = run_quality(database, snapshot, saved["id"])
    before = deepcopy(calculation_input(database, first["run_id"], "A"))

    second, saved_second = _run(database, snapshot, payload)

    assert saved_second["id"] != saved["id"]
    assert second["run_id"] != first["run_id"]
    assert before["history"][0]["quantity"] == 10
    assert calculation_input(database, second["run_id"], "A")["history"][0]["quantity"] == 12
    assert calculation_input(database, first["run_id"], "A") == before
    assert _sku(quality_report(database, first["run_id"], sku="A"), "A")["status"] == READY


def _append_transaction(database, snapshot, *, occurred_at, quantity):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        file_id = connection.execute(
            "SELECT file_id FROM snapshot_files WHERE snapshot_id=? AND source_kind='transactions'", (snapshot,),
        ).fetchone()[0]
        raw = {
            "A": {"value": occurred_at, "formula": None, "data_type": "s", "number_format": "General"},
            "D": {"value": "A", "formula": None, "data_type": "s", "number_format": "General"},
            "H": {"value": quantity, "formula": None, "data_type": "n", "number_format": "General"},
        }
        connection.execute(
            "INSERT INTO source_rows(file_id,sheet,row,cells_json) VALUES (?,'Data',4,?)", (file_id, json.dumps(raw)),
        )
        connection.execute(
            """INSERT INTO transactions
            (file_id,sheet,row,sku,occurred_at,document_number,document_type,unit,warehouse,quantity,state,cell)
            VALUES (?,'Data',4,'A',?,'EXTRA-DOC','Расходная накладная','шт','Алматы',?,'value','H4')""",
            (file_id, occurred_at, quantity),
        )


@pytest.mark.parametrize(("source", "status"), [("transactions", MISSING), ("monthly_sales", READY)])
def test_underlying_negative_transaction_cannot_hide_in_positive_selected_sum(tmp_path, source, status):
    database, snapshot = _seed_store(tmp_path)
    _append_transaction(database, snapshot, occurred_at="2025-01-20T10:00:00", quantity=-2)
    payload = _configuration()
    payload["sales_choices"] = [_choice(source=source)]

    result, _ = _run(database, snapshot, payload)

    assert _sku(result, "A")["status"] == status
    if status == MISSING:
        with pytest.raises(ValueError):
            calculation_input(database, result["run_id"], "A", allow_scenario=True)
    else:
        assert calculation_input(database, result["run_id"], "A")["history"][0]["quantity"] == 10


@pytest.mark.parametrize(("source", "status"), [("transactions", MISSING), ("monthly_sales", READY)])
def test_undated_transaction_blocks_only_the_selected_source(tmp_path, source, status):
    database, snapshot = _seed_store(tmp_path)
    _append_transaction(database, snapshot, occurred_at=None, quantity=5)
    payload = _configuration()
    payload["sales_choices"] = [_choice(source=source)]

    result, _ = _run(database, snapshot, payload)

    assert _sku(result, "A")["status"] == status
    assert any(issue["code"] == "FACT_PERIOD_INVALID" for issue in result["issues"])
    if status == MISSING:
        with pytest.raises(ValueError):
            calculation_input(database, result["run_id"], "A", allow_scenario=True)
    else:
        assert calculation_input(database, result["run_id"], "A")["history"][0]["quantity"] == 10


def _add_incoming(database, snapshot, *, quantity=5, state="value", row=2, sku="A", unit=None):
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys=ON")
        supplier = connection.execute("SELECT supplier FROM snapshots WHERE id=?", (snapshot,)).fetchone()[0]
        kind = "current" if supplier == "Systeme Electric" else "incoming"
        file_id = connection.execute(
            "SELECT file_id FROM snapshot_files WHERE snapshot_id=? AND source_kind=?", (snapshot, kind),
        ).fetchone()[0]
        cell = f"F{row}"
        existing = connection.execute(
            "SELECT cells_json FROM source_rows WHERE file_id=? AND sheet='Data' AND row=?", (file_id, row),
        ).fetchone()
        raw = json.loads(existing[0]) if existing else {
            "A": {"value": sku, "formula": None, "data_type": "s", "number_format": "General"},
        }
        raw["F"] = {
            "value": "#N/A" if state == "error" else quantity,
            "formula": None, "data_type": "e" if state == "error" else "n", "number_format": "General",
        }
        connection.execute(
            """INSERT INTO source_rows(file_id,sheet,row,cells_json) VALUES (?,'Data',?,?)
            ON CONFLICT(file_id,sheet,row) DO UPDATE SET cells_json=excluded.cells_json""",
            (file_id, row, json.dumps(raw)),
        )
        if kind == "current":
            connection.execute(
                """INSERT INTO measures(file_id,sheet,row,sku,metric,number,text,state,cell)
                VALUES (?,'Data',?,?,'incoming_quantity',?,NULL,?,?)""",
                (file_id, row, sku, quantity, state, cell),
            )
        else:
            connection.execute(
                """INSERT INTO incoming_orders
                (file_id,sheet,row,sku,article,order_number,order_date,eta_deadline,quantity,state,unit,cell,header_cell)
                VALUES (?,'Data',?,?,'ART-A','УТ-TEST','2026-09-20','2026-09-30',?,?,?,?,'F1')""",
                (file_id, row, sku, quantity, state, unit, cell),
            )
    return kind, cell


def _eta(kind, *cells):
    return _entry([
        {"source_kind": kind, "sheet": "Data", "cell": cell, "eta": "2026-09-30",
         "meaning": "deadline" if kind == "incoming" else "expected"}
        for cell in cells
    ])


def test_empty_incoming_requires_explicit_no_open_orders_confirmation(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    payload = _configuration()
    del payload["defaults"]["no_open_orders"]

    result, _ = _run(database, snapshot, payload)

    assert _sku(result, "A")["status"] == MISSING
    with pytest.raises(ValueError):
        calculation_input(database, result["run_id"], "A")


@pytest.mark.parametrize(("confirmed_cells", "no_open_orders", "status"), [
    (["F2", "F3"], False, READY),
    (["F2"], False, MISSING),
    (["F2", "F99"], False, MISSING),
    (["F2", "F3"], True, MISSING),
])
def test_each_positive_incoming_needs_its_own_valid_eta_reference(
    tmp_path, confirmed_cells, no_open_orders, status,
):
    database, snapshot = _seed_store(tmp_path)
    _add_incoming(database, snapshot, row=2, quantity=5)
    _add_incoming(database, snapshot, row=3, quantity=7)
    payload = _configuration()
    payload["defaults"]["no_open_orders"] = _entry(no_open_orders)
    payload["defaults"]["incoming_unit"] = _entry("шт")
    payload["defaults"]["eta_confirmations"] = _eta("current", *confirmed_cells)

    result, _ = _run(database, snapshot, payload)

    assert _sku(result, "A")["status"] == status
    if status == READY:
        incoming = calculation_input(database, result["run_id"], "A")["incoming"]
        assert {item["cell"]: item["accounting_quantity"] for item in incoming} == {"F2": 5, "F3": 7}
    else:
        with pytest.raises(ValueError):
            calculation_input(database, result["run_id"], "A", allow_scenario=True)


def test_no_open_orders_cannot_override_an_incoming_quantity_error(tmp_path):
    database, snapshot = _seed_store(tmp_path)
    _add_incoming(database, snapshot, quantity=None, state="error")

    result, _ = _run(database, snapshot, _configuration())

    assert _sku(result, "A")["status"] == MISSING
    with pytest.raises(ValueError):
        calculation_input(database, result["run_id"], "A", allow_scenario=True)


@pytest.mark.parametrize(("declared_unit", "status"), [("шт", READY), ("компл", MISSING)])
def test_known_accounting_unit_incoming_is_not_multiplied_by_purchase_factor(tmp_path, declared_unit, status):
    database, snapshot = _seed_store(tmp_path, supplier="IEK")
    kind, cell = _add_incoming(database, snapshot, quantity=10, unit="шт")
    payload = _configuration()
    payload["defaults"].update({
        "purchase_unit": _entry("компл"), "unit_factor": _entry(4),
        "incoming_unit": _entry(declared_unit), "no_open_orders": _entry(False),
        "eta_confirmations": _eta(kind, cell),
    })

    result, _ = _run(database, snapshot, payload)

    assert _sku(result, "A")["status"] == status
    if status == READY:
        incoming = calculation_input(database, result["run_id"], "A")["incoming"]
        assert len(incoming) == 1
        assert incoming[0]["accounting_quantity"] == 10
    else:
        with pytest.raises(ValueError):
            calculation_input(database, result["run_id"], "A", allow_scenario=True)


def test_stage3_migration_preserves_imported_facts_and_snapshot_membership(tmp_path):
    database, snapshot = _seed_store(tmp_path, legacy=True)

    def old_contents():
        with closing(sqlite3.connect(database)) as connection:
            tables = [row[0] for row in connection.execute(
                    """SELECT name FROM sqlite_master WHERE type='table'
                        AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'quality_%'
                        AND name NOT LIKE 'cleaning_%' AND name NOT LIKE 'forecast_%' ORDER BY name""",
            )]
            return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall() for table in tables}

    before = old_contents()

    initialized = initialize_database(database)

    assert initialized.schema_version == SCHEMA_VERSION
    assert old_contents() == before
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    result, _ = _run(database, snapshot, _configuration())
    assert calculation_input(database, result["run_id"], "A")["history"][0]["quantity"] == 10
