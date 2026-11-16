-- relaybay:foreign-keys-off
-- Wakes of the coordinator (design 0023): each wake is one turn of the coordinator run whose text
-- lists, as data, the events it listens to since the last wake. A row links that turn to the
-- range of events it carried (`first_event_id`..`last_event_id`, `count` of them; a wake shows at
-- most 50 lines and names the rest). The cursor, the last event handled, is an app_setting.
-- `returned_at`: its turn ended without a result (cancelled, lost, failed to start, stopped), so
-- its range went back into a later wake.
CREATE TABLE coordinator_wake (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    turn_id        INTEGER REFERENCES run_turn(id) ON DELETE SET NULL,
    first_event_id INTEGER NOT NULL,
    last_event_id  INTEGER NOT NULL CHECK (last_event_id >= first_event_id),
    count          INTEGER NOT NULL CHECK (count > 0),
    created_at     TEXT NOT NULL,
    returned_at    TEXT
);
CREATE INDEX coordinator_wake_run ON coordinator_wake(run_id, id);
CREATE UNIQUE INDEX coordinator_wake_turn ON coordinator_wake(turn_id) WHERE turn_id IS NOT NULL;

-- `question.overdue` is written once per question that passed its deadline unanswered.
ALTER TABLE question ADD COLUMN overdue_at TEXT;

-- A `/compact` turn's `compact_boundary`: the context before and, when the CLI says, after.
ALTER TABLE run_turn ADD COLUMN compact_pre_tokens INTEGER;
ALTER TABLE run_turn ADD COLUMN compact_post_tokens INTEGER;
-- The context the turn ended with: input + cache read + cache write of its last assistant message.
ALTER TABLE run_turn ADD COLUMN context_tokens INTEGER;

-- A budget of one session (the coordinator run's own): `scope` 'session' with `session_id`.
-- SQLite cannot widen a CHECK in place, so the table is rebuilt with foreign keys off; every
-- budget keeps its id, its marks and the brakes that name it.
CREATE TABLE token_budget_new (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL CHECK (scope IN ('all', 'project', 'task', 'session')),
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    task_id    INTEGER REFERENCES task(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES session(id) ON DELETE CASCADE,
    period     TEXT NOT NULL CHECK (period IN ('day', 'total')),
    max_tokens INTEGER NOT NULL CHECK (max_tokens > 0),
    action     TEXT NOT NULL DEFAULT 'warn' CHECK (action IN ('warn', 'stop')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    CHECK ((scope = 'session') = (session_id IS NOT NULL))
);
INSERT INTO token_budget_new (id, scope, project_id, task_id, period, max_tokens, action,
                              updated_at, updated_by)
SELECT id, scope, project_id, task_id, period, max_tokens, action, updated_at, updated_by
FROM token_budget;
DROP TABLE token_budget;
ALTER TABLE token_budget_new RENAME TO token_budget;
CREATE UNIQUE INDEX token_budget_all ON token_budget(scope) WHERE scope = 'all';
CREATE UNIQUE INDEX token_budget_project ON token_budget(project_id) WHERE scope = 'project';
CREATE UNIQUE INDEX token_budget_task ON token_budget(task_id) WHERE scope = 'task';
CREATE UNIQUE INDEX token_budget_session ON token_budget(session_id) WHERE scope = 'session';
