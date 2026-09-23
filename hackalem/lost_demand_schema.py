"""Append-only history of availability evidence and lost-demand estimates."""

MIGRATION_6 = [
    """CREATE TABLE lost_demand_runs (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        cleaning_run_id INTEGER NOT NULL REFERENCES cleaning_runs(id), sku TEXT NOT NULL,
        fingerprint TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL,
        as_of TEXT NOT NULL, rules_version TEXT NOT NULL, code_version TEXT NOT NULL,
        evidence_json TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    """CREATE TABLE lost_demand_days (
        run_id INTEGER NOT NULL REFERENCES lost_demand_runs(id), day TEXT NOT NULL,
        state TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id, day))""",
    """CREATE TABLE lost_demand_months (
        run_id INTEGER NOT NULL REFERENCES lost_demand_runs(id), period TEXT NOT NULL,
        state TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(run_id, period))""",
]
