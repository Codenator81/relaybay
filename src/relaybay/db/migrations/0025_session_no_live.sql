-- "No live now": the owner's switch per session, on top of the policy. Set: any touch of a live
-- environment by this session is denied. Additive only.
ALTER TABLE session ADD COLUMN no_live_at TEXT;
ALTER TABLE session ADD COLUMN no_live_by TEXT;
