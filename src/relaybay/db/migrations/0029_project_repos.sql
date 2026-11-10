-- A project's repositories, the ones a task touches and the task's pull requests. A path is kept
-- as written: the server never opens or lists it. `project.repo_path` stays for older readers and
-- is kept equal to the path of the first repository (lowest position) by every write. Additive.
CREATE TABLE project_repo (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    path       TEXT NOT NULL,
    kind       TEXT CHECK (kind IN ('app', 'web', 'api', 'lib', 'other')),
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, name)
);
CREATE INDEX project_repo_project ON project_repo(project_id, position);

CREATE TABLE task_repo (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    project_repo_id INTEGER NOT NULL REFERENCES project_repo(id) ON DELETE CASCADE,
    added_by        TEXT NOT NULL,
    how             TEXT NOT NULL CHECK (how IN ('auto', 'agent', 'owner')),
    added_at        TEXT NOT NULL,
    UNIQUE (task_id, project_repo_id)
);
CREATE INDEX task_repo_repo ON task_repo(project_repo_id);

CREATE TABLE task_pr (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    project_repo_id INTEGER REFERENCES project_repo(id) ON DELETE SET NULL,
    url             TEXT NOT NULL,
    number          INTEGER,
    title           TEXT,
    added_by        TEXT NOT NULL,
    added_at        TEXT NOT NULL,
    UNIQUE (task_id, url)
);
CREATE INDEX task_pr_repo ON task_pr(project_repo_id) WHERE project_repo_id IS NOT NULL;
-- The hook path finds a session's bound tasks (and their repositories) with one indexed read.
CREATE INDEX task_session_open_session ON task_session(session_id) WHERE unbound_at IS NULL;

-- One repository per project that has a path, named after its last folder (either slash style)
-- the way a repository name must look: each run of characters outside [A-Za-z0-9._-] becomes one
-- '-', leading and trailing '-' and '.' go, at most 60 characters. A bare drive or root, or a name
-- that is then empty or does not start with a letter or digit, takes the project's slug.
WITH RECURSIVE cleaned AS (
    SELECT id, slug, repo_path,
           replace(rtrim(trim(repo_path), '/\'), '\', '/') AS p
    FROM project
    WHERE repo_path IS NOT NULL AND trim(repo_path) != ''
),
base AS (
    SELECT id, slug, repo_path,
           CASE WHEN p GLOB '[A-Za-z]:' THEN ''
                ELSE replace(p, rtrim(p, replace(p, '/', '')), '') END AS folder
    FROM cleaned
),
walk (id, rest, out) AS (
    SELECT id, folder, '' FROM base
    UNION ALL
    SELECT id,
           substr(rest, 2),
           out || CASE WHEN substr(rest, 1, 1) GLOB '[A-Za-z0-9._-]' THEN substr(rest, 1, 1)
                       WHEN substr(out, -1) = '-' THEN ''
                       ELSE '-' END
    FROM walk
    WHERE rest != ''
),
named AS (
    SELECT id, substr(trim(out, '-.'), 1, 60) AS name FROM walk WHERE rest = ''
)
INSERT INTO project_repo (project_id, name, path, kind, position, created_at, updated_at)
SELECT b.id,
       CASE WHEN n.name GLOB '[A-Za-z0-9]*' THEN n.name ELSE b.slug END,
       trim(b.repo_path),
       NULL,
       0,
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
       strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
FROM base b JOIN named n ON n.id = b.id;
