"""Append-only stage-8 forecast runs and their auditable points."""

MIGRATION_7 = [
    """CREATE TABLE forecast_runs (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        quality_run_id INTEGER NOT NULL REFERENCES quality_runs(id),
        cleaning_run_id INTEGER NOT NULL REFERENCES cleaning_runs(id),
        sku TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
        created_at_utc TEXT NOT NULL, as_of TEXT NOT NULL,
        rules_version TEXT NOT NULL, code_version TEXT NOT NULL,
        status TEXT NOT NULL, selected_model TEXT,
        config_json TEXT NOT NULL, summary_json TEXT NOT NULL,
        lost_demand_run_id INTEGER REFERENCES lost_demand_runs(id))""",
    "CREATE INDEX forecast_run_snapshot_sku ON forecast_runs(snapshot_id, sku, id)",
    """CREATE TABLE forecast_points (
        run_id INTEGER NOT NULL REFERENCES forecast_runs(id),
        kind TEXT NOT NULL CHECK(kind IN ('backtest','forecast')),
        period TEXT NOT NULL, model TEXT NOT NULL,
        actual REAL, prediction REAL, payload_json TEXT NOT NULL,
        PRIMARY KEY(run_id, kind, period, model))""",
]

# Stage 8 was briefly published as schema 6 before the parallel stage-7 work
# was merged. Keep its exact shape so those local databases can be upgraded.
LEGACY_MIGRATION_6 = [
    MIGRATION_7[0].replace(
        ",\n        lost_demand_run_id INTEGER REFERENCES lost_demand_runs(id)", ""
    ),
    *MIGRATION_7[1:],
]

LINK_LOST_DEMAND = [
    "ALTER TABLE forecast_runs ADD COLUMN lost_demand_run_id INTEGER REFERENCES lost_demand_runs(id)"
]
