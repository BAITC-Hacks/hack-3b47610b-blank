"""Append-only calculation runs with the exact scenario inputs and results."""

MIGRATION_8 = [
    """CREATE TABLE replenishment_runs (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        quality_run_id INTEGER NOT NULL REFERENCES quality_runs(id),
        cleaning_run_id INTEGER NOT NULL REFERENCES cleaning_runs(id),
        fingerprint TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL,
        as_of TEXT NOT NULL, rules_version TEXT NOT NULL, code_version TEXT NOT NULL,
        input_json TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    """CREATE TABLE replenishment_items (
        run_id INTEGER NOT NULL REFERENCES replenishment_runs(id), sku TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(run_id, sku))""",
]
