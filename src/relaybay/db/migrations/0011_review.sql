-- relaybay:foreign-keys-off
-- The `review` stage sits between dev and test. SQLite cannot change a CHECK constraint in place,
-- so the task table is rebuilt (create, copy, drop, rename) with foreign keys off: with them on,
-- dropping the old table would cascade into reports, comments and bindings. The migration runner
-- checks `PRAGMA foreign_key_check` before it commits.
CREATE TABLE task_new (
    id               INTEGER PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    key              TEXT NOT NULL UNIQUE,
    title            TEXT NOT NULL,
    stage            TEXT NOT NULL DEFAULT 'plan'
                     CHECK (stage IN ('plan', 'dev', 'review', 'test', 'live', 'done')),
    stage_note       TEXT,
    waiting_on       TEXT,
    next_step        TEXT,
    branch           TEXT,
    doc_path         TEXT,
    session_id       INTEGER REFERENCES session(id) ON DELETE SET NULL,
    priority         INTEGER NOT NULL DEFAULT 0,
    version          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    closed_at        TEXT,
    description      TEXT,
    progress         TEXT,
    tracker_ref      TEXT,
    tracker_name     TEXT,
    tracker_url      TEXT,
    tracker_logged   INTEGER NOT NULL DEFAULT 0,
    next_step_at     TEXT,
    stage_changed_at TEXT
);
INSERT INTO task_new (id, project_id, key, title, stage, stage_note, waiting_on, next_step, branch,
                      doc_path, session_id, priority, version, created_at, updated_at, closed_at,
                      description, progress, tracker_ref, tracker_name, tracker_url,
                      tracker_logged, next_step_at, stage_changed_at)
SELECT id, project_id, key, title, stage, stage_note, waiting_on, next_step, branch, doc_path,
       session_id, priority, version, created_at, updated_at, closed_at, description, progress,
       tracker_ref, tracker_name, tracker_url, tracker_logged, next_step_at, stage_changed_at
FROM task;
DROP TABLE task;
ALTER TABLE task_new RENAME TO task;
CREATE INDEX task_project_stage ON task(project_id, stage);

-- The acceptance checklist of a task: ordered steps the owner accepts or rejects on the board.
CREATE TABLE review_step (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    position   INTEGER NOT NULL,
    title      TEXT NOT NULL,
    details_md TEXT,
    status     TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'accepted', 'rejected')),
    decided_at TEXT,
    decided_by TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (task_id, position)
);

-- Screenshots of a task, optionally of one step. The file lives under the data directory at
-- attachments/<project>/<task key>/<stored_name>; the database keeps only the stored name.
CREATE TABLE attachment (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    step_id       INTEGER REFERENCES review_step(id) ON DELETE SET NULL,
    kind          TEXT NOT NULL DEFAULT 'image' CHECK (kind IN ('image')),
    stored_name   TEXT NOT NULL UNIQUE,
    original_name TEXT,
    mime          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    caption_md    TEXT,
    created_by    TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX attachment_task ON attachment(task_id, id);

-- An owner comment can be about one step or one screenshot (a review decision is such a comment).
ALTER TABLE comment ADD COLUMN step_id INTEGER REFERENCES review_step(id) ON DELETE SET NULL;
ALTER TABLE comment ADD COLUMN attachment_id INTEGER REFERENCES attachment(id) ON DELETE SET NULL;
