-- A question an agent asks the owner on a card (MCP question_ask): from a run (`run_id`, its
-- session in `session_id`) or from a desktop session (`session_id` only). `kind` 'look' asks the
-- owner to look at a screen: always blocking, with the URL and the test account's name (never its
-- secret). `options` is a JSON list. The owner's answer reaches the agent as the next turn of the
-- run (`turn_id`, delivered when that turn's process starts) or as an owner comment (`comment_id`,
-- delivered when the hooks hand it over); `told_at` is when a running turn was told an answer
-- waits. A non-blocking question closes with its default at `default_after` (`defaulted_at`); a
-- later answer of the owner is a change of course.
CREATE TABLE question (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
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
    CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
);
CREATE INDEX question_task ON question(task_id, id);
CREATE INDEX question_open ON question(status, default_after) WHERE status = 'open';
CREATE INDEX question_run ON question(run_id, id) WHERE run_id IS NOT NULL;
CREATE INDEX question_comment ON question(comment_id) WHERE comment_id IS NOT NULL;
