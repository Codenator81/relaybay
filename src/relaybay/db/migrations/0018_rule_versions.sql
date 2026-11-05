-- Versioned working rules. `rule_set` is one scope, found by `project_id` (NULL: the global set);
-- `scope` is its display name. `rule_version` is its numbered history; `project_rules` stays the
-- row a project's rules are read from and is written through by every new version.
CREATE TABLE rule_set (
    id              INTEGER PRIMARY KEY,
    scope           TEXT NOT NULL UNIQUE,
    project_id      INTEGER UNIQUE REFERENCES project(id) ON DELETE CASCADE,
    current_version INTEGER NOT NULL DEFAULT 0,
    config_hash     TEXT,  -- global only: the [rules] table last turned into a version
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX rule_set_project ON rule_set(COALESCE(project_id, 0));

CREATE TABLE rule_version (
    id          INTEGER PRIMARY KEY,
    rule_set_id INTEGER NOT NULL REFERENCES rule_set(id) ON DELETE CASCADE,
    number      INTEGER NOT NULL,
    body        TEXT NOT NULL,
    rendered    TEXT NOT NULL,
    author      TEXT NOT NULL,
    session_id  INTEGER REFERENCES session(id) ON DELETE SET NULL,
    reason      TEXT NOT NULL,
    diff        TEXT NOT NULL,
    notice_open INTEGER NOT NULL DEFAULT 0,  -- a direct rules_update the owner has not marked read
    created_at  TEXT NOT NULL,
    UNIQUE (rule_set_id, number)
);

-- A rules change proposed by an agent (`rules_propose`); only a person decides it on the board.
CREATE TABLE rule_proposal (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL,
    project_id  INTEGER REFERENCES project(id) ON DELETE CASCADE,
    patch       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    session_id  INTEGER REFERENCES session(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open', 'accepted', 'rejected')),
    version     INTEGER,  -- the version an accepted proposal made
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE INDEX rule_proposal_open ON rule_proposal(id) WHERE status = 'open';

-- The newest version of a rule set each session has been told about. `project_id` 0 is the
-- global set.
CREATE TABLE rule_seen (
    session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    PRIMARY KEY (session_id, project_id)
);
