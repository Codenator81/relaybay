-- A permission profile: what a run's turns start with (mode, --allowedTools and --disallowedTools
-- rules, each list joined by ", ") and what a run under it may do on the board (`rights`, a JSON
-- object; `kinds`, a JSON list of 'card', 'coordinator', 'check'). Edited by the owner on the
-- board; each edit adds 1 to `version`, which a turn records. Values are checked in Python
-- (relaybay.domain.profiles), which also adds the fixed taken-away tools to every turn.
CREATE TABLE run_profile (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    description      TEXT NOT NULL DEFAULT '',
    permission_mode  TEXT NOT NULL,
    allowed_tools    TEXT NOT NULL,
    disallowed_tools TEXT NOT NULL DEFAULT '',
    rights           TEXT NOT NULL DEFAULT '{}',
    kinds            TEXT NOT NULL DEFAULT '["card"]',
    builtin          INTEGER NOT NULL DEFAULT 0 CHECK (builtin IN (0, 1)),
    version          INTEGER NOT NULL DEFAULT 1,
    updated_at       TEXT NOT NULL,
    updated_by       TEXT NOT NULL,
    CHECK (permission_mode != 'bypassPermissions')
);

-- The shipped rows, the same as relaybay.domain.profiles.BUILTIN. The coordinator's Edit and
-- Write inside its own folder are added by its run at start, not stored here.
INSERT INTO run_profile
    (name, description, permission_mode, allowed_tools, disallowed_tools, rights, kinds, builtin,
     updated_at, updated_by)
VALUES
    ('agent', 'A run on a card: auto mode, relaybay''s MCP tools allowed.', 'auto',
     'mcp__relaybay', 'CronCreate, CronDelete, ScheduleWakeup, RemoteTrigger',
     '{"messages": 1, "plan_accept": 0, "rules_update": 0, "run_settings": 0, "run_start": 0, "run_start_per_day": 0, "schedule_add": 0, "tasks_write": 0}',
     '["card"]', 1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'relaybay'),
    ('coordinator', 'The coordinator run: reads, the board and read-only git and gh.', 'dontAsk',
     'Read, Glob, Grep, Task, WebFetch, WebSearch, mcp__relaybay, Bash(git log *), Bash(git status *), Bash(git diff *), Bash(git show *), Bash(gh pr view *), Bash(gh pr checks *)',
     'CronCreate, CronDelete, ScheduleWakeup, RemoteTrigger',
     '{"messages": 1, "plan_accept": 1, "rules_update": 1, "run_settings": 1, "run_start": 1, "run_start_per_day": 10, "schedule_add": 1, "tasks_write": 1}',
     '["coordinator"]', 1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'relaybay'),
    ('read-only', 'A one-shot check: reads and read-only git and gh, nothing on the board.',
     'dontAsk',
     'Read, Glob, Grep, Bash(git log *), Bash(git status *), Bash(git diff *), Bash(git show *), Bash(gh pr view *), Bash(gh pr checks *)',
     'CronCreate, CronDelete, ScheduleWakeup, RemoteTrigger',
     '{"messages": 0, "plan_accept": 0, "rules_update": 0, "run_settings": 0, "run_start": 0, "run_start_per_day": 0, "schedule_add": 0, "tasks_write": 0}',
     '["check"]', 1, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'relaybay');
