"""Additive stage-4 tables; imported observations stay immutable."""

MIGRATION_4 = [
    """CREATE TABLE quality_configurations (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        fingerprint TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL,
        payload_json TEXT NOT NULL)""",
    """CREATE TABLE quality_runs (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        configuration_id INTEGER NOT NULL REFERENCES quality_configurations(id),
        fingerprint TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL,
        rules_version TEXT NOT NULL, code_version TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    """CREATE TABLE quality_skus (
        run_id INTEGER NOT NULL REFERENCES quality_runs(id), sku TEXT NOT NULL,
        name TEXT, status TEXT NOT NULL, eligible_for_calculation INTEGER NOT NULL,
        eligible_for_confirmed_order INTEGER NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY(run_id, sku))""",
    """CREATE TABLE quality_selected_sales (
        run_id INTEGER NOT NULL, sku TEXT NOT NULL, period TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(run_id, sku, period),
        FOREIGN KEY(run_id, sku) REFERENCES quality_skus(run_id, sku))""",
    """CREATE TABLE quality_comparisons (
        run_id INTEGER NOT NULL REFERENCES quality_runs(id), ordinal INTEGER NOT NULL,
        sku TEXT NOT NULL, period TEXT NOT NULL, kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(run_id, ordinal))""",
    "CREATE INDEX quality_comparison_sku ON quality_comparisons(run_id, sku, kind)",
    """CREATE TABLE quality_issues (
        run_id INTEGER NOT NULL REFERENCES quality_runs(id), ordinal INTEGER NOT NULL,
        sku TEXT, code TEXT NOT NULL, severity TEXT NOT NULL, message TEXT NOT NULL,
        evidence_json TEXT NOT NULL, PRIMARY KEY(run_id, ordinal))""",
    "CREATE INDEX quality_issue_sku ON quality_issues(run_id, sku)",
]
