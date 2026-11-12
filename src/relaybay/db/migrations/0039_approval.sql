-- An approval the owner gives or refuses on a card for one guarded action: a push of a branch, a
-- merge of a pull request, a commit, a deploy or an SQL change on a stand. Requested by an agent
-- (MCP approval_request, `requested_via` 'agent'), by the PreToolUse guard when a call needs one
-- ('guard'), or by the runner from a turn's permission denial ('denial'); from a run (`run_id`,
-- its session in `session_id`) or a desktop session (`session_id` only). Only the owner's button
-- decides it. An approved one allows exactly one matching tool call: `used_at` / `used_by` (the
-- tool's name and its masked, cut input) are set in the same transaction as that call's allow.
-- The decision reaches the agent as the run's next turn (`turn_id`) or as an owner comment
-- (`comment_id`), as a question's answer does.
CREATE TABLE approval (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    run_id         INTEGER REFERENCES run(id) ON DELETE SET NULL,
    session_id     INTEGER REFERENCES session(id) ON DELETE SET NULL,
    requested_by   TEXT NOT NULL,
    requested_via  TEXT NOT NULL CHECK (requested_via IN ('agent', 'guard', 'denial')),
    action         TEXT NOT NULL CHECK (action IN ('commit', 'push', 'merge', 'deploy', 'sql')),
    repository     TEXT,
    branch         TEXT,
    pr             TEXT,
    environment_id INTEGER REFERENCES environment(id) ON DELETE SET NULL,
    environment    TEXT,
    summary        TEXT NOT NULL,
    rollback       TEXT,
    requested_at   TEXT NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('open', 'approved', 'refused', 'used')),
    decision       TEXT CHECK (decision IN ('approved', 'refused')),
    decided_by     TEXT,
    decided_at     TEXT,
    decision_text  TEXT,
    used_at        TEXT,
    used_by        TEXT,
    used_session   TEXT,
    turn_id        INTEGER REFERENCES run_turn(id) ON DELETE SET NULL,
    comment_id     INTEGER REFERENCES comment(id) ON DELETE SET NULL,
    told_at        TEXT,
    delivered_at   TEXT,
    CHECK (action NOT IN ('push', 'commit') OR branch IS NOT NULL),
    CHECK (action != 'merge' OR pr IS NOT NULL),
    CHECK (action NOT IN ('deploy', 'sql') OR environment IS NOT NULL),
    CHECK ((status = 'open') = (decision IS NULL)),
    CHECK (status != 'refused' OR decision = 'refused'),
    CHECK (status NOT IN ('approved', 'used') OR decision = 'approved'),
    CHECK ((status = 'used') = (used_at IS NOT NULL)),
    CHECK (decision IS NULL OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    CHECK (action != 'deploy' OR decision IS NOT 'approved' OR rollback IS NOT NULL)
);
CREATE INDEX approval_task ON approval(task_id, id);
CREATE INDEX approval_open ON approval(status) WHERE status IN ('open', 'approved');
CREATE INDEX approval_run ON approval(run_id, id) WHERE run_id IS NOT NULL;
CREATE INDEX approval_comment ON approval(comment_id) WHERE comment_id IS NOT NULL;
