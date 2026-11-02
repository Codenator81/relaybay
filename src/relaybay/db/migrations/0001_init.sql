CREATE TABLE project (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    repo_path   TEXT,
    description TEXT,
    task_seq    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    archived_at TEXT
);

CREATE TABLE session (
    id           INTEGER PRIMARY KEY,
    external_id  TEXT UNIQUE,                 -- e.g. a Claude desktop session id "local_<uuid>"
    name         TEXT NOT NULL,
    project_id   INTEGER REFERENCES project(id) ON DELETE SET NULL,
    cwd          TEXT,
    kind         TEXT,
    desktop_link TEXT,
    status       TEXT CHECK (status IN ('working', 'idle', 'waiting', 'ended')),
    activity     TEXT,
    last_seen_at TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
-- A plain name identifies a session only without an external id (desktop titles may repeat).
CREATE UNIQUE INDEX session_plain_name ON session(name) WHERE external_id IS NULL;

CREATE TABLE task (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    key        TEXT NOT NULL UNIQUE,
    title      TEXT NOT NULL,
    stage      TEXT NOT NULL DEFAULT 'plan'
               CHECK (stage IN ('plan', 'dev', 'test', 'live', 'done')),
    stage_note TEXT,
    waiting_on TEXT,
    next_step  TEXT,
    branch     TEXT,
    doc_path   TEXT,
    session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    priority   INTEGER NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    closed_at  TEXT
);
CREATE INDEX task_project_stage ON task(project_id, stage);

CREATE TABLE task_session (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    bound_at   TEXT NOT NULL,
    unbound_at TEXT,
    bound_by   TEXT NOT NULL
);
CREATE INDEX task_session_task ON task_session(task_id, id);
CREATE UNIQUE INDEX task_session_open ON task_session(task_id) WHERE unbound_at IS NULL;

CREATE TABLE report (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    author     TEXT NOT NULL,
    state      TEXT,
    size       TEXT,
    summary    TEXT NOT NULL,
    body_md    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX report_task ON report(task_id, id);

CREATE TABLE decision (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER REFERENCES task(id) ON DELETE SET NULL,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    decided_by TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX decision_project ON decision(project_id, id);

CREATE TABLE event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    kind       TEXT NOT NULL,
    project_id INTEGER,
    task_id    INTEGER,
    session_id INTEGER,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX event_project ON event(project_id, id);
