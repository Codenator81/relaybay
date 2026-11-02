-- Project environments (local / test / live) with an access map that holds references only,
-- never secret values, and the agents' permission level. `level` NULL means the default for the
-- kind (config `permission_defaults`); `overrides` is a JSON object capability -> level.
CREATE TABLE environment (
    id               INTEGER PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('local', 'test', 'live')),
    base_url         TEXT,
    sign_in_allowed  INTEGER NOT NULL DEFAULT 0 CHECK (kind <> 'live' OR sign_in_allowed = 0),
    login_state_path TEXT,
    notes            TEXT,
    level            TEXT CHECK (level IN ('none', 'read', 'ask', 'full')),
    overrides        TEXT NOT NULL DEFAULT '{}',
    position         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    updated_by       TEXT NOT NULL,
    UNIQUE (project_id, name)
);

-- How an agent reaches an environment: `fields` is a JSON object of references (ssh alias,
-- kube context, database host, a key file path, an environment variable name, a vault path).
CREATE TABLE environment_access (
    id             INTEGER PRIMARY KEY,
    environment_id INTEGER NOT NULL REFERENCES environment(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL
                   CHECK (kind IN ('ssh', 'kubectl', 'db', 'gitops', 'logs', 'monitoring', 'other')),
    fields         TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    updated_by     TEXT NOT NULL
);
CREATE INDEX environment_access_env ON environment_access(environment_id, id);

-- A change an agent proposed (`environment_propose`); only a person accepts it on the board.
CREATE TABLE environment_proposal (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    change      TEXT NOT NULL,
    reason      TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    session_id  INTEGER REFERENCES session(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE INDEX environment_proposal_pending ON environment_proposal(project_id)
    WHERE status = 'pending';

-- The live-actions journal reads events by kind (Activity) and by task and kind (task dialog).
CREATE INDEX event_kind ON event(kind, id);
CREATE INDEX event_task_kind ON event(task_id, kind, id);
