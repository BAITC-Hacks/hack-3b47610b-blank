"""Append-only stage-8 forecast runs and their auditable points."""

MIGRATION_6 = [
    """CREATE TABLE forecast_runs (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        quality_run_id INTEGER NOT NULL REFERENCES quality_runs(id),
        cleaning_run_id INTEGER NOT NULL REFERENCES cleaning_runs(id),
        sku TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
        created_at_utc TEXT NOT NULL, as_of TEXT NOT NULL,
        rules_version TEXT NOT NULL, code_version TEXT NOT NULL,
        status TEXT NOT NULL, selected_model TEXT,
        config_json TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    "CREATE INDEX forecast_run_snapshot_sku ON forecast_runs(snapshot_id, sku, id)",
    """CREATE TABLE forecast_points (
        run_id INTEGER NOT NULL REFERENCES forecast_runs(id),
        kind TEXT NOT NULL CHECK(kind IN ('backtest','forecast')),
        period TEXT NOT NULL, model TEXT NOT NULL,
        actual REAL, prediction REAL, payload_json TEXT NOT NULL,
        PRIMARY KEY(run_id, kind, period, model))""",
]
