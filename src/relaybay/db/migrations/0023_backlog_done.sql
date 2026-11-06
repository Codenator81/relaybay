-- relaybay:foreign-keys-off
-- Backlog "done": an item fixed without a task of its own is closed with an optional link (a
-- pull request, a commit). SQLite cannot widen the status CHECK in place, so the table is rebuilt
-- (create, copy, drop, rename) with foreign keys off; existing rows keep every value.
CREATE TABLE backlog_item_new (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    body_md           TEXT,
    area              TEXT,
    origin            TEXT NOT NULL CHECK (origin IN ('session', 'review', 'intake', 'owner')),
    origin_task_id    INTEGER REFERENCES task(id) ON DELETE SET NULL,
    origin_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    intake_message_id INTEGER REFERENCES intake_message(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new', 'later', 'planned', 'done', 'declined')),
    decline_reason    TEXT,
    link              TEXT,
    task_id           INTEGER REFERENCES task(id) ON DELETE SET NULL,
    decided_at        TEXT,
    decided_by        TEXT,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL
);
INSERT INTO backlog_item_new (id, project_id, title, body_md, area, origin, origin_task_id,
                              origin_session_id, intake_message_id, status, decline_reason,
                              task_id, decided_at, decided_by, created_at, created_by)
SELECT id, project_id, title, body_md, area, origin, origin_task_id, origin_session_id,
       intake_message_id, status, decline_reason, task_id, decided_at, decided_by, created_at,
       created_by
FROM backlog_item;
DROP TABLE backlog_item;
ALTER TABLE backlog_item_new RENAME TO backlog_item;
CREATE INDEX backlog_item_project ON backlog_item(project_id, status, id);
CREATE INDEX backlog_item_task ON backlog_item(task_id);
CREATE INDEX backlog_item_origin_task ON backlog_item(origin_task_id);
