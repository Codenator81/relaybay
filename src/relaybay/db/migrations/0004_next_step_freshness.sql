-- When next_step was last written and when the stage last changed: a next step set before the
-- last stage change is shown as stale. Existing rows start from updated_at, so nothing is stale.
ALTER TABLE task ADD COLUMN next_step_at TEXT;
ALTER TABLE task ADD COLUMN stage_changed_at TEXT;
UPDATE task SET next_step_at = updated_at, stage_changed_at = updated_at;
