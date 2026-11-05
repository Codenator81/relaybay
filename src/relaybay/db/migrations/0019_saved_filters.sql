-- A named board query of one project (stage, waiting on, session, text), shared on this machine.
CREATE TABLE saved_filter (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    stage      TEXT,
    waiting_on TEXT,
    session    TEXT,
    q          TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, name)
);
