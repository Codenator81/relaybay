-- The brake and token budgets. Additive: four new tables. Budgets of a task count from this
-- migration on: usage read before it has no task, and it is not backfilled.

-- One row per target. The action is `warn` unless the owner opts into `stop` for this budget.
CREATE TABLE token_budget (
    id         INTEGER PRIMARY KEY,
    scope      TEXT NOT NULL CHECK (scope IN ('all', 'project', 'task')),
    project_id INTEGER REFERENCES project(id) ON DELETE CASCADE,
    task_id    INTEGER REFERENCES task(id) ON DELETE CASCADE,
    period     TEXT NOT NULL CHECK (period IN ('day', 'total')),
    max_tokens INTEGER NOT NULL CHECK (max_tokens > 0),
    action     TEXT NOT NULL DEFAULT 'warn' CHECK (action IN ('warn', 'stop')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE UNIQUE INDEX token_budget_all ON token_budget(scope) WHERE scope = 'all';
CREATE UNIQUE INDEX token_budget_project ON token_budget(project_id) WHERE scope = 'project';
CREATE UNIQUE INDEX token_budget_task ON token_budget(task_id) WHERE scope = 'task';

-- A released row stays as the history. `session_id` goes NULL only on a released row: an open
-- brake moves with the session when a guessed row is merged away.
CREATE TABLE brake (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('all', 'project', 'session', 'task')),
    project_id  INTEGER REFERENCES project(id) ON DELETE CASCADE,
    session_id  INTEGER REFERENCES session(id) ON DELETE SET NULL,
    task_id     INTEGER REFERENCES task(id) ON DELETE CASCADE,
    reason      TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('owner', 'coordinator', 'budget')),
    budget_id   INTEGER REFERENCES token_budget(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    released_at TEXT,
    released_by TEXT
);
-- Read on every tool call of every session: empty almost always.
CREATE INDEX brake_open ON brake(id) WHERE released_at IS NULL;

-- A threshold counts once per period: `period_key` is the UTC day, or 'total'.
CREATE TABLE budget_mark (
    budget_id  INTEGER NOT NULL REFERENCES token_budget(id) ON DELETE CASCADE,
    period_key TEXT NOT NULL,
    level      TEXT NOT NULL CHECK (level IN ('warned', 'exceeded')),
    at         TEXT NOT NULL,
    PRIMARY KEY (budget_id, period_key, level)
) WITHOUT ROWID;

-- What usage_day holds, per task the session was bound to when the line was written. One session
-- on several tasks counts in full on each. The file in the key lets a file that shrank replace
-- its own rows.
CREATE TABLE usage_task_day (
    file_id     INTEGER NOT NULL REFERENCES usage_file(id) ON DELETE CASCADE,
    task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    day         TEXT NOT NULL,
    model       TEXT NOT NULL,
    input       INTEGER NOT NULL DEFAULT 0,
    output      INTEGER NOT NULL DEFAULT 0,
    cache_read  INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    messages    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_id, task_id, day, model)
) WITHOUT ROWID;
CREATE INDEX usage_task_day_task ON usage_task_day(task_id, day);
