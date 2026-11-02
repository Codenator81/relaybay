-- Claude Code hooks report the CLI session id (not the desktop local_ id): the session row it is
-- linked to keeps it, plus when the activity text was last written. `unlinked_reason` is set
-- for a hook session that could not be matched to a relaybay session unambiguously.
ALTER TABLE session ADD COLUMN cc_session_id TEXT;
ALTER TABLE session ADD COLUMN activity_at TEXT;
ALTER TABLE session ADD COLUMN unlinked_reason TEXT;
CREATE UNIQUE INDEX session_cc_session_id ON session(cc_session_id)
    WHERE cc_session_id IS NOT NULL;
