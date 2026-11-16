-- Run defaults name a profile instead of their own mode and tools. A project whose pair differs
-- from the shipped `agent` keeps it as a profile `agent-<project>`, so no run changes its rights
-- by the upgrade. The old columns stay (unread) to keep this additive.
ALTER TABLE project_run_defaults ADD COLUMN profile TEXT NOT NULL DEFAULT 'agent';

INSERT INTO run_profile
    (name, description, permission_mode, allowed_tools, disallowed_tools, rights, kinds,
     updated_at, updated_by)
SELECT 'agent-' || p.slug,
       'The agent defaults ' || p.slug || ' had before profiles.',
       d.permission_mode, d.allowed_tools, a.disallowed_tools, a.rights, a.kinds,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'), 'relaybay'
FROM project_run_defaults d
JOIN project p ON p.id = d.project_id
JOIN run_profile a ON a.name = 'agent'
WHERE d.permission_mode != a.permission_mode OR d.allowed_tools != a.allowed_tools;

UPDATE project_run_defaults
SET profile = (SELECT 'agent-' || p.slug FROM project p WHERE p.id = project_id)
WHERE EXISTS (SELECT 1 FROM run_profile a WHERE a.name = 'agent'
              AND (project_run_defaults.permission_mode != a.permission_mode
                   OR project_run_defaults.allowed_tools != a.allowed_tools));

-- A run's profile by name (NULL: started before profiles; it keeps its own mode and tools), its
-- taken-away rules as last read from the profile, and `mode_set` = 1 when its mode was chosen for
-- it (the owner's Start form, run_settings) rather than followed from the profile. Runs from
-- before profiles keep their own mode.
ALTER TABLE run ADD COLUMN profile TEXT;
ALTER TABLE run ADD COLUMN disallowed_tools TEXT;
ALTER TABLE run ADD COLUMN mode_set INTEGER NOT NULL DEFAULT 0 CHECK (mode_set IN (0, 1));
UPDATE run SET mode_set = 1;

-- What a turn ran with: the profile's name and version when it started.
ALTER TABLE run_turn ADD COLUMN profile TEXT;
ALTER TABLE run_turn ADD COLUMN profile_version INTEGER;
