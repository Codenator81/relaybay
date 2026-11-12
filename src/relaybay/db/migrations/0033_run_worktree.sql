-- The folders a run works in, one row per repository it was started with: a `git worktree add`
-- the run created (`created_by_run` = 1, on `branch`) or the repository itself used in place.
-- `path_key` is the path as compared (case and separators folded, set in Python): one open run per
-- path is checked by the service against the open runs. `copied` is a JSON list of the untracked
-- files copied from the source repository into a worktree the run created. `removed_at` is set
-- when the worktree is removed (a later release).
CREATE TABLE run_worktree (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
    project_repo_id INTEGER REFERENCES project_repo(id) ON DELETE SET NULL,
    repo_name       TEXT NOT NULL,
    source_path     TEXT NOT NULL,
    path            TEXT NOT NULL,
    path_key        TEXT NOT NULL,
    branch          TEXT,
    created_by_run  INTEGER NOT NULL CHECK (created_by_run IN (0, 1)),
    copied          TEXT,
    created_at      TEXT NOT NULL,
    removed_at      TEXT
);
CREATE INDEX run_worktree_run ON run_worktree(run_id);
CREATE INDEX run_worktree_path ON run_worktree(path_key) WHERE removed_at IS NULL;
