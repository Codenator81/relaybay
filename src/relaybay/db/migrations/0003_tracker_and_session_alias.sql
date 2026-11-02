-- The task's issue in an external tracker: key ("ENG-12"), tracker name ("Linear"), optional
-- http(s) link, and whether the work log has been entered there.
ALTER TABLE task ADD COLUMN tracker_ref TEXT;
ALTER TABLE task ADD COLUMN tracker_name TEXT;
ALTER TABLE task ADD COLUMN tracker_url TEXT;
ALTER TABLE task ADD COLUMN tracker_logged INTEGER NOT NULL DEFAULT 0;

-- Plain names that resolve to a desktop session: a session that does not know its local_ id
-- keeps acting as that desktop session instead of replacing its binding.
CREATE TABLE session_alias (
    name       TEXT PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL
);
CREATE INDEX session_alias_session ON session_alias(session_id);
