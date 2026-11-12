-- A done task sent to the archive by hand (board or a coordinator's task_archive): when and by
-- whom. `unarchived_at` is the last "Back from the archive": the automatic done window counts
-- from it as from closed_at. Any stage change clears all three.
ALTER TABLE task ADD COLUMN archived_at TEXT;
ALTER TABLE task ADD COLUMN archived_by TEXT;
ALTER TABLE task ADD COLUMN unarchived_at TEXT;
