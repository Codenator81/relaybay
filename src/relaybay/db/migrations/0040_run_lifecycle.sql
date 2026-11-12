-- The lifecycle of a run. `secret_seen_at`: the journal's masking replaced something in one of its
-- stored lines; `touched_live_at`: the guard saw one of its calls on a live stand. Either one has
-- the run's lines and its Claude Code transcript (`transcript_path`, as computed when its session
-- id came) deleted when its card goes to done (`deleted_at`). A project keeps its runs' lines
-- `run_retention` days (a whole number), or not at all ('off'). A worktree the run created that
-- the card's close left in place has `kept_reason` (work left in it, or git's refusal) and the
-- question to the owner whether to remove it; `kept_at` is the owner's "keep".
ALTER TABLE run ADD COLUMN secret_seen_at TEXT;
ALTER TABLE run ADD COLUMN touched_live_at TEXT;
ALTER TABLE run ADD COLUMN transcript_path TEXT;
ALTER TABLE run ADD COLUMN deleted_at TEXT;
ALTER TABLE project ADD COLUMN run_retention TEXT NOT NULL DEFAULT '30';
ALTER TABLE run_worktree ADD COLUMN kept_reason TEXT;
ALTER TABLE run_worktree ADD COLUMN question_id INTEGER REFERENCES question(id) ON DELETE SET NULL;
ALTER TABLE run_worktree ADD COLUMN kept_at TEXT;
CREATE INDEX run_event_ts ON run_event(ts);
CREATE INDEX run_worktree_question ON run_worktree(question_id) WHERE question_id IS NOT NULL;
