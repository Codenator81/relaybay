-- Areas (design 0024): what part of a project a card belongs to ("web shop", "api"). A list per
-- project that grows from use; names are unique without regard to case and outer spaces (the
-- service trims them). `color` is a palette key, not a CSS value: the board maps it to tokens
-- that read in both themes. A task has one area or none; deleting an area clears it from its
-- tasks. A saved board filter may name an area. Additive: no table is rebuilt.
CREATE TABLE project_area (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL,
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX project_area_name ON project_area(project_id, lower(name));
CREATE INDEX project_area_project ON project_area(project_id, position);

ALTER TABLE task ADD COLUMN area_id INTEGER REFERENCES project_area(id) ON DELETE SET NULL;
CREATE INDEX task_area ON task(area_id) WHERE area_id IS NOT NULL;

ALTER TABLE saved_filter ADD COLUMN area TEXT;
