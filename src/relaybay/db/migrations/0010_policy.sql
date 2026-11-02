-- Policy layers below the global file (policy as data): a project layer has no environment, an
-- environment layer names one. `body` is a JSON policy layer, validated on write and on read.
CREATE TABLE policy_layer (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    environment_id INTEGER REFERENCES environment(id) ON DELETE CASCADE,
    body           TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    updated_by     TEXT NOT NULL
);
CREATE UNIQUE INDEX policy_layer_scope ON policy_layer(project_id, COALESCE(environment_id, 0));

-- A policy change an agent proposed (`policy_propose`); only a person accepts it on the board.
-- `scope` is "global", "<project>" or "<project>/<environment>"; `patch` is merged into it.
CREATE TABLE policy_proposal (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL,
    project_id  INTEGER REFERENCES project(id) ON DELETE CASCADE,
    patch       TEXT NOT NULL,
    reason      TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    session_id  INTEGER REFERENCES session(id) ON DELETE SET NULL,
    status      TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected')),
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT
);
CREATE INDEX policy_proposal_pending ON policy_proposal(id) WHERE status = 'pending';
