-- relaybay:foreign-keys-off
-- The coordinator run (design 0023): a run gets a `kind`. 'card' is a run on a card as before;
-- 'coordinator' is the one permanent run without a card (no task, no project, its cwd its own
-- folder); 'check' is a schedule's one-shot run, always in a project, on a card or not.
-- `started_by_run`: the coordinator run that started a run with run_start (its daily cap).
-- A coordinator's question has no card: `question.task_id` is nullable, bound to its run.
-- `run_turn.result_md`: the result text of a turn without a card (no report row holds it).
-- SQLite cannot relax NOT NULL or add a CHECK in place, so both tables are rebuilt (create,
-- copy, drop, rename) with foreign keys off; every existing row keeps every value.
CREATE TABLE run_new (
    id                INTEGER PRIMARY KEY,
    kind              TEXT NOT NULL DEFAULT 'card'
                      CHECK (kind IN ('card', 'coordinator', 'check')),
    task_id           INTEGER REFERENCES task(id) ON DELETE CASCADE,
    project_id        INTEGER REFERENCES project(id) ON DELETE CASCADE,
    session_id        INTEGER REFERENCES session(id) ON DELETE SET NULL,
    cwd               TEXT NOT NULL,
    cwd_repo_id       INTEGER REFERENCES project_repo(id) ON DELETE SET NULL,
    model             TEXT NOT NULL,
    effort            TEXT NOT NULL,
    permission_mode   TEXT NOT NULL,
    allowed_tools     TEXT NOT NULL,
    chrome            INTEGER NOT NULL DEFAULT 0 CHECK (chrome IN (0, 1)),
    claude_session_id TEXT,
    status            TEXT NOT NULL CHECK (status IN
                        ('queued', 'running', 'waiting', 'idle', 'stopped', 'ended', 'lost')),
    status_reason     TEXT,
    auth_method       TEXT,
    hooks_source      TEXT NOT NULL CHECK (hooks_source IN ('user', 'run')),
    mcp_source        TEXT NOT NULL CHECK (mcp_source IN ('user', 'run')),
    started_by        TEXT NOT NULL,
    started_by_kind   TEXT NOT NULL CHECK (started_by_kind IN ('owner', 'coordinator')),
    started_by_run    INTEGER REFERENCES run(id) ON DELETE SET NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    ended_at          TEXT,
    ended_by          TEXT,
    with_owner_at     TEXT,
    with_owner_by     TEXT,
    desktop_opened_at TEXT,
    secret_seen_at    TEXT,
    touched_live_at   TEXT,
    transcript_path   TEXT,
    deleted_at        TEXT,
    with_owner_in     TEXT,
    profile           TEXT,
    disallowed_tools  TEXT,
    mode_set          INTEGER NOT NULL DEFAULT 0 CHECK (mode_set IN (0, 1)),
    CHECK ((status = 'ended') = (ended_at IS NOT NULL)),
    CHECK (kind != 'card' OR (task_id IS NOT NULL AND project_id IS NOT NULL)),
    CHECK (kind != 'coordinator' OR (task_id IS NULL AND project_id IS NULL)),
    CHECK (kind != 'check' OR project_id IS NOT NULL)
);
INSERT INTO run_new (id, kind, task_id, project_id, session_id, cwd, cwd_repo_id, model, effort,
                     permission_mode, allowed_tools, chrome, claude_session_id, status,
                     status_reason, auth_method, hooks_source, mcp_source, started_by,
                     started_by_kind, created_at, updated_at, ended_at, ended_by, with_owner_at,
                     with_owner_by, desktop_opened_at, secret_seen_at, touched_live_at,
                     transcript_path, deleted_at, with_owner_in, profile, disallowed_tools,
                     mode_set)
SELECT id, 'card', task_id, project_id, session_id, cwd, cwd_repo_id, model, effort,
       permission_mode, allowed_tools, chrome, claude_session_id, status, status_reason,
       auth_method, hooks_source, mcp_source, started_by, started_by_kind, created_at,
       updated_at, ended_at, ended_by, with_owner_at, with_owner_by, desktop_opened_at,
       secret_seen_at, touched_live_at, transcript_path, deleted_at, with_owner_in, profile,
       disallowed_tools, mode_set
FROM run;
DROP TABLE run;
ALTER TABLE run_new RENAME TO run;
-- One open run per card; one run per Claude Code session; at most one open coordinator run.
CREATE UNIQUE INDEX run_open_task ON run(task_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX run_claude_session ON run(claude_session_id)
    WHERE claude_session_id IS NOT NULL;
CREATE INDEX run_project ON run(project_id, id);
CREATE UNIQUE INDEX run_open_coordinator ON run(kind)
    WHERE kind = 'coordinator' AND ended_at IS NULL;
CREATE INDEX run_started_by_run ON run(started_by_run, created_at)
    WHERE started_by_run IS NOT NULL;

CREATE TABLE question_new (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER REFERENCES task(id) ON DELETE CASCADE,
    run_id         INTEGER REFERENCES run(id) ON DELETE SET NULL,
    session_id     INTEGER REFERENCES session(id) ON DELETE SET NULL,
    asked_by       TEXT NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('question', 'look')),
    text           TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '[]',
    default_answer TEXT NOT NULL,
    blocking       INTEGER NOT NULL CHECK (blocking IN (0, 1)),
    look_url       TEXT,
    look_account   TEXT,
    default_after  TEXT,
    asked_at       TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('open', 'answered', 'defaulted', 'withdrawn')),
    answer         TEXT,
    answered_by    TEXT CHECK (answered_by IN ('owner', 'default')),
    answered_at    TEXT,
    defaulted_at   TEXT,
    withdrawn_at   TEXT,
    withdrawn_by   TEXT,
    turn_id        INTEGER REFERENCES run_turn(id) ON DELETE SET NULL,
    comment_id     INTEGER REFERENCES comment(id) ON DELETE SET NULL,
    told_at        TEXT,
    delivered_at   TEXT,
    CHECK (kind != 'look' OR (blocking = 1 AND look_url IS NOT NULL)),
    CHECK (blocking = 0 OR default_after IS NULL),
    CHECK (status != 'open' OR (answer IS NULL AND withdrawn_at IS NULL)),
    CHECK (status != 'answered' OR (answered_by = 'owner' AND answer IS NOT NULL)),
    CHECK (status != 'defaulted' OR (answered_by = 'default' AND defaulted_at IS NOT NULL)),
    CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL)),
    CHECK (task_id IS NOT NULL OR run_id IS NOT NULL)
);
INSERT INTO question_new (id, task_id, run_id, session_id, asked_by, kind, text, options,
                          default_answer, blocking, look_url, look_account, default_after,
                          asked_at, status, answer, answered_by, answered_at, defaulted_at,
                          withdrawn_at, withdrawn_by, turn_id, comment_id, told_at,
                          delivered_at)
SELECT id, task_id, run_id, session_id, asked_by, kind, text, options, default_answer, blocking,
       look_url, look_account, default_after, asked_at, status, answer, answered_by,
       answered_at, defaulted_at, withdrawn_at, withdrawn_by, turn_id, comment_id, told_at,
       delivered_at
FROM question;
DROP TABLE question;
ALTER TABLE question_new RENAME TO question;
CREATE INDEX question_task ON question(task_id, id);
CREATE INDEX question_open ON question(status, default_after) WHERE status = 'open';
CREATE INDEX question_run ON question(run_id, id) WHERE run_id IS NOT NULL;
CREATE INDEX question_comment ON question(comment_id) WHERE comment_id IS NOT NULL;

ALTER TABLE run_turn ADD COLUMN result_md TEXT;
