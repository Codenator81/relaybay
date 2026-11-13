-- An approved approval the guard let one tool call through with is reserved for that call
-- (`reserved_at`; `reserved_for`: the call's tool_use_id from its PreToolUse; `reserved_call`: the
-- call as `used_by` shows it; `reserved_session_id`: the session that made it) and stays
-- `approved` until the call ran: its PostToolUse, or its result in the run's stream, marks it
-- `used`. A reserved approval covers no other call. When the call did not run (it is in the
-- turn's permission denials, or the turn ended without a sign of it) the reservation is released:
-- the reserved columns are cleared and `released_count` grows; the third release expires the
-- approval. A reserved approval expires by `valid_until` as before.
ALTER TABLE approval ADD COLUMN reserved_at TEXT;
ALTER TABLE approval ADD COLUMN reserved_for TEXT;
ALTER TABLE approval ADD COLUMN reserved_call TEXT;
ALTER TABLE approval ADD COLUMN reserved_session_id INTEGER REFERENCES session(id) ON DELETE SET NULL;
ALTER TABLE approval ADD COLUMN released_count INTEGER NOT NULL DEFAULT 0;
CREATE INDEX approval_reserved ON approval(reserved_for)
    WHERE status = 'approved' AND reserved_at IS NOT NULL;
