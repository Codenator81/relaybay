-- Per-project override of the working rules: a partial rules document (JSON) merged on top of
-- the global rules. Read-only for tools for now; validated when the server starts.
CREATE TABLE project_rules (
    project_id INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    body       TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
