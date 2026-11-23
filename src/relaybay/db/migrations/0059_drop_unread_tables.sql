-- relaybay:foreign-keys-off
-- Drops what 0.12 (design 0025) left in the file unread and unwritten, after 0058 dropped the run
-- tables: schedules (schedule), usage and budgets (usage_file, usage_day, usage_message,
-- usage_task_day, token_budget, budget_mark), the brake (brake), session messages
-- (session_message), intake (intake_source, intake_message, intake_thread) and the columns
-- project.autonomy, project.run_retention, session.transcript_path, task.autonomy,
-- task.answer_received_at and task.answer_message_id.
-- Kept: backlog_item.intake_message_id (shown on an item as "message #N"; a plain number now)
-- and the run columns of question and approval (a CHECK of question uses run_id).
-- SQLite cannot drop a column that has a foreign key or a CHECK, so task and backlog_item are
-- rebuilt (create, copy, drop, rename) with foreign keys off, as 0045 did; every other column,
-- CHECK, index and value is kept. The backup before a new version runs before this migration.
DROP TABLE schedule;
DROP TABLE usage_task_day;
DROP TABLE usage_message;
DROP TABLE usage_day;
DROP TABLE usage_file;
DROP TABLE budget_mark;
DROP TABLE token_budget;
DROP TABLE brake;
DROP TABLE session_message;
DROP TABLE intake_thread;
DROP TABLE intake_message;
DROP TABLE intake_source;

ALTER TABLE project DROP COLUMN autonomy;
ALTER TABLE project DROP COLUMN run_retention;
ALTER TABLE session DROP COLUMN transcript_path;

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
    stage_changed_at TEXT,
    size             TEXT CHECK (size IS NULL OR size IN ('S', 'M', 'L')),
    plan_status      TEXT CHECK (plan_status IS NULL OR plan_status IN ('draft', 'accepted')),
    plan_accepted_at TEXT,
    plan_accepted_by TEXT,
    rank             INTEGER NOT NULL DEFAULT 0,
    archived_at      TEXT,
    archived_by      TEXT,
    unarchived_at    TEXT,
    area_id          INTEGER REFERENCES project_area(id) ON DELETE SET NULL,
    archived_from    TEXT CHECK (archived_from IN ('live'))
);
INSERT INTO task_new (id, project_id, key, title, stage, stage_note, waiting_on, next_step,
                      branch, doc_path, session_id, priority, version, created_at, updated_at,
                      closed_at, description, progress, tracker_ref, tracker_name, tracker_url,
                      tracker_logged, next_step_at, stage_changed_at, size, plan_status,
                      plan_accepted_at, plan_accepted_by, rank, archived_at, archived_by,
                      unarchived_at, area_id, archived_from)
SELECT id, project_id, key, title, stage, stage_note, waiting_on, next_step,
       branch, doc_path, session_id, priority, version, created_at, updated_at,
       closed_at, description, progress, tracker_ref, tracker_name, tracker_url,
       tracker_logged, next_step_at, stage_changed_at, size, plan_status,
       plan_accepted_at, plan_accepted_by, rank, archived_at, archived_by,
       unarchived_at, area_id, archived_from
FROM task;
DROP TABLE task;
ALTER TABLE task_new RENAME TO task;
CREATE INDEX task_project_stage ON task(project_id, stage);
CREATE INDEX task_board_order ON task(project_id, stage, rank);
CREATE INDEX task_area ON task(area_id) WHERE area_id IS NOT NULL;

CREATE TABLE backlog_item_new (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    body_md           TEXT,
    area              TEXT,
    origin            TEXT NOT NULL CHECK (origin IN ('session', 'review', 'intake', 'owner')),
    origin_task_id    INTEGER REFERENCES task(id) ON DELETE SET NULL,
    origin_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    intake_message_id INTEGER,
    status            TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new', 'later', 'planned', 'done', 'declined')),
    decline_reason    TEXT,
    link              TEXT,
    task_id           INTEGER REFERENCES task(id) ON DELETE SET NULL,
    decided_at        TEXT,
    decided_by        TEXT,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    priority          INTEGER NOT NULL DEFAULT 0,
    assignee          TEXT,
    kind              TEXT CHECK (kind IN ('bug', 'idea', 'question', 'debt')),
    location          TEXT,
    origin_text       TEXT,
    triage_note       TEXT,
    duplicate_of      INTEGER REFERENCES backlog_item(id) ON DELETE SET NULL,
    found_on          TEXT,
    seen_at           TEXT
);
INSERT INTO backlog_item_new (id, project_id, title, body_md, area, origin, origin_task_id,
                              origin_session_id, intake_message_id, status, decline_reason, link,
                              task_id, decided_at, decided_by, created_at, created_by, priority,
                              assignee, kind, location, origin_text, triage_note, duplicate_of,
                              found_on, seen_at)
SELECT id, project_id, title, body_md, area, origin, origin_task_id,
       origin_session_id, intake_message_id, status, decline_reason, link,
       task_id, decided_at, decided_by, created_at, created_by, priority,
       assignee, kind, location, origin_text, triage_note, duplicate_of,
       found_on, seen_at
FROM backlog_item;
DROP TABLE backlog_item;
ALTER TABLE backlog_item_new RENAME TO backlog_item;
CREATE INDEX backlog_item_project ON backlog_item(project_id, status, id);
CREATE INDEX backlog_item_task ON backlog_item(task_id);
CREATE INDEX backlog_item_origin_task ON backlog_item(origin_task_id);
CREATE INDEX backlog_item_queue ON backlog_item(project_id, status, priority DESC, id);
CREATE INDEX backlog_item_duplicate ON backlog_item(duplicate_of);
