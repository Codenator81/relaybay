-- relaybay:foreign-keys-off
-- Intake: messages from chats and mailboxes, tied to the task that waits for them, and an
-- autonomy switch per project and task. A comment of the new author kind 'external' carries an
-- external message to the bound agent; SQLite cannot widen a CHECK constraint in place, so the
-- comment table is rebuilt (create, copy, drop, rename) with foreign keys off. Nothing else
-- changes for existing rows.
CREATE TABLE comment_new (
    id                      INTEGER PRIMARY KEY,
    task_id                 INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    author_kind             TEXT NOT NULL CHECK (author_kind IN ('owner', 'agent', 'external')),
    author                  TEXT NOT NULL,
    session_id              INTEGER REFERENCES session(id) ON DELETE SET NULL,
    body_md                 TEXT NOT NULL,
    in_reply_to             INTEGER REFERENCES comment(id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL,
    delivered_at            TEXT,
    delivered_to_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    read_at                 TEXT,
    stop_blocks             INTEGER NOT NULL DEFAULT 0,
    step_id                 INTEGER REFERENCES review_step(id) ON DELETE SET NULL,
    attachment_id           INTEGER REFERENCES attachment(id) ON DELETE SET NULL
);
INSERT INTO comment_new (id, task_id, author_kind, author, session_id, body_md, in_reply_to,
                         created_at, delivered_at, delivered_to_session_id, read_at, stop_blocks,
                         step_id, attachment_id)
SELECT id, task_id, author_kind, author, session_id, body_md, in_reply_to, created_at,
       delivered_at, delivered_to_session_id, read_at, stop_blocks, step_id, attachment_id
FROM comment;
DROP TABLE comment;
ALTER TABLE comment_new RENAME TO comment;
CREATE INDEX comment_task ON comment(task_id, id);
CREATE INDEX comment_unread ON comment(task_id) WHERE read_at IS NULL;

-- `relaybay import` also tracks backlog.md rows: the kind CHECK widens the same way.
CREATE TABLE import_item_new (
    kind         TEXT NOT NULL
                 CHECK (kind IN ('document', 'task', 'task_note', 'decision', 'note', 'backlog')),
    item         TEXT NOT NULL,
    source_path  TEXT NOT NULL,
    target_id    INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    target_hash  TEXT NOT NULL,
    imported_at  TEXT NOT NULL,
    PRIMARY KEY (kind, item)
);
INSERT INTO import_item_new SELECT kind, item, source_path, target_id, content_hash, target_hash,
                                   imported_at FROM import_item;
DROP TABLE import_item;
ALTER TABLE import_item_new RENAME TO import_item;

-- A source is created by its first message, untrusted: only the board sets may_wake.
CREATE TABLE intake_source (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL DEFAULT 'other'
                    CHECK (kind IN ('telegram', 'email', 'chat', 'other')),
    name            TEXT NOT NULL,
    may_wake        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    last_message_at TEXT
);

-- One message as the watcher posted it; nothing of the raw payload beyond these fields. A refused
-- message keeps no text, only the reason (a secret detector or the rate limit). `outcome` says
-- what happened once it was tied: notified, forwarded (by the owner) or woke (the agent).
CREATE TABLE intake_message (
    id             INTEGER PRIMARY KEY,
    source_id      INTEGER NOT NULL REFERENCES intake_source(id) ON DELETE CASCADE,
    external_id    TEXT NOT NULL,
    thread         TEXT,
    author         TEXT,
    text           TEXT,
    received_at    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'new'
                   CHECK (status IN ('new', 'tied', 'proposed', 'dismissed', 'refused')),
    refused_reason TEXT,
    task_id        INTEGER REFERENCES task(id) ON DELETE SET NULL,
    outcome        TEXT CHECK (outcome IN ('notified', 'forwarded', 'woke')),
    created_at     TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);
CREATE INDEX intake_message_status ON intake_message(status, id);
CREATE INDEX intake_message_task ON intake_message(task_id, id);
CREATE INDEX intake_message_source ON intake_message(source_id, created_at);

-- A thread the owner (or any tie) tied to a task: later messages of the thread match it.
CREATE TABLE intake_thread (
    source_id  INTEGER NOT NULL REFERENCES intake_source(id) ON DELETE CASCADE,
    thread     TEXT NOT NULL,
    task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_id, thread)
);

-- The backlog: findings of sessions and reviews, ideas from intake and the owner's own notes.
-- Not a task and not a stage; "To plan" on the board creates the task (status planned).
CREATE TABLE backlog_item (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    title             TEXT NOT NULL,
    body_md           TEXT,
    area              TEXT,
    origin            TEXT NOT NULL CHECK (origin IN ('session', 'review', 'intake', 'owner')),
    origin_task_id    INTEGER REFERENCES task(id) ON DELETE SET NULL,
    origin_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    intake_message_id INTEGER REFERENCES intake_message(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new', 'later', 'planned', 'declined')),
    decline_reason    TEXT,
    task_id           INTEGER REFERENCES task(id) ON DELETE SET NULL,
    decided_at        TEXT,
    decided_by        TEXT,
    created_at        TEXT NOT NULL,
    created_by        TEXT NOT NULL
);
CREATE INDEX backlog_item_project ON backlog_item(project_id, status, id);
CREATE INDEX backlog_item_task ON backlog_item(task_id);
CREATE INDEX backlog_item_origin_task ON backlog_item(origin_task_id);

-- Autonomy: 'project' follows the project's switch; both are off by default.
ALTER TABLE task ADD COLUMN autonomy TEXT NOT NULL DEFAULT 'project'
    CHECK (autonomy IN ('project', 'on', 'off'));
ALTER TABLE task ADD COLUMN answer_received_at TEXT;
ALTER TABLE task ADD COLUMN answer_message_id INTEGER
    REFERENCES intake_message(id) ON DELETE SET NULL;
ALTER TABLE project ADD COLUMN autonomy INTEGER NOT NULL DEFAULT 0;
