-- Comments on a task: the owner writes them on the board, they reach the agent of the bound
-- session through its hooks (delivered_at, delivered_to_session_id) and count as read once the
-- agent acknowledges or answers them. Agents reply with comments of their own (author_kind
-- 'agent', session_id = the replying session); the owner marks those read.
CREATE TABLE comment (
    id                      INTEGER PRIMARY KEY,
    task_id                 INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    author_kind             TEXT NOT NULL CHECK (author_kind IN ('owner', 'agent')),
    author                  TEXT NOT NULL,
    session_id              INTEGER REFERENCES session(id) ON DELETE SET NULL,
    body_md                 TEXT NOT NULL,
    in_reply_to             INTEGER REFERENCES comment(id) ON DELETE SET NULL,
    created_at              TEXT NOT NULL,
    delivered_at            TEXT,
    delivered_to_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    read_at                 TEXT,
    stop_blocks             INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX comment_task ON comment(task_id, id);
CREATE INDEX comment_unread ON comment(task_id) WHERE read_at IS NULL;

-- Comment text is delivered only to a hook session linked by the agent itself (`hook_session`
-- on a write), never to one matched by its folder.
ALTER TABLE session ADD COLUMN cc_exact INTEGER NOT NULL DEFAULT 0;
