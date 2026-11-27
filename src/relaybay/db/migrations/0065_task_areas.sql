-- Several areas on one card (design 0029). `task_area` holds a card's areas in order; the
-- first is also kept in `task.area_id`, so a rollback to an older version still shows it and
-- `task` needs no rebuild. Saved filters keep a JSON list of names in `areas`, `area` mirrors
-- the first. Additive: no existing area's id, name or colour changes.
-- 0048 created an index named `task_area`; tables and indexes share one namespace.
DROP INDEX task_area;
CREATE INDEX task_first_area ON task(area_id) WHERE area_id IS NOT NULL;

CREATE TABLE task_area (
    task_id  INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
    area_id  INTEGER NOT NULL REFERENCES project_area(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, area_id)
);
CREATE INDEX task_area_area ON task_area(area_id);
INSERT INTO task_area (task_id, area_id, position)
SELECT id, area_id, 0 FROM task WHERE area_id IS NOT NULL;

ALTER TABLE saved_filter ADD COLUMN areas TEXT;
UPDATE saved_filter SET areas = json_array(area) WHERE area IS NOT NULL;
