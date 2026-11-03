-- Task plan and checkpoints. Additive only: new tables and new task columns.

-- The task's size (S / M / L) and the status of its plan: NULL while there is no plan, `draft`
-- after the agent writes it, `accepted` once the owner or the coordinator accepts it.
ALTER TABLE task ADD COLUMN size TEXT CHECK (size IS NULL OR size IN ('S', 'M', 'L'));
ALTER TABLE task ADD COLUMN plan_status TEXT
    CHECK (plan_status IS NULL OR plan_status IN ('draft', 'accepted'));
ALTER TABLE task ADD COLUMN plan_accepted_at TEXT;
ALTER TABLE task ADD COLUMN plan_accepted_by TEXT;

-- The ordered steps of a task's plan; a done step carries its checkpoint (when, link, outcome,
-- and whether it went as planned).
CREATE TABLE plan_step (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL,
    title       TEXT NOT NULL,
    details_md  TEXT,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'done', 'blocked')),
    done_at     TEXT,
    link        TEXT,
    outcome     TEXT,
    conformance TEXT,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (task_id, position)
);

-- Every plan write, with the whole plan as JSON: the plan's history on the board.
CREATE TABLE plan_revision (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    revision   INTEGER NOT NULL,
    body       TEXT NOT NULL,
    author     TEXT NOT NULL,
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (task_id, revision)
);

-- The Claude Code todo list of a session, as its PostToolUse hooks report it. Kept per Claude
-- Code id: a new id (after /clear) starts a fresh list.
CREATE TABLE session_todo (
    session_id    INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    cc_session_id TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'in_progress', 'completed')),
    active_form   TEXT,
    position      INTEGER NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (session_id, item_id)
);
