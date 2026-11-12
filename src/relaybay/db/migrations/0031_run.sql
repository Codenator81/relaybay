-- A run: the user's own `claude` started by relaybay on a card, one turn at a time with --resume.
-- `session_id` is the relaybay session row its hooks report to; `cwd_repo_id` the project
-- repository used as the cwd (NULL: the project folder). `auth_method` keeps the sign-in method
-- only, never an e-mail or an organization. All seven statuses are allowed now so later releases
-- need no table rebuild; `ended` is final.
CREATE TABLE run (
    id                INTEGER PRIMARY KEY,
    task_id           INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    project_id        INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
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
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    ended_at          TEXT,
    ended_by          TEXT,
    CHECK ((status = 'ended') = (ended_at IS NOT NULL))
);
-- One open run per card; one run per Claude Code session.
CREATE UNIQUE INDEX run_open_task ON run(task_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX run_claude_session ON run(claude_session_id)
    WHERE claude_session_id IS NOT NULL;
CREATE INDEX run_project ON run(project_id, id);
