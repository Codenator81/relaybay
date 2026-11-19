-- Archive and binding at start (design 0026). `task.archived_from` is the stage a card was
-- archived from when it was not done ('live': the archive closed it, and Back from the archive
-- returns it there); NULL for a done card. A stage change clears it with the other archive marks.
-- `session.launched_by` is the session whose desktop start tool started this one. Additive only.
ALTER TABLE task ADD COLUMN archived_from TEXT CHECK (archived_from IN ('live'));
ALTER TABLE session ADD COLUMN launched_by INTEGER REFERENCES session(id) ON DELETE SET NULL;
