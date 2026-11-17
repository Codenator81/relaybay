-- The session card (design 0025): where a session runs, so the card's Open button can follow it.
-- `surface` is 'desktop' or 'terminal' when the hook could tell (from Claude Code's
-- CLAUDE_CODE_ENTRYPOINT) or the session said so; NULL when unknown. `entrypoint` keeps the raw
-- value the hook sent. A session with a desktop id (`external_id`) is a desktop one whatever these
-- say. `start_cwd` is the folder of the Claude Code session's first SessionStart, where `claude
-- --resume` finds the conversation again (`cwd` follows every hook). Additive: older hook clients
-- send no entrypoint and leave `surface` and `entrypoint` NULL.
ALTER TABLE session ADD COLUMN surface TEXT CHECK (surface IN ('desktop', 'terminal'));
ALTER TABLE session ADD COLUMN entrypoint TEXT;
ALTER TABLE session ADD COLUMN start_cwd TEXT;

-- Why a binding ended: 'session_end' when the session's own end unbound the task (a resume of
-- the same Claude Code session binds it again); NULL for every other way (by hand, a rebind).
ALTER TABLE task_session ADD COLUMN unbound_reason TEXT;
