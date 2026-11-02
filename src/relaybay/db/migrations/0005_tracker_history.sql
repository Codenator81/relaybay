-- Every tracker issue a task has had: a new key appends a row, the work-log mark stamps the
-- current row. The task's own tracker_* columns stay the current issue.
CREATE TABLE tracker_entry (
    id           INTEGER PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    tracker_name TEXT,
    tracker_ref  TEXT NOT NULL,
    tracker_url  TEXT,
    logged_at    TEXT,
    unlogged_at  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX tracker_entry_task ON tracker_entry(task_id, id);

INSERT INTO tracker_entry (task_id, tracker_name, tracker_ref, tracker_url, logged_at, created_at)
SELECT id, tracker_name, tracker_ref, tracker_url,
       CASE WHEN tracker_logged THEN updated_at END, updated_at
FROM task WHERE tracker_ref IS NOT NULL;
