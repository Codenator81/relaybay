-- relaybay:foreign-keys-off
-- Drops the tables of the runs inside the program (removed in 0.12, design 0025), unread and
-- unwritten since then: run, run_turn, run_event, run_worktree, run_profile,
-- project_run_defaults, coordinator_wake and schedule_run (the history of schedule runs, which
-- points at runs). schedule, token_budget and app_setting stay.
-- question and approval keep every row and every column: their run_id and turn_id stay as plain
-- numbers (the run's id in the events), only the foreign keys to the dropped tables go, since
-- SQLite refuses any write into a table whose foreign key names a missing table. SQLite cannot
-- drop a foreign key in place, so both tables are rebuilt (create, copy, drop, rename) with
-- foreign keys off, as 0045 did. The backup before a new version runs before this migration.
DROP TABLE coordinator_wake;
DROP TABLE schedule_run;
DROP TABLE run_event;
DROP TABLE run_worktree;
DROP TABLE run_turn;
DROP TABLE run;
DROP TABLE run_profile;
DROP TABLE project_run_defaults;

CREATE TABLE question_new (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER REFERENCES task(id) ON DELETE CASCADE,
    run_id         INTEGER,
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
    turn_id        INTEGER,
    comment_id     INTEGER REFERENCES comment(id) ON DELETE SET NULL,
    told_at        TEXT,
    delivered_at   TEXT, overdue_at TEXT,
    CHECK (kind != 'look' OR (blocking = 1 AND look_url IS NOT NULL)),
    CHECK (blocking = 0 OR default_after IS NULL),
    CHECK (status != 'open' OR (answer IS NULL AND withdrawn_at IS NULL)),
    CHECK (status != 'answered' OR (answered_by = 'owner' AND answer IS NOT NULL)),
    CHECK (status != 'defaulted' OR (answered_by = 'default' AND defaulted_at IS NOT NULL)),
    CHECK ((status = 'withdrawn') = (withdrawn_at IS NOT NULL)),
    CHECK (task_id IS NOT NULL OR run_id IS NOT NULL)
);

INSERT INTO question_new (id, task_id, run_id, session_id, asked_by, kind, text, options, default_answer, blocking, look_url, look_account, default_after, asked_at, status, answer, answered_by, answered_at, defaulted_at, withdrawn_at, withdrawn_by, turn_id, comment_id, told_at, delivered_at, overdue_at)
SELECT id, task_id, run_id, session_id, asked_by, kind, text, options, default_answer, blocking, look_url, look_account, default_after, asked_at, status, answer, answered_by, answered_at, defaulted_at, withdrawn_at, withdrawn_by, turn_id, comment_id, told_at, delivered_at, overdue_at FROM question;

DROP TABLE question;
ALTER TABLE question_new RENAME TO question;

CREATE INDEX question_comment ON question(comment_id) WHERE comment_id IS NOT NULL;

CREATE INDEX question_open ON question(status, default_after) WHERE status = 'open';

CREATE INDEX question_run ON question(run_id, id) WHERE run_id IS NOT NULL;

CREATE INDEX question_task ON question(task_id, id);

CREATE TABLE approval_new (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    run_id         INTEGER,
    session_id     INTEGER REFERENCES session(id) ON DELETE SET NULL,
    requested_by   TEXT NOT NULL,
    requested_via  TEXT NOT NULL CHECK (requested_via IN ('agent', 'guard', 'denial')),
    action         TEXT NOT NULL CHECK (action IN ('commit', 'push', 'merge', 'deploy', 'sql')),
    mode           TEXT NOT NULL DEFAULT 'plain' CHECK (mode IN ('plain', 'force', 'delete')),
    repository     TEXT,
    branch         TEXT,
    pr             TEXT,
    environment_id INTEGER REFERENCES environment(id) ON DELETE SET NULL,
    environment    TEXT,
    summary        TEXT NOT NULL,
    rollback       TEXT,
    requested_at   TEXT NOT NULL,
    status         TEXT NOT NULL
                   CHECK (status IN ('open', 'approved', 'refused', 'used', 'expired')),
    decision       TEXT CHECK (decision IN ('approved', 'refused')),
    decided_by     TEXT,
    decided_at     TEXT,
    decision_text  TEXT,
    valid_until    TEXT,
    used_at        TEXT,
    used_by        TEXT,
    used_session   TEXT,
    expired_at     TEXT,
    expired_reason TEXT,
    turn_id        INTEGER,
    comment_id     INTEGER REFERENCES comment(id) ON DELETE SET NULL,
    told_at        TEXT,
    delivered_at   TEXT, reserved_at TEXT, reserved_for TEXT, reserved_call TEXT, reserved_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL, released_count INTEGER NOT NULL DEFAULT 0,
    CHECK (action NOT IN ('push', 'commit') OR branch IS NOT NULL),
    CHECK (action != 'merge' OR pr IS NOT NULL),
    CHECK (action NOT IN ('deploy', 'sql') OR environment IS NOT NULL),
    CHECK (action = 'push' OR mode = 'plain'),
    CHECK (status != 'open' OR decision IS NULL),
    CHECK (decision IS NOT NULL OR status IN ('open', 'expired')),
    CHECK ((status = 'expired') = (expired_at IS NOT NULL)),
    CHECK (status != 'refused' OR decision = 'refused'),
    CHECK (status NOT IN ('approved', 'used') OR decision = 'approved'),
    CHECK ((status = 'used') = (used_at IS NOT NULL)),
    CHECK (decision IS NULL OR (decided_by IS NOT NULL AND decided_at IS NOT NULL)),
    CHECK (action != 'deploy' OR decision IS NOT 'approved' OR rollback IS NOT NULL)
);

INSERT INTO approval_new (id, task_id, run_id, session_id, requested_by, requested_via, action, mode, repository, branch, pr, environment_id, environment, summary, rollback, requested_at, status, decision, decided_by, decided_at, decision_text, valid_until, used_at, used_by, used_session, expired_at, expired_reason, turn_id, comment_id, told_at, delivered_at, reserved_at, reserved_for, reserved_call, reserved_session_id, released_count)
SELECT id, task_id, run_id, session_id, requested_by, requested_via, action, mode, repository, branch, pr, environment_id, environment, summary, rollback, requested_at, status, decision, decided_by, decided_at, decision_text, valid_until, used_at, used_by, used_session, expired_at, expired_reason, turn_id, comment_id, told_at, delivered_at, reserved_at, reserved_for, reserved_call, reserved_session_id, released_count FROM approval;

DROP TABLE approval;
ALTER TABLE approval_new RENAME TO approval;

CREATE INDEX approval_comment ON approval(comment_id) WHERE comment_id IS NOT NULL;

CREATE INDEX approval_open ON approval(task_id, status) WHERE status IN ('open', 'approved');

CREATE INDEX approval_reserved ON approval(reserved_for)
    WHERE status = 'approved' AND reserved_at IS NOT NULL;

CREATE INDEX approval_run ON approval(run_id, id) WHERE run_id IS NOT NULL;

CREATE INDEX approval_task ON approval(task_id, id);
