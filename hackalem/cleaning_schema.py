"""Append-only regular-demand preparation, separate from source observations."""

MIGRATION_5 = [
    """CREATE TABLE cleaning_runs (
        id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
        fingerprint TEXT NOT NULL UNIQUE, created_at_utc TEXT NOT NULL,
        as_of TEXT NOT NULL, rules_version TEXT NOT NULL, code_version TEXT NOT NULL,
        policy TEXT NOT NULL, decisions_json TEXT NOT NULL, summary_json TEXT NOT NULL)""",
    """CREATE TABLE cleaning_documents (
        run_id INTEGER NOT NULL REFERENCES cleaning_runs(id), document_key TEXT NOT NULL,
        sku TEXT NOT NULL, period TEXT, status TEXT NOT NULL,
        payload_json TEXT NOT NULL, PRIMARY KEY(run_id, document_key))""",
    "CREATE INDEX cleaning_document_sku ON cleaning_documents(run_id, sku, period, status)",
    """CREATE TABLE cleaning_months (
        run_id INTEGER NOT NULL REFERENCES cleaning_runs(id), sku TEXT NOT NULL,
        period TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL,
        PRIMARY KEY(run_id, sku, period))""",
    """CREATE TABLE cleaning_decisions (
        run_id INTEGER NOT NULL, document_key TEXT NOT NULL,
        action TEXT NOT NULL, author TEXT NOT NULL, reason TEXT NOT NULL,
        project_commitment_quantity REAL NOT NULL,
        PRIMARY KEY(run_id, document_key),
        FOREIGN KEY(run_id, document_key) REFERENCES cleaning_documents(run_id, document_key))""",
]
