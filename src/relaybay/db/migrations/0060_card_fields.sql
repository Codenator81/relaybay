-- What a deploy of the card needs (migration, flag, script, rollback) and whether it ran; a card
-- given up on, with the reason and when; the folder a task works in per linked repository (a
-- git worktree), set by the hooks or by a `repos` entry 'name <absolute path>'.
ALTER TABLE task ADD COLUMN deploy_notes TEXT;
ALTER TABLE task ADD COLUMN deploy_done INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task ADD COLUMN cancelled TEXT;
ALTER TABLE task ADD COLUMN cancelled_at TEXT;
ALTER TABLE task_repo ADD COLUMN worktree TEXT;
