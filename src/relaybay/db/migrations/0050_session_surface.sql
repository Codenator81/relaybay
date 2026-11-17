-- The session card (design 0025): where a session runs, so the card's Open button can follow it.
-- `surface` is 'desktop' or 'terminal' when the hook could tell (from Claude Code's
-- CLAUDE_CODE_ENTRYPOINT) or the session said so; NULL when unknown. `entrypoint` keeps the raw
-- value the hook sent. A session with a desktop id (`external_id`) is a desktop one whatever these
-- say. Additive: older hook clients send nothing and leave both NULL.
ALTER TABLE session ADD COLUMN surface TEXT CHECK (surface IN ('desktop', 'terminal'));
ALTER TABLE session ADD COLUMN entrypoint TEXT;
