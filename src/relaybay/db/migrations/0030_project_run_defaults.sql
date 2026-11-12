-- What a run on a card of the project starts with: model, effort and permission profile. The
-- folder is the project folder a run may use as its cwd, kept as written. Mode, effort and model
-- are checked in Python (relaybay.domain.runs), so a new CLI value needs no table rebuild.
CREATE TABLE project_run_defaults (
    project_id      INTEGER PRIMARY KEY REFERENCES project(id) ON DELETE CASCADE,
    folder          TEXT,
    model           TEXT NOT NULL DEFAULT 'opus',
    effort          TEXT NOT NULL DEFAULT 'medium',
    permission_mode TEXT NOT NULL DEFAULT 'auto',
    allowed_tools   TEXT NOT NULL DEFAULT 'mcp__relaybay',
    chrome          INTEGER NOT NULL DEFAULT 0 CHECK (chrome IN (0, 1)),
    updated_at      TEXT NOT NULL,
    updated_by      TEXT NOT NULL
);

-- Server-wide switches the owner sets on the board. `run_login_any` = '1': runs may start under
-- a sign-in that is not a subscription (an API key, a third-party provider). No row = off.
CREATE TABLE app_setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
) WITHOUT ROWID;
