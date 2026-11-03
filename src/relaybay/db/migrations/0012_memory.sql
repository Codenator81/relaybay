-- Memory and documents. Additive only: new tables and new columns; existing rows keep their data
-- and only the new columns are filled in. The full-text index is not created here: the service
-- creates it at start when this SQLite build has FTS5 (and searches with LIKE otherwise).

-- Decisions: scope, structured provenance and two time axes (when the fact held, when it was
-- recorded or replaced). Superseding fills superseded_at / superseded_by and valid_to; nothing
-- is deleted.
ALTER TABLE decision ADD COLUMN scope TEXT NOT NULL DEFAULT 'project'
    CHECK (scope IN ('project', 'task'));
ALTER TABLE decision ADD COLUMN source_kind TEXT
    CHECK (source_kind IN ('report', 'event', 'comment', 'message'));
ALTER TABLE decision ADD COLUMN source_ref TEXT;
ALTER TABLE decision ADD COLUMN source_text TEXT;
ALTER TABLE decision ADD COLUMN valid_from TEXT;
ALTER TABLE decision ADD COLUMN valid_to TEXT;
ALTER TABLE decision ADD COLUMN recorded_at TEXT;
ALTER TABLE decision ADD COLUMN superseded_at TEXT;
ALTER TABLE decision ADD COLUMN superseded_by INTEGER REFERENCES decision(id) ON DELETE SET NULL;
UPDATE decision SET scope = CASE WHEN task_id IS NULL THEN 'project' ELSE 'task' END,
                    recorded_at = created_at,
                    valid_from = created_at;

-- A project document (spec, status, brief, runbook, ADR); its text lives in versions.
CREATE TABLE document (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    slug       TEXT NOT NULL,
    title      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    version    INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, slug)
);

CREATE TABLE document_version (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    version      INTEGER NOT NULL,
    body         TEXT NOT NULL,
    author       TEXT NOT NULL,
    reason       TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    section      TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (document_id, version)
);

-- Memory notes: owner preferences, feedback, references. Global and role notes have no project.
CREATE TABLE memory (
    id            INTEGER PRIMARY KEY,
    scope         TEXT NOT NULL CHECK (scope IN ('global', 'role', 'project')),
    project_id    INTEGER REFERENCES project(id) ON DELETE CASCADE,
    role          TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('user', 'feedback', 'project', 'reference')),
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,
    tags          TEXT,
    source_kind   TEXT CHECK (source_kind IN ('report', 'event', 'comment', 'message')),
    source_ref    TEXT,
    source_text   TEXT,
    links         TEXT,
    review_at     TEXT,
    valid_from    TEXT NOT NULL,
    valid_to      TEXT,
    recorded_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    superseded_at TEXT,
    superseded_by INTEGER REFERENCES memory(id) ON DELETE SET NULL,
    last_read_at  TEXT,
    read_count    INTEGER NOT NULL DEFAULT 0,
    version       INTEGER NOT NULL DEFAULT 1,
    author        TEXT NOT NULL,
    CHECK ((scope = 'project') = (project_id IS NOT NULL)),
    CHECK (scope != 'role' OR role IS NOT NULL)
);
CREATE INDEX memory_project ON memory(project_id, id);

-- Blocks: short labelled texts always shown at their level ("owner preferences"). A block
-- marked owner_only is edited on the board only.
CREATE TABLE memory_block (
    id          INTEGER PRIMARY KEY,
    scope       TEXT NOT NULL CHECK (scope IN ('global', 'project')),
    project_id  INTEGER REFERENCES project(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    description TEXT,
    body        TEXT NOT NULL,
    char_limit  INTEGER NOT NULL DEFAULT 2000,
    owner_only  INTEGER NOT NULL DEFAULT 0,
    version     INTEGER NOT NULL DEFAULT 1,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    CHECK ((scope = 'project') = (project_id IS NOT NULL))
);
CREATE UNIQUE INDEX memory_block_label ON memory_block(COALESCE(project_id, 0), label);
