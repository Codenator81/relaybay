-- The day time log: marks (start, switch, pause) on a card or a free label, and the minutes
-- entered in the external tracker as an append-only ledger (a correction is a negative row).
-- ON DELETE SET NULL keeps past days after a purge or a project deletion: the rows keep the
-- card's key. `at` is UTC ISO with milliseconds, like every other time. Additive.
CREATE TABLE time_mark (
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('start', 'pause', 'switch')),
    task_id    INTEGER REFERENCES task(id) ON DELETE SET NULL,
    task_key   TEXT,
    project_id INTEGER REFERENCES project(id) ON DELETE SET NULL,
    label      TEXT,
    actor      TEXT NOT NULL,
    session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    CHECK ((kind = 'pause' AND task_key IS NULL)
        OR (kind <> 'pause' AND ((task_key IS NULL) <> (label IS NULL))))
);
CREATE INDEX time_mark_at ON time_mark(at, id);
CREATE INDEX time_mark_task ON time_mark(task_id);
CREATE TABLE tracker_worklog (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER REFERENCES task(id) ON DELETE SET NULL,
    task_key   TEXT NOT NULL,
    project_id INTEGER REFERENCES project(id) ON DELETE SET NULL,
    day        TEXT NOT NULL,
    minutes    INTEGER NOT NULL CHECK (minutes <> 0 AND minutes BETWEEN -1440 AND 1440),
    ref        TEXT,
    reason     TEXT,
    actor      TEXT NOT NULL,
    session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX tracker_worklog_task ON tracker_worklog(task_id, day);
