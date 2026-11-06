-- Messages between sessions: one session writes with `message_send`, the recipient's hooks deliver
-- the text (exact links only, like owner comments), the recipient acknowledges. Names are kept
-- as they were at the time; a session row merged into another hands its messages over first.
-- `sender_verified`, `read_verified`: the writer proved its live hook link. `loop_stopped_at`: a reply
-- to this message went over the depth limit and was refused; the Inbox shows it until
-- `loop_seen_at`. Additive only.
CREATE TABLE session_message (
    id                   INTEGER PRIMARY KEY,
    sender_session_id    INTEGER REFERENCES session(id) ON DELETE SET NULL,
    sender_name          TEXT NOT NULL,
    sender_verified      INTEGER NOT NULL DEFAULT 0,
    recipient_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL,
    recipient_name       TEXT NOT NULL,
    task_id              INTEGER REFERENCES task(id) ON DELETE SET NULL,
    body_md              TEXT NOT NULL,
    in_reply_to          INTEGER REFERENCES session_message(id) ON DELETE SET NULL,
    depth                INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    delivered_at         TEXT,
    read_at              TEXT,
    read_verified        INTEGER NOT NULL DEFAULT 0,
    stop_blocks          INTEGER NOT NULL DEFAULT 0,
    loop_stopped_at      TEXT,
    loop_seen_at         TEXT
);
CREATE INDEX session_message_recipient ON session_message(recipient_session_id, read_at);
CREATE INDEX session_message_sender ON session_message(sender_session_id, created_at);
CREATE INDEX session_message_task ON session_message(task_id, id) WHERE task_id IS NOT NULL;
CREATE INDEX session_message_loop ON session_message(id)
    WHERE loop_stopped_at IS NOT NULL AND loop_seen_at IS NULL;
