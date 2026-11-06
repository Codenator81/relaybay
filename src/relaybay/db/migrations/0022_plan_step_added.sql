-- The living plan: a step added to an accepted plan with plan_step_add keeps the acceptance and
-- carries when, by whom and why it was added. Additive only: existing steps keep NULL here.
ALTER TABLE plan_step ADD COLUMN added_after_acceptance_at TEXT;
ALTER TABLE plan_step ADD COLUMN added_by TEXT;
ALTER TABLE plan_step ADD COLUMN added_reason TEXT;
