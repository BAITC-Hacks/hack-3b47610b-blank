"""Schema for immutable source observations and explicitly selected input sets."""

FACT_COLUMNS = {
    "products": "sheet row sku name article unit cell".split(),
    "transactions": (
        "sheet row sku occurred_at document_number document_type unit warehouse quantity state cell"
    ).split(),
    "monthly_values": "sheet row sku period series quantity state cell".split(),
    "measures": "sheet row sku metric number text state cell".split(),
    "seasonal_values": "sheet row period value state cell".split(),
}

SCHEMA_SQL = [
    """CREATE TABLE import_files (
        id INTEGER PRIMARY KEY, source_kind TEXT NOT NULL, supplier TEXT NOT NULL,
        path TEXT NOT NULL, source_name TEXT NOT NULL, sha256 TEXT NOT NULL, imported_at_utc TEXT NOT NULL,
        snapshot_date TEXT, rules_version TEXT NOT NULL, parser_version TEXT NOT NULL,
        UNIQUE(sha256, source_name, rules_version, parser_version))""",
    """CREATE TABLE import_locations (
        file_id INTEGER NOT NULL REFERENCES import_files(id), path TEXT NOT NULL,
        first_seen_utc TEXT NOT NULL, PRIMARY KEY(file_id, path))""",
    """CREATE TABLE import_sheets (
        file_id INTEGER NOT NULL REFERENCES import_files(id), sheet TEXT NOT NULL,
        state TEXT NOT NULL, declared_dimension TEXT, max_row INTEGER NOT NULL,
        max_column INTEGER NOT NULL, PRIMARY KEY(file_id, sheet))""",
    """CREATE TABLE source_rows (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        cells_json TEXT NOT NULL, PRIMARY KEY(file_id, sheet, row),
        FOREIGN KEY(file_id, sheet) REFERENCES import_sheets(file_id, sheet))""",
    """CREATE TABLE products (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        sku TEXT NOT NULL, name TEXT, article TEXT, unit TEXT, cell TEXT NOT NULL,
        PRIMARY KEY(file_id, sheet, row),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    """CREATE TABLE transactions (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        sku TEXT NOT NULL, occurred_at TEXT, document_number TEXT, document_type TEXT,
        unit TEXT, warehouse TEXT, quantity REAL, state TEXT NOT NULL,
        cell TEXT NOT NULL, PRIMARY KEY(file_id, sheet, row),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    """CREATE TABLE monthly_values (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        sku TEXT NOT NULL, period TEXT NOT NULL, series TEXT NOT NULL,
        quantity REAL, state TEXT NOT NULL, cell TEXT NOT NULL,
        PRIMARY KEY(file_id, sheet, row, cell),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    """CREATE TABLE measures (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        sku TEXT NOT NULL, metric TEXT NOT NULL, number REAL, text TEXT,
        state TEXT NOT NULL, cell TEXT NOT NULL,
        PRIMARY KEY(file_id, sheet, row, cell, metric),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    """CREATE TABLE seasonal_values (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        period TEXT NOT NULL, value REAL, state TEXT NOT NULL, cell TEXT NOT NULL,
        PRIMARY KEY(file_id, sheet, row, cell),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    """CREATE TABLE import_issues (
        id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES import_files(id),
        severity TEXT NOT NULL CHECK(severity IN ('error', 'warning')),
        code TEXT NOT NULL, sheet TEXT, row INTEGER, cell TEXT, message TEXT NOT NULL)""",
    """CREATE TABLE snapshots (
        id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE,
        created_at_utc TEXT NOT NULL, code_version TEXT NOT NULL,
        code_manifest_json TEXT NOT NULL, parameter_version TEXT NOT NULL,
        parameters_json TEXT NOT NULL)""",
    """CREATE TABLE snapshot_files (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), source_kind TEXT NOT NULL,
        file_id INTEGER NOT NULL REFERENCES import_files(id),
        PRIMARY KEY(snapshot_id, source_kind), UNIQUE(snapshot_id, file_id))""",
    "CREATE INDEX transactions_sku_date ON transactions(file_id, sku, occurred_at)",
    "CREATE INDEX monthly_sku_period ON monthly_values(file_id, sku, period)",
    "CREATE INDEX measures_sku ON measures(file_id, sku, metric)",
    "CREATE INDEX issues_file ON import_issues(file_id)",
]


EXTRA_FACT_COLUMNS = {
    "incoming_orders": "sheet row sku article order_number order_date eta_deadline quantity state unit cell header_cell".split(),
    "catalog_items": (
        "sheet row article name category group_name subgroup subsubgroup purchase_unit ntin "
        "status packaging pack_quantity order_multiple minimum_order base_price currency price_date states_json cell"
    ).split(),
}

# Keep the exact stage-2 DDL above for validating databases before migration.
MIGRATION_3 = [
    "ALTER TABLE snapshots ADD COLUMN supplier TEXT NOT NULL DEFAULT 'Systeme Electric'",
    "ALTER TABLE import_issues ADD COLUMN sku TEXT",
    """CREATE TABLE row_metadata (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        hidden INTEGER NOT NULL CHECK(hidden IN (0, 1)), PRIMARY KEY(file_id, sheet, row),
        FOREIGN KEY(file_id, sheet) REFERENCES import_sheets(file_id, sheet))""",
    """CREATE TABLE external_sources (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, xml_path TEXT NOT NULL,
        external_target TEXT, price_date TEXT, status TEXT NOT NULL,
        PRIMARY KEY(file_id, sheet),
        FOREIGN KEY(file_id, sheet) REFERENCES import_sheets(file_id, sheet))""",
    """CREATE TABLE incoming_orders (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        sku TEXT NOT NULL, article TEXT, order_number TEXT, order_date TEXT,
        eta_deadline TEXT, quantity REAL, state TEXT NOT NULL, unit TEXT,
        cell TEXT NOT NULL, header_cell TEXT NOT NULL,
        PRIMARY KEY(file_id, sheet, row, cell),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    """CREATE TABLE catalog_items (
        file_id INTEGER NOT NULL, sheet TEXT NOT NULL, row INTEGER NOT NULL,
        article TEXT NOT NULL, name TEXT, category TEXT, group_name TEXT,
        subgroup TEXT, subsubgroup TEXT, purchase_unit TEXT, ntin TEXT, status TEXT,
        packaging TEXT, pack_quantity REAL, order_multiple REAL, minimum_order REAL,
        base_price REAL, currency TEXT, price_date TEXT, states_json TEXT NOT NULL,
        cell TEXT NOT NULL, PRIMARY KEY(file_id, sheet, row),
        FOREIGN KEY(file_id, sheet, row) REFERENCES source_rows(file_id, sheet, row))""",
    "CREATE INDEX catalog_article ON catalog_items(file_id, article)",
    "CREATE INDEX incoming_sku ON incoming_orders(file_id, sku)",
    """CREATE TABLE unit_assessments (
        snapshot_id INTEGER NOT NULL REFERENCES snapshots(id), sku TEXT NOT NULL,
        accounting_unit TEXT, purchase_unit TEXT, proposed_factor REAL,
        status TEXT NOT NULL, details_json TEXT NOT NULL,
        PRIMARY KEY(snapshot_id, sku))""",
    """CREATE TABLE unit_confirmations (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, sku TEXT NOT NULL,
        factor REAL NOT NULL CHECK(factor > 0), accounting_unit TEXT NOT NULL,
        purchase_unit TEXT NOT NULL, archive_units_confirmed INTEGER NOT NULL CHECK(archive_units_confirmed=1),
        reason TEXT NOT NULL, confirmed_by TEXT NOT NULL, created_at_utc TEXT NOT NULL,
        FOREIGN KEY(snapshot_id, sku) REFERENCES unit_assessments(snapshot_id, sku))""",
]


def schema_signature(connection, version=3):
    """Compare the owned table structures before touching an existing database."""
    statements = SCHEMA_SQL + (MIGRATION_3 if version >= 3 else [])
    tables = [statement.split()[2] for statement in statements if statement.startswith("CREATE TABLE")]
    return {
        table: connection.execute(f"PRAGMA table_info({table})").fetchall()
        for table in tables
    }
